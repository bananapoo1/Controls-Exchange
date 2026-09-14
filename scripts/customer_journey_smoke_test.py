#!/usr/bin/env python3
"""Customer-journey regression test for first-time and returning users.

Covers the product flow rather than one feature phase:
- new buyer registration -> email verification -> wanted request -> search -> RFQ
- new supplier registration -> email verification -> pre-approval inventory/billing -> admin approval -> founding trial
- returning demo buyer/supplier login and dashboard navigation

Uses a temporary SQLite database and captures emails in memory.
"""
from __future__ import annotations

import os
import re
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_tmp = tempfile.NamedTemporaryFile(prefix="cx_customer_journey_", suffix=".db", delete=False)
_tmp.close()
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_PATH"] = _tmp.name
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
from platform_core import db  # noqa: E402

emails: list[tuple[str, str, str]] = []


def capture_email(to_email: str, subject: str, body: str, html: str | None = None) -> bool:
    emails.append((to_email, subject, body))
    return True


app.send_email = capture_email


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token missing from rendered page"
    return match.group(1)


def latest_verify_token(email: str) -> str:
    for to_email, _subject, body in reversed(emails):
        if to_email == email:
            match = re.search(r"/verify-email/([^\s]+)", body)
            if match:
                return match.group(1)
    raise AssertionError(f"No verification email captured for {email}")


def register(client: TestClient, kind: str, email: str, company: str, name: str) -> None:
    page = client.get(f"/register?account_type={kind}")
    response = client.post(
        "/register",
        data={
            "csrf_token": csrf(page.text),
            "account_type": kind,
            "company_name": company,
            "name": name,
            "location": "London, UK",
            "email": email,
            "password": "StrongPass123!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


def login(client: TestClient, email: str, password: str, next_url: str = "/dashboard") -> None:
    page = client.get(f"/login?next={next_url}")
    response = client.post(
        "/login",
        data={"csrf_token": csrf(page.text), "email": email, "password": password, "next": next_url},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == next_url


def main() -> int:
    try:
        # New buyer: useful first action should be obvious before and after verification.
        with TestClient(app.app) as buyer:
            register(buyer, "buyer", "newbuyer@test.local", "New Buyer Ltd", "Nina Buyer")
            dashboard = buyer.get("/dashboard")
            assert "Search now. Verify before you contact suppliers." in dashboard.text
            assert "Search live inventory" in dashboard.text

            token = latest_verify_token("newbuyer@test.local")
            verified = buyer.get(f"/verify-email/{token}", follow_redirects=False)
            assert verified.status_code == 303

            wanted = buyer.get("/wanted")
            assert "Ask the supplier network directly" in wanted.text
            created = buyer.post(
                "/wanted",
                data={"csrf_token": csrf(wanted.text), "query": "OLD-CTRL-99", "quantity": "2", "notes": "Any tested unit"},
                follow_redirects=False,
            )
            assert created.status_code == 303
            assert "OLD-CTRL-99" in buyer.get("/wanted").text

            search = buyer.get("/api/search?q=IQ233")
            assert search.status_code == 200 and search.json()["count"] >= 1
            inventory_id = search.json()["results"][0]["id"]
            home = buyer.get("/")
            rfq = buyer.post(
                "/rfqs",
                data={
                    "csrf_token": csrf(home.text),
                    "inventory_ids": str(inventory_id),
                    "part_number": "IQ233",
                    "quantity": "1",
                    "required_by": "",
                    "delivery_location": "London",
                    "notes": "Customer-journey test",
                },
                follow_redirects=False,
            )
            assert rfq.status_code == 303 and rfq.headers["location"].startswith("/rfqs/")

        # New supplier: do not imply payment is due while approval is pending.
        with TestClient(app.app) as supplier:
            register(supplier, "supplier", "newsupplier@test.local", "New Supplier Ltd", "Nora Supplier")
            dashboard = supplier.get("/dashboard")
            assert "Trial starts after approval" in dashboard.text
            assert "Commercial access paused" not in dashboard.text
            assert "Stock stays private until company approval." in dashboard.text

            supplier.get(f"/verify-email/{latest_verify_token('newsupplier@test.local')}")
            billing = supplier.get("/billing")
            assert "Your founding trial starts after approval" in billing.text
            assert "No payment is required while company verification is pending." in billing.text
            assert "Available after approval" in billing.text

            inventory = supplier.get("/inventory")
            assert inventory.status_code == 200
            assert "inventory remains hidden pending company verification" in inventory.text

            with db() as conn:
                company_id = conn.execute("SELECT id FROM companies WHERE name='New Supplier Ltd'").fetchone()["id"]

            with TestClient(app.app) as admin:
                login(admin, "admin@controlsexchange.local", "ChangeMe123!", "/admin")
                admin_page = admin.get("/admin")
                approved = admin.post(
                    f"/admin/companies/{company_id}/verify",
                    data={"csrf_token": csrf(admin_page.text)},
                    follow_redirects=False,
                )
                assert approved.status_code == 303

            approved_dashboard = supplier.get("/dashboard")
            assert "Verified supplier" in approved_dashboard.text
            assert "days remain in your founding trial" in approved_dashboard.text

        # Returning users: first task should still be one click away.
        with TestClient(app.app) as returning_buyer:
            login(returning_buyer, "buyer@example.com", "Buyer123!")
            dashboard = returning_buyer.get("/dashboard")
            assert "Search inventory" in dashboard.text and "My RFQs" in dashboard.text
            assert ">Plan<" not in dashboard.text

        with TestClient(app.app) as returning_supplier:
            login(returning_supplier, "supplier@example.com", "Supplier123!")
            dashboard = returning_supplier.get("/dashboard")
            assert "Manage inventory" in dashboard.text and "RFQ inbox" in dashboard.text
            assert "Analytics</a>" not in dashboard.text.split('dashboard-actions', 1)[-1].split('</div>', 1)[0]

        print("CUSTOMER_JOURNEY_SMOKE_TEST_OK")
        return 0
    finally:
        try:
            Path(_tmp.name).unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
