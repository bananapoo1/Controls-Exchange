#!/usr/bin/env python3
"""Self-contained Phase 5 monetisation smoke test using temporary SQLite.

Covers local trials, entitlements, promotion/analytics, manufacturer campaigns,
admin recovery, Stripe request construction/webhook verification, and reminders.
No real Stripe/network call is made.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="controls-exchange-phase5-")
os.environ["DATABASE_PATH"] = str(Path(TMP.name) / "phase5.db")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SEED_ADMIN"] = "true"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ["EMAIL_LOG_PATH"] = str(Path(TMP.name) / "emails.log")
os.environ["ENVIRONMENT"] = "development"
os.environ["FOUNDING_TRIAL_ENABLED"] = "true"
os.environ["FOUNDING_TRIAL_DAYS"] = "90"
os.environ["STANDARD_TRIAL_DAYS"] = "30"
os.environ["STRIPE_PRICE_SUPPLIER_PRO_MONTHLY"] = "price_pro_month_test"
os.environ["STRIPE_PRICE_SUPPLIER_STARTER_MONTHLY"] = "price_starter_month_test"
os.environ["STRIPE_TRIAL_END_BEHAVIOR"] = "pause"
os.environ["STRIPE_TEST_CLOCK_ID"] = "clock_phase5_test"

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402
import billing  # noqa: E402
from billing import check_limit, commercial_access, process_trial_reminders, subscription_for  # noqa: E402
from platform_core import db, hash_password, insert_id, now_iso  # noqa: E402


def csrf(client: TestClient, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, f"No CSRF token on {path}"
    return m.group(1)


def login(client: TestClient, email: str, password: str) -> None:
    r = client.post("/login", data={"csrf_token": csrf(client, "/login"), "email": email, "password": password, "next": "/dashboard"}, follow_redirects=False)
    assert r.status_code == 303, r.text


# Startup seeds Demo Controls Ltd and attaches the launch billing schema.
with TestClient(app.app) as boot:
    assert boot.get("/health/ready").status_code == 200

with db() as conn:
    supplier = conn.execute("SELECT c.*,u.id AS user_id FROM companies c JOIN users u ON u.company_id=c.id WHERE u.email='supplier@example.com'").fetchone()
    supplier_id = supplier["id"]
    sub = subscription_for(conn, supplier_id)
    assert sub["status"] == "trialing" and sub["founding_trial"] == 1 and sub["plan_key"] == "supplier_pro"
    start = datetime.fromisoformat(sub["trial_started_at"])
    end = datetime.fromisoformat(sub["trial_ends_at"])
    assert 89.5 <= (end - start).total_seconds() / 86400 <= 90.5
    assert commercial_access(conn, supplier_id)
    inv = conn.execute("SELECT id,part_number FROM inventory WHERE company_id=? ORDER BY id LIMIT 1", (supplier_id,)).fetchone()
    inventory_id, part_number = inv["id"], inv["part_number"]

# Billing page exposes founding trial and no-card local mode when Stripe is absent.
supplier_client = TestClient(app.app)
with supplier_client:
    login(supplier_client, "supplier@example.com", "Supplier123!")
    r = supplier_client.get("/billing")
    assert r.status_code == 200 and "90 days free" in r.text and "No card" in r.text

    # Promotion is a real plan entitlement.
    r = supplier_client.post(f"/promotions/{inventory_id}/toggle", data={"csrf_token": csrf(supplier_client, "/promotions")}, follow_redirects=False)
    assert r.status_code == 303
    with db() as conn:
        assert conn.execute("SELECT active FROM inventory_promotions WHERE inventory_id=?", (inventory_id,)).fetchone()["active"] == 1

# Buyer search sees stock, promotion flag and generates supplier analytics.
buyer = TestClient(app.app)
with buyer:
    login(buyer, "buyer@example.com", "Buyer123!")
    payload = buyer.get("/api/search", params={"q": part_number}).json()
    result = next(x for x in payload["results"] if x["id"] == inventory_id)
    assert bool(result["promoted"])
    supplier_profile = buyer.get(f"/directory/{supplier_id}")
    assert supplier_profile.status_code == 200

with supplier_client:
    r = supplier_client.get("/analytics?days=90")
    assert r.status_code == 200 and "Search appearances" in r.text

# Trial expiry hides supplier inventory everywhere but preserves account/data.
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
with db() as conn:
    conn.execute("UPDATE billing_subscriptions SET status='trialing',trial_ends_at=?,updated_at=? WHERE company_id=?", (yesterday, now_iso(), supplier_id))
    assert not commercial_access(conn, supplier_id)
    assert conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE company_id=?", (supplier_id,)).fetchone()["n"] > 0

with buyer:
    payload = buyer.get("/api/search", params={"q": part_number}).json()
    assert all(x["company_id"] != supplier_id for x in payload["results"])
    assert buyer.get(f"/directory/{supplier_id}").status_code == 404

# Existing supplier can still sign in, but commercial surfaces show paused state.
with supplier_client:
    r = supplier_client.get("/billing")
    assert r.status_code == 200 and "access is paused" in r.text.lower()

# Admin can deliberately grant/extend a strategic company's trial.
admin = TestClient(app.app)
with admin:
    login(admin, "admin@controlsexchange.local", "ChangeMe123!")
    r = admin.post(f"/admin/billing/{supplier_id}/trial", data={"csrf_token": csrf(admin, "/admin/billing"), "days": "90", "plan_key": "supplier_pro"}, follow_redirects=False)
    assert r.status_code == 303
with db() as conn:
    assert commercial_access(conn, supplier_id)

# Manufacturer account: verification starts the same 90-day launch clock, then campaigns surface in catalogue.
with db() as conn:
    manufacturer_id = insert_id(conn, "INSERT INTO companies(name,company_type,account_kind,location,verified,active,created_at) VALUES(?,?,?,?,?,?,?)",
                                ("Phase 5 Manufacturer", "supplier", "manufacturer", "London, UK", 0, 1, now_iso()))
    insert_id(conn, "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (manufacturer_id, "Manufacturer Owner", "maker@example.com", hash_password("Maker12345!"), "supplier", "owner", 1, now_iso(), 1, now_iso()))
with admin:
    r = admin.post(f"/admin/companies/{manufacturer_id}/verify", data={"csrf_token": csrf(admin, "/admin")}, follow_redirects=False)
    assert r.status_code == 303
with db() as conn:
    msub = subscription_for(conn, manufacturer_id)
    assert msub["status"] == "trialing" and msub["founding_trial"] == 1 and msub["plan_key"] == "manufacturer"

maker = TestClient(app.app)
with maker:
    login(maker, "maker@example.com", "Maker12345!")
    token = csrf(maker, "/manufacturer")
    r = maker.post("/manufacturer/profile", data={"csrf_token": token, "brand_name": "Phase5 Controls", "website": "https://example.com", "support_email": "maker@example.com", "description": "Official test manufacturer"}, follow_redirects=False)
    assert r.status_code == 303
    r = maker.post("/manufacturer/campaigns", data={"csrf_token": csrf(maker, "/manufacturer"), "title": "Legacy controller support", "body": "Official technical support for legacy controllers.", "cta_label": "Learn more", "cta_url": "https://example.com/support"}, follow_redirects=False)
    assert r.status_code == 303
with buyer:
    page = buyer.get("/catalog")
    assert page.status_code == 200 and "Legacy controller support" in page.text and "Phase5 Controls" in page.text

# Limits are enforced by the shared entitlement engine.
with db() as conn:
    # Temporarily lower test-only limit without changing production defaults.
    original = billing.PLANS["supplier_pro"]["promotion_limit"]
    billing.PLANS["supplier_pro"]["promotion_limit"] = 1
    try:
        ok, current, limit = check_limit(conn, supplier_id, "promotions", 1)
        assert not ok and current == 1 and limit == 1
    finally:
        billing.PLANS["supplier_pro"]["promotion_limit"] = original

# Stripe Checkout construction: preserve remaining founding trial when supplier subscribes early.
captured: list[tuple[str, object]] = []
old_secret, old_post = billing.STRIPE_SECRET_KEY, billing._stripe_post
billing.STRIPE_SECRET_KEY = "sk_test_not_sent"
def fake_stripe_post(path, data):
    captured.append((path, data))
    if path == "customers": return {"id": "cus_phase5"}
    if path == "checkout/sessions": return {"id": "cs_test", "url": "https://checkout.stripe.test/session"}
    raise AssertionError(path)
billing._stripe_post = fake_stripe_post
try:
    with db() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id=?", (supplier_id,)).fetchone()
        url = billing.create_checkout_session(conn, company, "supplier@example.com", "supplier_pro", "monthly")
        assert url.startswith("https://checkout.stripe.test/")
    customer_data = dict(captured[0][1])
    assert customer_data["test_clock"] == "clock_phase5_test"
    checkout_data = dict(captured[-1][1])
    assert checkout_data["line_items[0][price]"] == "price_pro_month_test"
    assert int(checkout_data["subscription_data[trial_end]"]) > int(time.time()) + 47 * 3600
    assert checkout_data["payment_method_collection"] == "if_required"
    assert checkout_data["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "pause"
finally:
    billing._stripe_post = old_post
    billing.STRIPE_SECRET_KEY = old_secret

# If less than Stripe's 48-hour minimum remains, Checkout must not charge early.
# The app deliberately grants Stripe a two-day trial and lets the webhook extend
# the local end date to the subscription's actual trial_end.
short_trial = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
with db() as conn:
    conn.execute("UPDATE billing_subscriptions SET status='trialing',trial_ends_at=?,updated_at=? WHERE company_id=?",
                 (short_trial, now_iso(), supplier_id))
captured = []
old_secret, old_post = billing.STRIPE_SECRET_KEY, billing._stripe_post
billing.STRIPE_SECRET_KEY = "sk_test_not_sent"
billing._stripe_post = fake_stripe_post
try:
    with db() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id=?", (supplier_id,)).fetchone()
        url = billing.create_checkout_session(conn, company, "supplier@example.com", "supplier_pro", "monthly")
        assert url.startswith("https://checkout.stripe.test/")
    short_checkout = dict(captured[-1][1])
    assert short_checkout["subscription_data[trial_period_days]"] == "2"
    assert "subscription_data[trial_end]" not in short_checkout
    assert short_checkout["payment_method_collection"] == "if_required"
finally:
    billing._stripe_post = old_post
    billing.STRIPE_SECRET_KEY = old_secret

# Signed Stripe webhook updates the subscription and is idempotent.
old_webhook = billing.STRIPE_WEBHOOK_SECRET
billing.STRIPE_WEBHOOK_SECRET = "whsec_phase5_test"
try:
    event = {"id": "evt_phase5_1", "type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_phase5", "customer": "cus_phase5", "status": "active", "trial_end": None,
        "current_period_end": int(time.time()) + 30 * 86400, "cancel_at_period_end": False,
        "metadata": {"company_id": str(supplier_id), "plan_key": "supplier_pro"},
        "items": {"data": [{"price": {"id": "price_pro_month_test"}}]},
    }}}
    raw = json.dumps(event, separators=(",", ":")).encode()
    ts = int(time.time())
    sig = hmac.new(billing.STRIPE_WEBHOOK_SECRET.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
    headers = {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}
    with TestClient(app.app) as webhook_client:
        assert webhook_client.post("/webhooks/stripe", content=raw, headers=headers).status_code == 200
        assert webhook_client.post("/webhooks/stripe", content=raw, headers=headers).status_code == 200
    with db() as conn:
        sub = subscription_for(conn, supplier_id)
        assert sub["status"] == "active" and sub["stripe_subscription_id"] == "sub_phase5"
        assert conn.execute("SELECT COUNT(*) AS n FROM billing_events WHERE event_id='evt_phase5_1'").fetchone()["n"] == 1
finally:
    billing.STRIPE_WEBHOOK_SECRET = old_webhook

# A Billing Portal price change is mapped from the server-configured Price ID, not stale metadata,
# and retroactive slot entitlements are reconciled safely on downgrade.
with db() as conn:
    # Re-enable one promotion to prove the Starter downgrade removes the paid promotion entitlement.
    conn.execute("UPDATE inventory_promotions SET active=1,ends_at=NULL WHERE company_id=?", (supplier_id,))
old_webhook = billing.STRIPE_WEBHOOK_SECRET
billing.STRIPE_WEBHOOK_SECRET = "whsec_phase5_test"
try:
    event = {"id": "evt_phase5_downgrade", "type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_phase5", "customer": "cus_phase5", "status": "active", "trial_end": None,
        "current_period_end": int(time.time()) + 30 * 86400, "cancel_at_period_end": False,
        # Metadata deliberately says Pro; actual configured Stripe Price says Starter.
        "metadata": {"company_id": str(supplier_id), "plan_key": "supplier_pro"},
        "items": {"data": [{"price": {"id": "price_starter_month_test"}}]},
    }}}
    raw = json.dumps(event, separators=(",", ":")).encode()
    ts = int(time.time())
    sig = hmac.new(billing.STRIPE_WEBHOOK_SECRET.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
    with TestClient(app.app) as webhook_client:
        assert webhook_client.post("/webhooks/stripe", content=raw, headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}).status_code == 200
    with db() as conn:
        sub = subscription_for(conn, supplier_id)
        assert sub["plan_key"] == "supplier_starter"
        assert conn.execute("SELECT COUNT(*) AS n FROM inventory_promotions WHERE company_id=? AND active=1", (supplier_id,)).fetchone()["n"] == 0
finally:
    billing.STRIPE_WEBHOOK_SECRET = old_webhook

# Trial reminder worker deduplicates notifications.
seven_days = (datetime.now(timezone.utc) + timedelta(days=6, hours=12)).isoformat(timespec="seconds")
with db() as conn:
    conn.execute("UPDATE billing_subscriptions SET status='trialing',trial_ends_at=?,updated_at=? WHERE company_id=?", (seven_days, now_iso(), supplier_id))
    conn.execute("DELETE FROM billing_notifications WHERE company_id=?", (supplier_id,))
first = process_trial_reminders(); second = process_trial_reminders()
assert first >= 1 and second == 0
with db() as conn:
    assert conn.execute("SELECT COUNT(*) AS n FROM billing_notifications WHERE company_id=? AND notification_key='trial_7d'", (supplier_id,)).fetchone()["n"] == 1

print("PHASE5_SMOKE_TEST_OK")
