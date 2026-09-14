#!/usr/bin/env python3
"""Seed synthetic Phase 6 intelligence so the dashboards can be viewed locally.

Development/staging only. Refuses ENVIRONMENT=production. The generated companies,
RFQs and quotes are clearly named as demo data and must never be used as real market evidence.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.getenv("ENVIRONMENT", "development").lower() == "production":
    raise SystemExit("Refusing to seed synthetic intelligence in production.")

from billing import init_billing_schema, subscription_for  # noqa: E402
from intelligence import init_intelligence_schema, record_demand_event  # noqa: E402
from platform_core import db, hash_password, init_db, insert_id, normalize, now_iso  # noqa: E402


def company_user(conn, name: str, email: str, role: str) -> tuple[int, int]:
    row = conn.execute("SELECT c.id,u.id AS user_id FROM companies c JOIN users u ON u.company_id=c.id WHERE lower(u.email)=lower(?)", (email,)).fetchone()
    if row:
        return int(row["id"]), int(row["user_id"])
    company_type = "buyer" if role == "buyer" else "supplier"
    account_kind = "buyer" if role == "buyer" else "supplier"
    cid = insert_id(conn, "INSERT INTO companies(name,company_type,account_kind,location,verified,active,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, company_type, account_kind, "Demo, UK", 1, 1, now_iso()))
    uid = insert_id(conn, "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, f"{name} User", email, hash_password("DemoOnly123!"), role, "owner", 1, now_iso(), 1, now_iso()))
    if role == "supplier":
        subscription_for(conn, cid)
        conn.execute("UPDATE billing_subscriptions SET plan_key='supplier_pro',status='active',trial_ends_at=NULL,updated_at=? WHERE company_id=?", (now_iso(), cid))
    return cid, uid


init_db(); init_billing_schema(); init_intelligence_schema()
with db() as conn:
    buyers = [company_user(conn, f"Demo Demand Buyer {i}", f"phase6-buyer{i}@example.invalid", "buyer") for i in range(1, 5)]
    suppliers = [company_user(conn, f"Demo Quote Supplier {i}", f"phase6-supplier{i}@example.invalid", "supplier") for i in range(1, 4)]

    scenarios = [
        ("Satchwell BAS2800", [280, 310, 325, 340, 365], 0),
        ("Trend IQ204", [190, 205, 215, 230, 245], 1),
        ("Honeywell XFL521B", [150, 165, 170, 182, 195], 2),
    ]
    for scenario_idx, (part, prices, live_supplier_count) in enumerate(scenarios):
        nq = normalize(part)
        # Safe to rerun: only seed when this scenario is not already present.
        if conn.execute("SELECT COUNT(*) AS n FROM market_demand_events WHERE display_query=?", (part,)).fetchone()["n"]:
            continue
        for i in range(8):
            cid, _ = buyers[i % len(buyers)]
            record_demand_event(conn, "search", company_id=cid, query=part, result_count=live_supplier_count)
        for i, price in enumerate(prices):
            buyer_cid, buyer_uid = buyers[i % len(buyers)]
            created = now_iso()
            rfq_id = insert_id(conn, "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,status,created_at) VALUES(?,?,?,?,?,?)",
                               (buyer_uid, part, nq, 1 + (i % 2), "awarded" if i == 0 else "open", created))
            record_demand_event(conn, "rfq", company_id=buyer_cid, query=part, quantity=1 + (i % 2), source_id=rfq_id, created_at=created)
            supplier_id = suppliers[i % len(suppliers)][0]
            status = "accepted" if i == 0 else "quoted"
            conn.execute("INSERT INTO rfq_recipients(rfq_id,supplier_company_id,status,quoted_price,quoted_currency,responded_at,accepted_at) VALUES(?,?,?,?,?,?,?)",
                         (rfq_id, supplier_id, status, float(price), "GBP", created, created if i == 0 else None))
        # Add limited live supply for two scenarios, leaving BAS2800 as a clear gap.
        for supplier_idx in range(live_supplier_count):
            cid = suppliers[supplier_idx][0]
            if not conn.execute("SELECT id FROM inventory WHERE company_id=? AND normalized_part=? AND deleted_at IS NULL", (cid, nq)).fetchone():
                insert_id(conn, "INSERT INTO inventory(company_id,brand,part_number,normalized_part,description,condition,quantity,location,active,created_at,last_confirmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, part.split()[0], part, nq, "Synthetic Phase 6 demo stock", "New old stock", 2 + supplier_idx, "Demo, UK", 1, now_iso(), now_iso(), now_iso()))

print("Seeded synthetic Phase 6 market-intelligence data. Development/staging only.")
print("Sign in as supplier@example.com / Supplier123! and open /market-intelligence")
