#!/usr/bin/env python3
"""Self-contained Phase 2 smoke test. Uses a temporary SQLite database."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase2-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase2.db")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["ENVIRONMENT"] = "development"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
from platform_core import db, now_dt  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f"No CSRF token on {path}"
    return match.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"},
        follow_redirects=False,
    )
    assert response.status_code == 303, (email, response.status_code)


with TestClient(app.app) as public:
    exact = public.get("/api/search", params={"q": "IQ233UNB230VAC"}).json()
    assert exact["count"] >= 1 and exact["results"][0]["match_score"] >= 99
    fuzzy = public.get("/api/search", params={"q": "IQ233UNB230VCA"}).json()
    assert fuzzy["count"] >= 1
    with db() as conn:
        item = conn.execute("SELECT id FROM inventory WHERE part_number='IQ233/UNB/230VAC'").fetchone()
        old = (now_dt() - timedelta(days=91)).isoformat(timespec="seconds")
        conn.execute("UPDATE inventory SET last_confirmed_at=? WHERE id=?", (old, item["id"]))
    assert public.get("/api/search", params={"q": "IQ233"}).json()["count"] == 0

buyer = TestClient(app.app)
supplier = TestClient(app.app)
login(buyer, "buyer@example.com", "Buyer123!")
login(supplier, "supplier@example.com", "Supplier123!")

supplier.post("/inventory/confirm-all", data={"csrf_token": csrf(supplier, "/inventory")}, follow_redirects=False)
assert buyer.get("/api/search", params={"q": "IQ233"}).json()["count"] >= 1

buyer.post("/saved-searches", data={"csrf_token": csrf(buyer, "/"), "query": "PXC999-Z", "brand": "", "condition": ""}, follow_redirects=False)
with db() as conn:
    search_id = conn.execute("SELECT id FROM saved_searches WHERE query='PXC999-Z'").fetchone()["id"]
csv = b"Manufacturer,Part Number,Description,Condition,Quantity,Location\nSiemens,PXC999-Z,Test controller,New old stock,2,Birmingham\n"
supplier.post("/inventory/upload", data={"csrf_token": csrf(supplier, "/inventory"), "replace_existing": "no"}, files={"file": ("stock.csv", csv, "text/csv")}, follow_redirects=False)
with db() as conn:
    assert conn.execute("SELECT COUNT(*) AS n FROM saved_search_matches WHERE saved_search_id=?", (search_id,)).fetchone()["n"] == 1

buyer.post("/wanted", data={"csrf_token": csrf(buyer, "/"), "query": "LEGACY-777", "quantity": "1", "notes": "urgent"}, follow_redirects=False)
with db() as conn:
    wanted_id = conn.execute("SELECT id FROM wanted_requests WHERE query='LEGACY-777'").fetchone()["id"]
csv = b"Manufacturer,Part Number,Description,Condition,Quantity,Location\nTrend,LEGACY-777,Legacy controller,Refurbished,1,Birmingham\n"
supplier.post("/inventory/upload", data={"csrf_token": csrf(supplier, "/inventory"), "replace_existing": "no"}, files={"file": ("legacy.csv", csv, "text/csv")}, follow_redirects=False)
with db() as conn:
    assert conn.execute("SELECT COUNT(*) AS n FROM wanted_matches WHERE wanted_id=?", (wanted_id,)).fetchone()["n"] == 1

result = buyer.get("/api/search", params={"q": "IQ233"}).json()["results"][0]
response = buyer.post("/rfqs", data={"csrf_token": csrf(buyer, "/"), "inventory_ids": str(result["id"]), "part_number": "IQ233/UNB/230VAC", "quantity": "2"}, follow_redirects=False)
rfq_id = int(response.headers["location"].split("/")[-1])
supplier.post(f"/rfqs/{rfq_id}/respond", data={"csrf_token": csrf(supplier, f"/rfqs/{rfq_id}"), "status": "quoted", "quoted_price": "450", "quoted_currency": "GBP", "supplier_message": "Next-day dispatch"}, follow_redirects=False)
with db() as conn:
    recipient_id = conn.execute("SELECT id FROM rfq_recipients WHERE rfq_id=?", (rfq_id,)).fetchone()["id"]
buyer.post(f"/rfqs/{rfq_id}/messages", data={"csrf_token": csrf(buyer, f"/rfqs/{rfq_id}?recipient={recipient_id}"), "recipient_id": str(recipient_id), "body": "Confirm revision?"}, follow_redirects=False)
supplier.post(f"/rfqs/{rfq_id}/messages", data={"csrf_token": csrf(supplier, f"/rfqs/{rfq_id}"), "recipient_id": str(recipient_id), "body": "Revision B."}, follow_redirects=False)
buyer.post(f"/rfqs/{rfq_id}/accept/{recipient_id}", data={"csrf_token": csrf(buyer, f"/rfqs/{rfq_id}?recipient={recipient_id}")}, follow_redirects=False)
with db() as conn:
    recipient = conn.execute("SELECT status,accepted_at FROM rfq_recipients WHERE id=?", (recipient_id,)).fetchone()
    rfq = conn.execute("SELECT status FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    assert recipient["status"] == "accepted" and recipient["accepted_at"] and rfq["status"] == "awarded"
    assert conn.execute("SELECT COUNT(*) AS n FROM rfq_messages WHERE recipient_id=?", (recipient_id,)).fetchone()["n"] == 2

print("PHASE2_SMOKE_TEST_OK")
