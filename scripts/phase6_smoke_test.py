#!/usr/bin/env python3
"""Phase 6 smoke test: privacy-safe intelligence, opportunity ranking and API/ERP access."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase6-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase6.db")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["EMAIL_LOG_PATH"] = str(Path(TMP.name) / "emails.log")
os.environ["ENVIRONMENT"] = "development"
os.environ["FOUNDING_TRIAL_ENABLED"] = "true"
os.environ["INTELLIGENCE_MIN_DEMAND_EVENTS"] = "5"
os.environ["INTELLIGENCE_MIN_BUYER_COMPANIES"] = "3"
os.environ["INTELLIGENCE_MIN_QUOTE_SAMPLES"] = "5"
os.environ["INTELLIGENCE_MIN_QUOTE_SUPPLIERS"] = "3"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
from billing import reconcile_plan_limits, subscription_for  # noqa: E402
from intelligence import pricing_intelligence, record_demand_event, supplier_opportunities  # noqa: E402
from platform_core import db, hash_password, insert_id, normalize, now_iso  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, f"No CSRF token on {path}"
    return m.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    r = client.post("/login", data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def make_company(conn, name: str, email: str, kind: str, role: str = "buyer") -> tuple[int, int]:
    company_type = "buyer" if role == "buyer" else "supplier"
    company_id = insert_id(conn, "INSERT INTO companies(name,company_type,account_kind,location,verified,active,created_at) VALUES(?,?,?,?,?,?,?)",
                           (name, company_type, kind, "London, UK", 1, 1, now_iso()))
    user_id = insert_id(conn, "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (company_id, f"{name} Owner", email, hash_password("Password123!"), role, "owner", 1, now_iso(), 1, now_iso()))
    return company_id, user_id


with TestClient(app.app) as boot:
    assert boot.get("/health/ready").status_code == 200

with db() as conn:
    supplier = conn.execute("SELECT c.*,u.id AS user_id FROM companies c JOIN users u ON u.company_id=c.id WHERE u.email='supplier@example.com'").fetchone()
    supplier_id = int(supplier["id"])
    # Three independent buyer companies are required before demand can surface.
    existing_buyer = conn.execute("SELECT c.id,u.id AS user_id FROM companies c JOIN users u ON u.company_id=c.id WHERE u.email='buyer@example.com'").fetchone()
    buyer_companies = [(int(existing_buyer["id"]), int(existing_buyer["user_id"]))]
    buyer_companies.append(make_company(conn, "Buyer Two", "buyer2@example.com", "buyer", "buyer"))
    buyer_companies.append(make_company(conn, "Buyer Three", "buyer3@example.com", "buyer", "buyer"))

    # Three independent suppliers provide five quotes for the same part.
    supplier_companies = [supplier_id]
    for i in range(2):
        cid, uid = make_company(conn, f"Quote Supplier {i+2}", f"quote{i+2}@example.com", "supplier", "supplier")
        subscription_for(conn, cid)
        conn.execute("UPDATE billing_subscriptions SET plan_key='supplier_pro',status='active',trial_ends_at=NULL,updated_at=? WHERE company_id=?", (now_iso(), cid))
        supplier_companies.append(cid)

    target = "GAP-900"
    # Searches + RFQs from at least 3 buyer companies.
    for idx in range(5):
        cid, _ = buyer_companies[idx % 3]
        record_demand_event(conn, "search", company_id=cid, query=target, result_count=0)

    prices = [420.0, 450.0, 475.0, 500.0, 530.0]
    for idx, price in enumerate(prices):
        buyer_cid, buyer_uid = buyer_companies[idx % 3]
        created = now_iso()
        rfq_id = insert_id(conn, "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,status,created_at) VALUES(?,?,?,?,?,?)",
                           (buyer_uid, target, normalize(target), 1, "awarded" if idx == 0 else "open", created))
        record_demand_event(conn, "rfq", company_id=buyer_cid, query=target, quantity=1, source_id=rfq_id, created_at=created)
        status = "accepted" if idx == 0 else "quoted"
        conn.execute("INSERT INTO rfq_recipients(rfq_id,supplier_company_id,status,quoted_price,quoted_currency,responded_at,accepted_at) VALUES(?,?,?,?,?,?,?)",
                     (rfq_id, supplier_companies[idx % 3], status, price, "GBP", created, created if idx == 0 else None))

    # Sparse competitor data must remain private.
    private_rfq = insert_id(conn, "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,status,created_at) VALUES(?,?,?,?,?,?)",
                            (buyer_companies[0][1], "PRIVATE-1", normalize("PRIVATE-1"), 1, "open", now_iso()))
    conn.execute("INSERT INTO rfq_recipients(rfq_id,supplier_company_id,status,quoted_price,quoted_currency,responded_at) VALUES(?,?,?,?,?,?)",
                 (private_rfq, supplier_companies[0], "quoted", 999.0, "GBP", now_iso()))

    intel = pricing_intelligence(conn, target, 90)
    assert intel["available"] and intel["currencies"][0]["median"] == 475.0, intel
    assert not pricing_intelligence(conn, "PRIVATE-1", 90)["available"]
    gaps = supplier_opportunities(conn, supplier_id, 90)
    assert any(x["normalized_query"] == normalize(target) and x["supplier_count"] == 0 for x in gaps), gaps

# Supplier UI: network intelligence renders privacy-safe benchmark.
supplier_client = TestClient(app.app)
with supplier_client:
    login(supplier_client, "supplier@example.com", "Supplier123!")
    page = supplier_client.get("/market-intelligence", params={"q": target, "days": 90})
    assert page.status_code == 200
    assert "475.00" in page.text and "GAP-900" in page.text and "Stock you may be missing" in page.text

    # Create a scoped API key; secret is displayed exactly once.
    token = csrf(supplier_client, "/integrations/api")
    create = supplier_client.post("/integrations/api/keys", data={
        "csrf_token": token, "name": "Smoke ERP",
        "scopes": ["inventory:read", "catalog:read", "intelligence:read"],
    }, follow_redirects=False)
    assert create.status_code == 303, create.text
    key_page = supplier_client.get("/integrations/api")
    m = re.search(r'(cxk_[A-Za-z0-9_\-]+)', key_page.text)
    assert m, key_page.text[:1000]
    raw_key = m.group(1)
    again = supplier_client.get("/integrations/api")
    assert raw_key not in again.text  # one-time display only

with db() as conn:
    stored = conn.execute("SELECT * FROM api_keys WHERE company_id=? AND active=1", (supplier_id,)).fetchone()
    assert stored and raw_key not in stored["key_hash"] and stored["key_prefix"] == raw_key[:12]
    api_key_id = stored["id"]

    # Add a second live supplier's inventory so the ERP API can find network stock.
    api_supplier, _ = make_company(conn, "API Stock Ltd", "apistock@example.com", "supplier", "supplier")
    subscription_for(conn, api_supplier)
    conn.execute("UPDATE billing_subscriptions SET plan_key='supplier_pro',status='active',trial_ends_at=NULL,updated_at=? WHERE company_id=?", (now_iso(), api_supplier))
    insert_id(conn, "INSERT INTO inventory(company_id,brand,part_number,normalized_part,description,condition,quantity,location,active,created_at,last_confirmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (api_supplier, "Trend", "API-777", normalize("API-777"), "API test controller", "New old stock", 3, "Leeds, UK", 1, now_iso(), now_iso(), now_iso()))

headers = {"Authorization": f"Bearer {raw_key}"}
external = TestClient(app.app)
with external:
    inv = external.get("/v1/inventory/search", params={"q": "API-777"}, headers=headers)
    assert inv.status_code == 200, inv.text
    assert inv.json()["data"][0]["part_number"] == "API-777"

    cat = external.get("/v1/catalog/resolve", params={"q": "IQ233"}, headers=headers)
    assert cat.status_code == 200 and cat.json()["data"], cat.text

    market = external.get("/v1/intelligence/part", params={"q": target, "days": 90}, headers=headers)
    assert market.status_code == 200, market.text
    assert market.json()["data"]["pricing"]["available"] is True
    assert market.json()["data"]["pricing"]["currencies"][0]["median"] == 475.0

with db() as conn:
    assert conn.execute("SELECT COUNT(*) AS n FROM api_usage_events WHERE api_key_id=?", (api_key_id,)).fetchone()["n"] >= 3

# Revocation is immediate.
with supplier_client:
    r = supplier_client.post(f"/integrations/api/keys/{api_key_id}/revoke", data={"csrf_token": csrf(supplier_client, "/integrations/api")}, follow_redirects=False)
    assert r.status_code == 303
with external:
    denied = external.get("/v1/catalog/resolve", params={"q": "IQ233"}, headers=headers)
    assert denied.status_code == 401

# Downgrade reconciliation cannot leave premium API access alive.
with db() as conn:
    conn.execute("UPDATE billing_subscriptions SET plan_key='supplier_pro',status='active',updated_at=? WHERE company_id=?", (now_iso(), supplier_id))
    from intelligence import create_api_key
    raw2, key2 = create_api_key(conn, supplier_id, int(supplier["user_id"]), "Downgrade key", {"inventory:read"})
    conn.execute("UPDATE billing_subscriptions SET plan_key='supplier_starter',updated_at=? WHERE company_id=?", (now_iso(), supplier_id))
    reconcile_plan_limits(conn, supplier_id, "supplier_starter")
    assert conn.execute("SELECT active FROM api_keys WHERE id=?", (key2,)).fetchone()["active"] == 0

print("PHASE6_SMOKE_TEST_OK")
