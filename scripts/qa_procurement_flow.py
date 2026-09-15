from __future__ import annotations

import os
import re
from pathlib import Path

# This script is intentionally isolated from staging/production. The workflow points
# DATABASE_PATH at /tmp before importing the application.
db_path = Path(os.environ["DATABASE_PATH"])
db_path.unlink(missing_ok=True)

from fastapi.testclient import TestClient

from app import app
from billing import init_billing_schema
from platform_core import db, hash_password, insert_id, normalize, now_iso


BUYER_EMAIL = "ci-buyer@controlsexchange.test"
SUPPLIER_EMAIL = "ci-supplier@controlsexchange.test"
BUYER_PASSWORD = "CI-Buyer-Only-2026!"
SUPPLIER_PASSWORD = "CI-Supplier-Only-2026!"
PART = "CX-CI-PROC-9917"


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, f"CSRF token not found on {response.url}"
    return match.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page),
            "next": "/dashboard",
            "email": email,
            "password": password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/dashboard"


def seed_flow() -> tuple[int, int]:
    now = now_iso()
    with db() as conn:
        buyer_company = insert_id(
            conn,
            "INSERT INTO companies(name,company_type,location,verified,active,created_at,account_kind) VALUES(?,?,?,?,?,?,?)",
            ("CI Buyer Ltd", "buyer", "London, UK", 1, 1, now, "trade"),
        )
        supplier_company = insert_id(
            conn,
            "INSERT INTO companies(name,company_type,location,verified,active,created_at,account_kind) VALUES(?,?,?,?,?,?,?)",
            ("CI Supplier Ltd", "supplier", "London, UK", 1, 1, now, "trade"),
        )
        buyer_user = insert_id(
            conn,
            "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (buyer_company, "CI Buyer", BUYER_EMAIL, hash_password(BUYER_PASSWORD), "buyer", "owner", 1, now, 1, now),
        )
        insert_id(
            conn,
            "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (supplier_company, "CI Supplier", SUPPLIER_EMAIL, hash_password(SUPPLIER_PASSWORD), "supplier", "owner", 1, now, 1, now),
        )

    # Re-run the idempotent billing initializer after seeding companies so the buyer
    # receives its free row and the verified supplier receives its founding trial.
    init_billing_schema()

    with db() as conn:
        supplier_sub = conn.execute(
            "SELECT status,trial_started_at,trial_ends_at FROM billing_subscriptions WHERE company_id=?",
            (supplier_company,),
        ).fetchone()
        assert supplier_sub and supplier_sub["status"] == "trialing"
        assert supplier_sub["trial_started_at"] and supplier_sub["trial_ends_at"]

        rfq_id = insert_id(
            conn,
            "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,required_by,delivery_location,notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                buyer_user,
                PART,
                normalize(PART),
                1,
                "2026-09-30",
                "CI London Site",
                "Disposable CI procurement test - do not fulfil",
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


def run() -> None:
    with TestClient(app) as startup_client:
        # Keeping this client alive ensures the FastAPI startup lifecycle has completed.
        assert startup_client.get("/health/ready").status_code == 200
        rfq_id, recipient_id = seed_flow()

        supplier = TestClient(app)
        login(supplier, SUPPLIER_EMAIL, SUPPLIER_PASSWORD)
        rfq_page = supplier.get(f"/rfqs/{rfq_id}")
        assert rfq_page.status_code == 200
        supplier_csrf = csrf_from(rfq_page)

        quote = supplier.post(
            f"/rfqs/{rfq_id}/respond",
            data={
                "csrf_token": supplier_csrf,
                "status": "quoted",
                "quoted_price": "123.45",
                "quoted_currency": "GBP",
                "supplier_message": "Disposable CI quote - do not fulfil",
            },
            follow_redirects=False,
        )
        assert quote.status_code == 303, quote.text

        supplier_message = supplier.post(
            f"/rfqs/{rfq_id}/messages",
            data={
                "csrf_token": supplier_csrf,
                "recipient_id": str(recipient_id),
                "body": "CI supplier private RFQ message",
            },
            follow_redirects=False,
        )
        assert supplier_message.status_code == 303, supplier_message.text

        with db() as conn:
            recipient = conn.execute("SELECT * FROM rfq_recipients WHERE id=?", (recipient_id,)).fetchone()
            assert recipient["status"] == "quoted"
            assert abs(float(recipient["quoted_price"]) - 123.45) < 0.001
            assert recipient["responded_at"]
            assert conn.execute("SELECT COUNT(*) AS n FROM rfq_messages WHERE recipient_id=?", (recipient_id,)).fetchone()["n"] == 1

        buyer = TestClient(app)
        login(buyer, BUYER_EMAIL, BUYER_PASSWORD)
        buyer_rfq = buyer.get(f"/rfqs/{rfq_id}?recipient={recipient_id}")
        assert buyer_rfq.status_code == 200
        buyer_csrf = csrf_from(buyer_rfq)
        assert "123.45" in buyer_rfq.text

        accept = buyer.post(
            f"/rfqs/{rfq_id}/accept/{recipient_id}",
            data={"csrf_token": buyer_csrf},
            follow_redirects=False,
        )
        assert accept.status_code == 303, accept.text
        assert accept.headers["location"].startswith("/orders/")
        order_id = int(accept.headers["location"].split("/")[-1])

        with db() as conn:
            rfq = conn.execute("SELECT status FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            assert rfq["status"] == "awarded"
            assert order["status"] == "awaiting_buyer_po"
            assert abs(float(order["quoted_price"]) - 123.45) < 0.001
            assert order["quoted_currency"] == "GBP"

        order_page = buyer.get(f"/orders/{order_id}")
        buyer_order_csrf = csrf_from(order_page)
        submit_po = buyer.post(
            f"/orders/{order_id}/buyer-details",
            data={
                "csrf_token": buyer_order_csrf,
                "buyer_po_number": "CI-PO-9917",
                "buyer_reference": "CI-REF-9917",
                "delivery_address": "CI London Site - NOT REAL",
                "delivery_contact": "CI Test Contact",
            },
            follow_redirects=False,
        )
        assert submit_po.status_code == 303, submit_po.text

        supplier_order_page = supplier.get(f"/orders/{order_id}")
        supplier_order_csrf = csrf_from(supplier_order_page)
        status_payloads = [
            {
                "status": "acknowledged",
                "supplier_order_reference": "CI-SUP-9917",
                "expected_dispatch_date": "2026-09-20",
            },
            {"status": "processing"},
            {
                "status": "dispatched",
                "carrier": "CI Test Carrier",
                "tracking_number": "CI-TRACK-9917",
            },
            {"status": "delivered"},
        ]
        for payload in status_payloads:
            response = supplier.post(
                f"/orders/{order_id}/supplier-status",
                data={"csrf_token": supplier_order_csrf, **payload},
                follow_redirects=False,
            )
            assert response.status_code == 303, (payload, response.text)

        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            assert order["status"] == "delivered"
            assert order["buyer_po_number"] == "CI-PO-9917"
            assert order["supplier_order_reference"] == "CI-SUP-9917"
            assert order["carrier"] == "CI Test Carrier"
            assert order["tracking_number"] == "CI-TRACK-9917"
            assert order["acknowledged_at"]
            assert order["dispatched_at"]
            assert order["delivered_at"]
            events = {
                row["event_type"]
                for row in conn.execute("SELECT event_type FROM order_events WHERE order_id=?", (order_id,)).fetchall()
            }
            required = {
                "order.created",
                "order.submitted",
                "order.acknowledged",
                "order.processing",
                "order.dispatched",
                "order.delivered",
            }
            assert required.issubset(events), (required - events, events)

        print(
            "PASS: quote -> private message -> award -> PO -> acknowledged -> processing -> dispatched -> delivered",
            f"rfq_id={rfq_id}",
            f"order_id={order_id}",
        )


if __name__ == "__main__":
    run()
