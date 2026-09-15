from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

Path(os.environ["DATABASE_PATH"]).unlink(missing_ok=True)

from fastapi.testclient import TestClient

from app import app
from billing import init_billing_schema
from platform_core import db, hash_password, insert_id, normalize, now_iso

BUYER_EMAIL = "hardening-buyer@controlsexchange.test"
SUPPLIER_EMAIL = "hardening-supplier@controlsexchange.test"
PASSWORD = "Hardening-CI-Only-2026!"


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, f"CSRF token not found on {response.url}"
    return match.group(1)


def login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page),
            "next": "/dashboard",
            "email": SUPPLIER_EMAIL,
            "password": PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def seed() -> tuple[int, int, int]:
    now = now_iso()
    with db() as conn:
        buyer_company = insert_id(
            conn,
            "INSERT INTO companies(name,company_type,location,verified,active,created_at,account_kind) VALUES(?,?,?,?,?,?,?)",
            ("Hardening Buyer Ltd", "buyer", "London, UK", 1, 1, now, "trade"),
        )
        supplier_company = insert_id(
            conn,
            "INSERT INTO companies(name,company_type,location,verified,active,created_at,account_kind) VALUES(?,?,?,?,?,?,?)",
            ("Hardening Supplier Ltd", "supplier", "London, UK", 1, 1, now, "trade"),
        )
        buyer_user = insert_id(
            conn,
            "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (buyer_company, "Hardening Buyer", BUYER_EMAIL, hash_password(PASSWORD), "buyer", "owner", 1, now, 1, now),
        )
        insert_id(
            conn,
            "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (supplier_company, "Hardening Supplier", SUPPLIER_EMAIL, hash_password(PASSWORD), "supplier", "owner", 1, now, 1, now),
        )
    init_billing_schema()
    return buyer_user, buyer_company, supplier_company


def create_rfq(buyer_user: int, supplier_company: int, suffix: str) -> tuple[int, int]:
    with db() as conn:
        rfq_id = insert_id(
            conn,
            "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,required_by,delivery_location,notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                buyer_user,
                f"CX-HARDEN-{suffix}",
                normalize(f"CX-HARDEN-{suffix}"),
                1,
                "2026-09-30",
                "CI only",
                "Quote hardening test",
                "open",
                now_iso(),
            ),
        )
        recipient_id = insert_id(
            conn,
            "INSERT INTO rfq_recipients(rfq_id,supplier_company_id,status) VALUES(?,?,?)",
            (rfq_id, supplier_company, "sent"),
        )
    return rfq_id, recipient_id


def recipient_state(recipient_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT status,quoted_price,responded_at FROM rfq_recipients WHERE id=?",
            (recipient_id,),
        ).fetchone()


def submit_quote(client: TestClient, rfq_id: int, quoted_price: str):
    page = client.get(f"/rfqs/{rfq_id}")
    assert page.status_code == 200
    return client.post(
        f"/rfqs/{rfq_id}/respond",
        data={
            "csrf_token": csrf_from(page),
            "status": "quoted",
            "quoted_price": quoted_price,
            "quoted_currency": "GBP",
            "supplier_message": "Hardening QA quote",
        },
        follow_redirects=False,
    )


def assert_not_quoted(recipient_id: int) -> None:
    row = recipient_state(recipient_id)
    assert row["status"] in {"sent", "viewed"}, dict(row)
    assert row["quoted_price"] is None, dict(row)
    assert row["responded_at"] is None, dict(row)


def run() -> None:
    with TestClient(app) as startup_client:
        assert startup_client.get("/health/ready").status_code == 200
        buyer_user, _, supplier_company = seed()
        supplier = TestClient(app)
        login(supplier)

        # A commercial quote must have a positive, finite price.
        negative_rfq, negative_recipient = create_rfq(buyer_user, supplier_company, "NEG")
        response = submit_quote(supplier, negative_rfq, "-1.00")
        assert response.status_code == 303
        assert_not_quoted(negative_recipient)

        nan_rfq, nan_recipient = create_rfq(buyer_user, supplier_company, "NAN")
        response = submit_quote(supplier, nan_rfq, "nan")
        assert response.status_code == 303
        assert_not_quoted(nan_recipient)

        # If admin verification is removed after an RFQ was sent, supplier trading
        # actions must stop until the company is verified again.
        unverified_rfq, unverified_recipient = create_rfq(buyer_user, supplier_company, "UNVERIFIED")
        with db() as conn:
            conn.execute("UPDATE companies SET verified=0 WHERE id=?", (supplier_company,))
        response = submit_quote(supplier, unverified_rfq, "100.00")
        assert response.status_code == 403, response.status_code
        assert_not_quoted(unverified_recipient)

        # Commercial access being paused should likewise prevent a new quote on an
        # already-issued RFQ, matching the existing wanted-response access rule.
        with db() as conn:
            conn.execute("UPDATE companies SET verified=1 WHERE id=?", (supplier_company,))
            conn.execute(
                "UPDATE billing_subscriptions SET status='canceled',trial_ends_at=?,updated_at=? WHERE company_id=?",
                ("2026-01-01T00:00:00+00:00", now_iso(), supplier_company),
            )
        paused_rfq, paused_recipient = create_rfq(buyer_user, supplier_company, "PAUSED")
        response = submit_quote(supplier, paused_rfq, "100.00")
        assert response.status_code == 303
        assert response.headers.get("location") == "/billing", response.headers.get("location")
        assert_not_quoted(paused_recipient)

        print("PASS: invalid-price, company-verification and commercial-access quote guards")


if __name__ == "__main__":
    run()
