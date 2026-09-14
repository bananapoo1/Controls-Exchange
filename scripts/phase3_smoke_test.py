#!/usr/bin/env python3
"""Self-contained Phase 3 smoke/regression test using a temporary SQLite database."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase3-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase3.db")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["ENVIRONMENT"] = "development"
os.environ["INTEGRATION_ENCRYPTION_KEY"] = "phase3-test-integration-encryption-key-123456789"
os.environ["INVENTORY_IMPORT_EMAIL_DOMAIN"] = "imports.test"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
import inventory_ingestion as ing  # noqa: E402
from platform_core import db, encrypt_secret_dict, hash_password, now_iso  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:500])
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f"No CSRF token on {path}"
    return match.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/login", data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"}, follow_redirects=False)
    assert response.status_code == 303


supplier = TestClient(app.app)
with supplier:
    login(supplier, "supplier@example.com", "Supplier123!")

    # Manual mapped import: preview, duplicate merge, validation error and report.
    preview_csv = b"Maker,ItemCode,Description,Condition,Qty,Depot\nTrend,CX-300,Controller,New old stock,2,Birmingham\nTrend,CX-300,Controller,New old stock,3,Birmingham\nTrend,BAD-QTY,Bad quantity,Used,abc,Birmingham\n"
    response = supplier.post(
        "/inventory-imports/preview",
        data={"csrf_token": csrf(supplier, "/inventory")},
        files={"file": ("mapped.csv", preview_csv, "text/csv")},
    )
    assert response.status_code == 200 and "Map your columns" in response.text
    stage_id = int(re.search(r"/inventory-imports/stage/(\d+)/execute", response.text).group(1))
    response = supplier.post(
        f"/inventory-imports/stage/{stage_id}/execute",
        data={
            "csrf_token": csrf(supplier, "/inventory"),
            "map_brand": "Maker",
            "map_part_number": "ItemCode",
            "map_description": "Description",
            "map_condition": "Condition",
            "map_quantity": "Qty",
            "map_location": "Depot",
            "import_mode": "upsert",
            "duplicate_policy": "merge",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = int(response.headers["location"].split("/")[-1])
    report = supplier.get(f"/inventory-imports/{job_id}")
    assert report.status_code == 200 and "invalid_quantity" in report.text and "duplicate_in_file" in report.text
    with db() as conn:
        row = conn.execute("SELECT quantity FROM inventory WHERE company_id=(SELECT company_id FROM users WHERE email='supplier@example.com') AND part_number='CX-300' AND active=1").fetchone()
        assert row and row["quantity"] == 5
        job = conn.execute("SELECT duplicate_rows,error_rows,status FROM inventory_import_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["duplicate_rows"] == 1 and job["error_rows"] == 1 and job["status"] == "completed_with_errors"

    # Legacy Phase 1/2 stock should be updated rather than duplicated on first Phase 3 upsert.
    legacy_csv = b"Manufacturer,Part Number,Description,Condition,Quantity,Location\nTrend,IQ233/UNB/230VAC,Updated description,New old stock,11,Birmingham, UK\n"
    # CSV contains a comma in location, so use XLSX-like equivalent CSV with quoted location.
    legacy_csv = b'Manufacturer,Part Number,Description,Condition,Quantity,Location\nTrend,IQ233/UNB/230VAC,Updated description,New old stock,11,"Birmingham, UK"\n'
    quick = supplier.post("/inventory/upload", data={"csrf_token": csrf(supplier, "/inventory"), "replace_existing": "no"}, files={"file": ("legacy.csv", legacy_csv, "text/csv")}, follow_redirects=False)
    assert quick.status_code == 303
    with db() as conn:
        rows = conn.execute("SELECT id,quantity,source_key FROM inventory WHERE part_number='IQ233/UNB/230VAC' AND active=1 AND deleted_at IS NULL").fetchall()
        assert len(rows) == 1 and rows[0]["quantity"] == 11 and rows[0]["source_key"]

    # Create push API feed and verify encrypted token + upsert behaviour.
    token = csrf(supplier, "/inventory-feeds/new")
    created = supplier.post(
        "/inventory-feeds/new",
        data={
            "csrf_token": token,
            "name": "ERP Push",
            "feed_type": "api_push",
            "schedule_minutes": "60",
            "import_mode": "upsert",
            "duplicate_policy": "merge",
            "map_brand": "brand",
            "map_part_number": "part",
            "map_description": "description",
            "map_condition": "condition",
            "map_quantity": "qty",
            "map_location": "location",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE name='ERP Push'").fetchone()
        raw_token = ing.feed_secrets(feed)["inbound_token"]
        assert raw_token not in feed["secret_blob"] and feed["ingest_token_hash"]
        push_feed_id = feed["id"]
    payload = [{"brand": "Honeywell", "part": "API-100", "description": "API part", "condition": "New", "qty": 4, "location": "Birmingham"}]
    r = supplier.post(f"/api/inventory-feed/{raw_token}", json=payload)
    assert r.status_code == 200 and r.json()["counts"]["inserted"] == 1
    payload[0]["qty"] = 9
    r = supplier.post(f"/api/inventory-feed/{raw_token}", json=payload)
    assert r.status_code == 200 and r.json()["counts"]["updated"] == 1
    with db() as conn:
        rows = conn.execute("SELECT quantity FROM inventory WHERE source_feed_id=? AND part_number='API-100' AND active=1", (push_feed_id,)).fetchall()
        assert len(rows) == 1 and rows[0]["quantity"] == 9

    # Email feed webhook accepts an attached file, and the UI exposes an inbound alias.
    supplier.post(
        "/inventory-feeds/new",
        data={
            "csrf_token": csrf(supplier, "/inventory-feeds/new"),
            "name": "Email Stock",
            "feed_type": "email",
            "schedule_minutes": "60",
            "import_mode": "upsert",
            "duplicate_policy": "merge",
        },
        follow_redirects=False,
    )
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE name='Email Stock'").fetchone()
        email_token = ing.feed_secrets(feed)["inbound_token"]
        assert ing.inbound_address(feed).startswith("imports+") and ing.inbound_address(feed).endswith("@imports.test")
    email_csv = b"Manufacturer,Part Number,Quantity\nSiemens,MAIL-42,2\n"
    r = supplier.post(f"/api/inventory-email/{email_token}", files={"attachment": ("mail-stock.csv", email_csv, "text/csv")})
    assert r.status_code == 200 and r.json()["counts"]["inserted"] == 1

    # Feed-scoped sync: retire missing feed lines, but never retire on a partially invalid source.
    with db() as conn:
        company_id = conn.execute("SELECT company_id FROM users WHERE email='supplier@example.com'").fetchone()["company_id"]
        feed_id = ing.insert_id(conn, """INSERT INTO inventory_feeds(company_id,name,feed_type,enabled,schedule_minutes,next_run_at,config_json,secret_blob,mapping_json,import_mode,duplicate_policy,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (company_id, "Nightly URL", "url", 1, 60, now_iso(), json.dumps({"url":"https://example.test/stock.csv"}), "", json.dumps({}), "sync", "merge", now_iso(), now_iso()))

    original_fetch = ing.fetch_feed_payload
    state = {"payload": b"Manufacturer,Part Number,Quantity\nTrend,SYNC-A,1\nTrend,SYNC-B,2\n"}
    ing.fetch_feed_payload = lambda feed: ("stock.csv", state["payload"], None)
    try:
        _job, changed, counts = ing.run_feed(feed_id)
        app.process_inventory_match_alerts(changed)
        assert counts["inserted"] == 2
        state["payload"] = b"Manufacturer,Part Number,Quantity\nTrend,SYNC-A,7\n"
        _job, _changed, counts = ing.run_feed(feed_id)
        assert counts["updated"] == 1 and counts["archived"] == 1
        with db() as conn:
            assert conn.execute("SELECT active FROM inventory WHERE source_feed_id=? AND part_number='SYNC-B'", (feed_id,)).fetchone()["active"] == 0
        # Restore B with a clean sync, then make B invalid. Retirement must be suppressed.
        state["payload"] = b"Manufacturer,Part Number,Quantity\nTrend,SYNC-A,7\nTrend,SYNC-B,2\n"
        ing.run_feed(feed_id)
        state["payload"] = b"Manufacturer,Part Number,Quantity\nTrend,SYNC-A,8\nTrend,SYNC-B,not-a-number\n"
        job_id, _changed, counts = ing.run_feed(feed_id)
        assert counts["errors"] == 1 and counts["archived"] == 0
        with db() as conn:
            assert conn.execute("SELECT active FROM inventory WHERE source_feed_id=? AND part_number='SYNC-B'", (feed_id,)).fetchone()["active"] == 1
            report = json.loads(conn.execute("SELECT report_json FROM inventory_import_jobs WHERE id=?", (job_id,)).fetchone()["report_json"])
            assert report["sync_retirement_suppressed"] is True
        state["payload"] = b""
        try:
            ing.run_feed(feed_id)
            raise AssertionError("empty sync should have failed")
        except ValueError as exc:
            assert "zero rows" in str(exc)
    finally:
        ing.fetch_feed_payload = original_fetch

    # SFTP implementation is exercised with a small fake Paramiko transport (no external server required).
    class FakeSFTP:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def open(self, path, mode):
            import io
            return io.BytesIO(b"Manufacturer,Part Number,Quantity\nABB,SFTP-1,3\n")
    class FakeClient:
        def load_system_host_keys(self): pass
        def set_missing_host_key_policy(self, p): pass
        def connect(self, **kwargs):
            assert kwargs["password"] == "sftp-secret"
        def open_sftp(self): return FakeSFTP()
        def close(self): pass
    fake = types.SimpleNamespace(
        SSHClient=FakeClient,
        MissingHostKeyPolicy=object,
        AutoAddPolicy=lambda: object(),
        SSHException=Exception,
        Ed25519Key=types.SimpleNamespace(from_private_key=lambda *a, **k: None),
        RSAKey=types.SimpleNamespace(from_private_key=lambda *a, **k: None),
        ECDSAKey=types.SimpleNamespace(from_private_key=lambda *a, **k: None),
    )
    sys.modules["paramiko"] = fake
    sftp_feed = {
        "feed_type": "sftp",
        "config_json": json.dumps({"host":"sftp.example","port":22,"username":"stock","remote_path":"/stock.csv"}),
        "secret_blob": encrypt_secret_dict({"password":"sftp-secret"}),
    }
    name, raw, json_rows = ing.fetch_feed_payload(sftp_feed)
    assert name == "stock.csv" and b"SFTP-1" in raw and json_rows is None

    # Token rotation immediately revokes the old push endpoint.
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=?", (push_feed_id,)).fetchone()
        old_push_token = ing.feed_secrets(feed)["inbound_token"]
    rotate = supplier.post(f"/inventory-feeds/{push_feed_id}/regenerate-token", data={"csrf_token": csrf(supplier, "/inventory-feeds")}, follow_redirects=False)
    assert rotate.status_code == 303
    assert supplier.post(f"/api/inventory-feed/{old_push_token}", json=payload).status_code == 404
    with db() as conn:
        new_push_token = ing.feed_secrets(conn.execute("SELECT * FROM inventory_feeds WHERE id=?", (push_feed_id,)).fetchone())["inbound_token"]
    assert new_push_token != old_push_token and supplier.post(f"/api/inventory-feed/{new_push_token}", json=payload).status_code == 200

    # Scheduled worker claims due feeds and records success.
    sys.path.insert(0, str(ROOT / "scripts"))
    import feed_worker  # noqa: E402
    with db() as conn:
        conn.execute("UPDATE inventory_feeds SET next_run_at=?,enabled=1 WHERE id=?", (now_iso(), feed_id))
    original_fetch = ing.fetch_feed_payload
    ing.fetch_feed_payload = lambda feed: ("stock.csv", b"Manufacturer,Part Number,Quantity\nTrend,WORKER-1,1\n", None)
    try:
        processed = feed_worker.run_due_feeds_once()
        assert processed >= 1
    finally:
        ing.fetch_feed_payload = original_fetch
    with db() as conn:
        assert conn.execute("SELECT last_status FROM inventory_feeds WHERE id=?", (feed_id,)).fetchone()["last_status"] == "success"
        assert conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE source_feed_id=? AND part_number='WORKER-1' AND active=1", (feed_id,)).fetchone()["n"] == 1

    # Direct mailbox ingestion parses the plus-address token and attachment.
    from email.message import EmailMessage
    with db() as conn:
        email_feed = conn.execute("SELECT * FROM inventory_feeds WHERE name='Email Stock'").fetchone()
        alias = ing.inbound_address(email_feed)
    msg = EmailMessage()
    msg["From"] = "supplier@example.test"
    msg["To"] = alias
    msg["Subject"] = "Nightly inventory"
    msg.set_content("Attached")
    msg.add_attachment(b"Manufacturer,Part Number,Quantity\nSiemens,IMAP-77,5\n", maintype="text", subtype="csv", filename="imap-stock.csv")
    assert feed_worker._process_email_message(msg) is True
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE part_number='IMAP-77' AND active=1").fetchone()["n"] == 1

    # Another supplier cannot inspect this company's feeds or import reports.
    with db() as conn:
        other_company = ing.insert_id(conn, "INSERT INTO companies(name,company_type,location,verified,active,created_at) VALUES(?,?,?,?,?,?)", ("Other Supplier Ltd","supplier","Leeds, UK",1,1,now_iso()))
        ing.insert_id(conn, "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (other_company,"Other User","other@example.com",hash_password("Other123!"),"supplier","owner",1,now_iso(),1,now_iso()))
    other = TestClient(app.app)
    login(other, "other@example.com", "Other123!")
    assert other.get(f"/inventory-imports/{job_id}").status_code == 404
    assert other.get(f"/inventory-feeds/{push_feed_id}/edit").status_code == 404

    # Page rendering sanity.
    assert supplier.get("/inventory-feeds").status_code == 200
    assert supplier.get("/inventory-imports").status_code == 200

print("PHASE3_SMOKE_TEST_OK")
