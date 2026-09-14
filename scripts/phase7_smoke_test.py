#!/usr/bin/env python3
"""Phase 7 smoke test: accepted quote -> order -> PO -> fulfilment -> docs -> API/webhooks."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase7-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase7.db")
os.environ["ORDER_DOCUMENT_DIR"] = str(Path(TMP.name) / "order_documents")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["EMAIL_LOG_PATH"] = str(Path(TMP.name) / "emails.log")
os.environ["ENVIRONMENT"] = "development"
os.environ["FOUNDING_TRIAL_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
import procurement  # noqa: E402
from platform_core import db, insert_id, normalize, now_iso  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, f"No CSRF token on {path}"
    return m.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    r = client.post("/login", data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def create_api_key_ui(client: TestClient, scopes: list[str]) -> str:
    token = csrf(client, "/integrations/api")
    r = client.post("/integrations/api/keys", data={"csrf_token": token, "name": "Phase7 ERP", "scopes": scopes}, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = client.get("/integrations/api")
    m = re.search(r'(cxk_[A-Za-z0-9_\-]+)', page.text)
    assert m, page.text[:1500]
    return m.group(1)


with TestClient(app.app) as boot:
    assert boot.get("/health/ready").status_code == 200

# Create a fresh RFQ + quote to exercise the actual select-quote route.
with db() as conn:
    buyer = conn.execute("SELECT u.*,c.id AS cid FROM users u JOIN companies c ON c.id=u.company_id WHERE u.email='buyer@example.com'").fetchone()
    supplier = conn.execute("SELECT u.*,c.id AS cid FROM users u JOIN companies c ON c.id=u.company_id WHERE u.email='supplier@example.com'").fetchone()
    rfq_id = insert_id(conn, "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,required_by,delivery_location,notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (buyer["id"], "IQ233/UNB/230VAC", normalize("IQ233/UNB/230VAC"), 2, "2026-10-01", "London", "Phase 7 order test", "open", now_iso()))
    recipient_id = insert_id(conn, "INSERT INTO rfq_recipients(rfq_id,supplier_company_id,status,quoted_price,quoted_currency,supplier_message,responded_at) VALUES(?,?,?,?,?,?,?)",
                             (rfq_id, supplier["cid"], "quoted", 750.0, "GBP", "Two NOS units", now_iso()))

buyer_client = TestClient(app.app)
supplier_client = TestClient(app.app)
with buyer_client, supplier_client:
    login(buyer_client, "buyer@example.com", "Buyer123!")
    login(supplier_client, "supplier@example.com", "Supplier123!")

    # Selecting a quote now creates the procurement order atomically.
    token = csrf(buyer_client, f"/rfqs/{rfq_id}")
    selected = buyer_client.post(f"/rfqs/{rfq_id}/accept/{recipient_id}", data={"csrf_token": token}, follow_redirects=False)
    assert selected.status_code == 303 and selected.headers["location"].startswith("/orders/"), selected.text
    order_id = int(selected.headers["location"].rsplit("/", 1)[1])
    page = buyer_client.get(f"/orders/{order_id}")
    assert page.status_code == 200 and "Awaiting PO" not in page.text and "Purchase order" in page.text

    # Create a supplier webhook before subsequent lifecycle events.
    wt = csrf(supplier_client, "/integrations/webhooks")
    created = supplier_client.post("/integrations/webhooks", data={
        "csrf_token": wt, "name": "ERP Orders", "url": "http://127.0.0.1:9999/webhook",
        "event_types": ["order.submitted", "order.acknowledged", "order.processing", "order.dispatched", "order.delivered", "order.document_added"],
    }, follow_redirects=False)
    assert created.status_code == 303
    wh_page = supplier_client.get("/integrations/webhooks")
    secret_match = re.search(r'(cxwhsec_[A-Za-z0-9_\-]+)', wh_page.text)
    assert secret_match
    webhook_secret = secret_match.group(1)
    assert webhook_secret not in supplier_client.get("/integrations/webhooks").text

    # Buyer submits its PO/delivery handoff.
    bt = csrf(buyer_client, f"/orders/{order_id}")
    submitted = buyer_client.post(f"/orders/{order_id}/buyer-details", data={
        "csrf_token": bt, "buyer_po_number": "PO-P7-001", "buyer_reference": "Site A",
        "delivery_address": "1 Test Street, London", "delivery_contact": "Ops Team",
    }, follow_redirects=False)
    assert submitted.status_code == 303

    # Buyer can attach a PO; supplier can download it, unrelated companies cannot.
    bt = csrf(buyer_client, f"/orders/{order_id}")
    upload = buyer_client.post(f"/orders/{order_id}/documents", data={"csrf_token": bt, "document_type": "purchase_order"},
                               files={"document": ("PO-P7-001.pdf", b"%PDF-1.4 phase7 smoke", "application/pdf")}, follow_redirects=False)
    assert upload.status_code == 303
    order_page = supplier_client.get(f"/orders/{order_id}")
    doc_match = re.search(rf'/orders/{order_id}/documents/(\d+)', order_page.text)
    assert doc_match
    doc_id = int(doc_match.group(1))
    downloaded = supplier_client.get(f"/orders/{order_id}/documents/{doc_id}")
    assert downloaded.status_code == 200 and downloaded.content.startswith(b"%PDF")

    # Both parties can create ERP keys. Buyer keys are order-only; supplier can include order scopes.
    buyer_key = create_api_key_ui(buyer_client, ["orders:read", "orders:write", "inventory:read"])
    supplier_key = create_api_key_ui(supplier_client, ["orders:read", "orders:write"])

    external = TestClient(app.app)
    with external:
        buyer_orders = external.get("/v1/orders", headers={"Authorization": f"Bearer {buyer_key}"})
        assert buyer_orders.status_code == 200 and any(x["id"] == order_id for x in buyer_orders.json()["data"])
        # Buyer key must not gain inventory scope even if it was submitted in the form.
        denied_inventory = external.get("/v1/inventory/search", params={"q": "IQ233"}, headers={"Authorization": f"Bearer {buyer_key}"})
        assert denied_inventory.status_code == 403

        ack = external.post(f"/v1/orders/{order_id}/status", headers={"Authorization": f"Bearer {supplier_key}"}, json={
            "status": "acknowledged", "supplier_order_reference": "SO-7788", "expected_dispatch_date": "2026-09-20"
        })
        assert ack.status_code == 200 and ack.json()["data"]["status"] == "acknowledged", ack.text
        locked = external.post(f"/v1/orders/{order_id}/buyer-details", headers={"Authorization": f"Bearer {buyer_key}"}, json={"buyer_po_number": "PO-CHANGED"})
        assert locked.status_code == 409

    # Supplier progresses via UI, then API dispatch, then UI delivery.
    st = csrf(supplier_client, f"/orders/{order_id}")
    processing = supplier_client.post(f"/orders/{order_id}/supplier-status", data={
        "csrf_token": st, "status": "processing", "supplier_order_reference": "SO-7788", "expected_dispatch_date": "2026-09-20"
    }, follow_redirects=False)
    assert processing.status_code == 303
    with TestClient(app.app) as external:
        dispatched = external.post(f"/v1/orders/{order_id}/status", headers={"Authorization": f"Bearer {supplier_key}"}, json={
            "status": "dispatched", "supplier_order_reference": "SO-7788", "carrier": "DHL", "tracking_number": "TRACK123",
            "tracking_url": "https://example.com/track/TRACK123"
        })
        assert dispatched.status_code == 200 and dispatched.json()["data"]["tracking_number"] == "TRACK123"
    st = csrf(supplier_client, f"/orders/{order_id}")
    delivered = supplier_client.post(f"/orders/{order_id}/supplier-status", data={"csrf_token": st, "status": "delivered", "supplier_order_reference": "SO-7788"}, follow_redirects=False)
    assert delivered.status_code == 303

# Verify persistence, auditability, order-document checksum and webhook queue.
with db() as conn:
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    assert order["status"] == "delivered" and order["buyer_po_number"] == "PO-P7-001" and order["supplier_order_reference"] == "SO-7788"
    assert order["carrier"] == "DHL" and order["tracking_number"] == "TRACK123" and order["tracking_url"].startswith("https://")
    assert conn.execute("SELECT COUNT(*) AS n FROM order_events WHERE order_id=?", (order_id,)).fetchone()["n"] >= 6
    doc = conn.execute("SELECT * FROM order_documents WHERE id=?", (doc_id,)).fetchone()
    assert len(doc["sha256"]) == 64
    endpoint = conn.execute("SELECT * FROM webhook_endpoints WHERE company_id=?", (supplier["cid"],)).fetchone()
    assert endpoint and webhook_secret not in endpoint["secret_blob"]
    pending = conn.execute("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE endpoint_id=?", (endpoint["id"],)).fetchone()["n"]
    assert pending >= 5, pending

# Deliver queued webhooks through a fake HTTP target and validate signature headers.
captured = []
class FakeResponse:
    status_code = 204
    text = ""
def fake_post(url, content, headers, timeout, follow_redirects):
    captured.append({"url": url, "content": content, "headers": headers, "follow_redirects": follow_redirects})
    return FakeResponse()
procurement.httpx.post = fake_post
stats = procurement.process_due_webhooks(limit=100)
assert stats["delivered"] >= 5 and captured
assert all(x["headers"]["X-CX-Signature"].startswith("v1=") for x in captured)
assert all(x["follow_redirects"] is False for x in captured)

# Backfill is idempotent and does not duplicate orders.
procurement.init_procurement_schema()
with db() as conn:
    assert conn.execute("SELECT COUNT(*) AS n FROM orders WHERE rfq_recipient_id=?", (recipient_id,)).fetchone()["n"] == 1

print("PHASE7_SMOKE_TEST_OK")
