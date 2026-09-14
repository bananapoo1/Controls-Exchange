from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from platform_core import IS_POSTGRES, PUBLIC_BASE_URL, db, insert_id, now_iso, parse_iso, scalar, send_email, logger, _ensure_column

FOUNDING_TRIAL_DAYS = int(os.getenv("FOUNDING_TRIAL_DAYS", "90"))
STANDARD_TRIAL_DAYS = int(os.getenv("STANDARD_TRIAL_DAYS", "30"))
FOUNDING_TRIAL_ENABLED = os.getenv("FOUNDING_TRIAL_ENABLED", "true").lower() == "true"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PORTAL_RETURN_URL = os.getenv("STRIPE_PORTAL_RETURN_URL", f"{PUBLIC_BASE_URL}/billing").strip()
STRIPE_TRIAL_END_BEHAVIOR = os.getenv("STRIPE_TRIAL_END_BEHAVIOR", "pause").strip().lower()
STRIPE_TEST_CLOCK_ID = os.getenv("STRIPE_TEST_CLOCK_ID", "").strip()
if STRIPE_TRIAL_END_BEHAVIOR not in {"pause", "cancel", "create_invoice"}:
    STRIPE_TRIAL_END_BEHAVIOR = "pause"

PLANS: dict[str, dict[str, Any]] = {
    "buyer_free": {
        "name": "Buyer", "audience": "buyer", "monthly_gbp": 0, "annual_gbp": 0,
        "inventory_limit": 0, "user_limit": 25, "feed_limit": 0, "promotion_limit": 0,
        "campaign_limit": 0, "analytics_days": 0, "api_key_limit": 1, "api_rate_per_hour": 500, "network_intelligence": False,
        "description": "Search, RFQs, wanted requests, saved alerts and one private-beta order API key remain free for buyers.",
    },
    "supplier_starter": {
        "name": "Supplier Starter", "audience": "supplier", "monthly_gbp": 59, "annual_gbp": 590,
        "inventory_limit": 5000, "user_limit": 3, "feed_limit": 1, "promotion_limit": 0,
        "campaign_limit": 0, "analytics_days": 30, "api_key_limit": 0, "api_rate_per_hour": 0, "network_intelligence": False,
        "description": "For smaller specialist stockists getting their inventory onto the network.",
    },
    "supplier_pro": {
        "name": "Supplier Pro", "audience": "supplier", "monthly_gbp": 119, "annual_gbp": 1190,
        "inventory_limit": 50000, "user_limit": 10, "feed_limit": 10, "promotion_limit": 5,
        "campaign_limit": 0, "analytics_days": 90, "api_key_limit": 2, "api_rate_per_hour": 1000, "network_intelligence": True,
        "description": "The default plan for established BMS distributors and integrators.",
    },
    "supplier_premium": {
        "name": "Supplier Premium", "audience": "supplier", "monthly_gbp": 229, "annual_gbp": 2290,
        "inventory_limit": 250000, "user_limit": 25, "feed_limit": 50, "promotion_limit": 20,
        "campaign_limit": 0, "analytics_days": 365, "api_key_limit": 5, "api_rate_per_hour": 5000, "network_intelligence": True,
        "description": "For larger distributors with multiple feeds, teams and promoted stock.",
    },
    "manufacturer": {
        "name": "Manufacturer", "audience": "manufacturer", "monthly_gbp": 199, "annual_gbp": 1990,
        "inventory_limit": 100000, "user_limit": 20, "feed_limit": 10, "promotion_limit": 20,
        "campaign_limit": 5, "analytics_days": 365, "api_key_limit": 5, "api_rate_per_hour": 5000, "network_intelligence": True,
        "description": "Brand presence, promoted inventory, campaigns and market analytics.",
    },
}


def plan_price_env(plan_key: str, interval: str) -> str:
    key = f"STRIPE_PRICE_{plan_key.upper()}_{interval.upper()}"
    return os.getenv(key, "").strip()


def plan_key_for_price_id(price_id: str | None) -> str | None:
    """Resolve a Stripe Price back to an application plan.

    This is intentionally based on server-side environment configuration rather than
    trusting mutable Stripe metadata, so Billing Portal plan changes stay in sync.
    """
    price_id = (price_id or "").strip()
    if not price_id:
        return None
    for plan_key in PLANS:
        if plan_key == "buyer_free":
            continue
        for interval in ("monthly", "annual"):
            if plan_price_env(plan_key, interval) == price_id:
                return plan_key
    return None


def _plan_allowed_for_company(company: Any, plan_key: str) -> bool:
    if not company or plan_key not in PLANS or plan_key == "buyer_free":
        return False
    if company["account_kind"] == "manufacturer":
        return plan_key == "manufacturer"
    return PLANS[plan_key]["audience"] == "supplier"


def reconcile_plan_limits(conn, company_id: int, plan_key: str) -> None:
    """Apply safe retroactive limits after a Stripe plan change.

    We never delete inventory or deactivate team members on downgrade. New additions
    remain blocked by check_limit. Features that are purely entitlement slots can be
    safely reduced automatically: promotions, campaigns, and automated feeds.
    """
    plan = PLANS.get(plan_key, PLANS["buyer_free"])
    now = now_iso()

    promotion_limit = int(plan.get("promotion_limit", 0) or 0)
    promos = conn.execute("SELECT id FROM inventory_promotions WHERE company_id=? AND active=1 ORDER BY starts_at ASC,id ASC", (company_id,)).fetchall()
    for row in promos[promotion_limit:]:
        conn.execute("UPDATE inventory_promotions SET active=0,ends_at=? WHERE id=?", (now, row["id"]))

    campaign_limit = int(plan.get("campaign_limit", 0) or 0)
    campaigns = conn.execute("SELECT id FROM manufacturer_campaigns WHERE company_id=? AND active=1 ORDER BY starts_at ASC,id ASC", (company_id,)).fetchall()
    for row in campaigns[campaign_limit:]:
        conn.execute("UPDATE manufacturer_campaigns SET active=0,updated_at=? WHERE id=?", (now, row["id"]))

    feed_limit = int(plan.get("feed_limit", 0) or 0)
    feeds = conn.execute("SELECT id FROM inventory_feeds WHERE company_id=? AND enabled=1 ORDER BY created_at ASC,id ASC", (company_id,)).fetchall()
    for row in feeds[feed_limit:]:
        conn.execute("UPDATE inventory_feeds SET enabled=0,updated_at=? WHERE id=?", (now, row["id"]))

    # Phase 6 API keys are entitlement slots too. If the table exists, excess active keys
    # are revoked on downgrade; historical usage remains for audit/reporting.
    try:
        api_limit = int(plan.get("api_key_limit", 0) or 0)
        keys = conn.execute("SELECT id FROM api_keys WHERE company_id=? AND active=1 ORDER BY created_at ASC,id ASC", (company_id,)).fetchall()
        for row in keys[api_limit:]:
            conn.execute("UPDATE api_keys SET active=0,revoked_at=? WHERE id=?", (now, row["id"]))
    except Exception:
        # During first startup billing schema is initialized before the Phase 6 schema.
        pass


def init_billing_schema() -> None:
    with db() as conn:
        if IS_POSTGRES:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS billing_subscriptions (
                id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
                plan_key TEXT NOT NULL DEFAULT 'buyer_free', status TEXT NOT NULL DEFAULT 'none',
                founding_trial INTEGER NOT NULL DEFAULT 0, trial_started_at TEXT, trial_ends_at TEXT,
                current_period_end TEXT, cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT, stripe_subscription_id TEXT, stripe_price_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_events (
                id BIGSERIAL PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'stripe', event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL, payload_hash TEXT NOT NULL DEFAULT '', processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_notifications (
                id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                notification_key TEXT NOT NULL, sent_at TEXT NOT NULL, UNIQUE(company_id, notification_key)
            );
            CREATE TABLE IF NOT EXISTS inventory_promotions (
                id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                inventory_id BIGINT NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
                created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                active INTEGER NOT NULL DEFAULT 1, starts_at TEXT NOT NULL, ends_at TEXT,
                created_at TEXT NOT NULL, UNIQUE(company_id, inventory_id)
            );
            CREATE TABLE IF NOT EXISTS manufacturer_profiles (
                id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
                brand_name TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                support_email TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS manufacturer_campaigns (
                id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', cta_label TEXT NOT NULL DEFAULT '', cta_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1, starts_at TEXT NOT NULL, ends_at TEXT,
                created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS marketplace_events (
                id BIGSERIAL PRIMARY KEY, event_type TEXT NOT NULL, user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                user_company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL, supplier_company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
                inventory_id BIGINT REFERENCES inventory(id) ON DELETE SET NULL, query TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_events_supplier_created ON marketplace_events(supplier_company_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_promotions_inventory_active ON inventory_promotions(inventory_id, active);
            """)
        else:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS billing_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
                plan_key TEXT NOT NULL DEFAULT 'buyer_free', status TEXT NOT NULL DEFAULT 'none',
                founding_trial INTEGER NOT NULL DEFAULT 0, trial_started_at TEXT, trial_ends_at TEXT,
                current_period_end TEXT, cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT, stripe_subscription_id TEXT, stripe_price_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL DEFAULT 'stripe', event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL, payload_hash TEXT NOT NULL DEFAULT '', processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                notification_key TEXT NOT NULL, sent_at TEXT NOT NULL, UNIQUE(company_id, notification_key)
            );
            CREATE TABLE IF NOT EXISTS inventory_promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                active INTEGER NOT NULL DEFAULT 1, starts_at TEXT NOT NULL, ends_at TEXT,
                created_at TEXT NOT NULL, UNIQUE(company_id, inventory_id)
            );
            CREATE TABLE IF NOT EXISTS manufacturer_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
                brand_name TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                support_email TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS manufacturer_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', cta_label TEXT NOT NULL DEFAULT '', cta_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1, starts_at TEXT NOT NULL, ends_at TEXT,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS marketplace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                user_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL, supplier_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                inventory_id INTEGER REFERENCES inventory(id) ON DELETE SET NULL, query TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_marketplace_events_supplier_created ON marketplace_events(supplier_company_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_promotions_inventory_active ON inventory_promotions(inventory_id, active);
            """)
        _ensure_column(conn, "companies", "account_kind TEXT NOT NULL DEFAULT 'trade'")
        # Seed billing rows for every company; verified suppliers get launch trial, buyers stay free.
        companies = conn.execute("SELECT id,company_type,verified,account_kind FROM companies").fetchall()
        for c in companies:
            if scalar(conn, "SELECT COUNT(*) FROM billing_subscriptions WHERE company_id=?", (c["id"],)):
                continue
            if c["company_type"] == "supplier":
                if c["verified"]:
                    _create_trial_row(conn, c["id"], c["account_kind"] == "manufacturer")
                else:
                    plan = "manufacturer" if c["account_kind"] == "manufacturer" else "supplier_pro"
                    conn.execute("INSERT INTO billing_subscriptions(company_id,plan_key,status,founding_trial,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                 (c["id"], plan, "pending_verification", 1 if FOUNDING_TRIAL_ENABLED else 0, now_iso(), now_iso()))
            else:
                conn.execute("INSERT INTO billing_subscriptions(company_id,plan_key,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (c["id"], "buyer_free", "active", now_iso(), now_iso()))


def _create_trial_row(conn, company_id: int, manufacturer: bool = False) -> None:
    days = FOUNDING_TRIAL_DAYS if FOUNDING_TRIAL_ENABLED else STANDARD_TRIAL_DAYS
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days)
    plan = "manufacturer" if manufacturer else "supplier_pro"
    founding = 1 if FOUNDING_TRIAL_ENABLED else 0
    existing = conn.execute("SELECT id,status,trial_started_at FROM billing_subscriptions WHERE company_id=?", (company_id,)).fetchone()
    if existing and existing["trial_started_at"]:
        return
    if existing:
        conn.execute("UPDATE billing_subscriptions SET plan_key=?,status='trialing',founding_trial=?,trial_started_at=?,trial_ends_at=?,updated_at=? WHERE company_id=?",
                     (plan, founding, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), now_iso(), company_id))
    else:
        conn.execute("INSERT INTO billing_subscriptions(company_id,plan_key,status,founding_trial,trial_started_at,trial_ends_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (company_id, plan, "trialing", founding, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), now_iso(), now_iso()))


def start_trial_if_needed(company_id: int) -> dict[str, Any] | None:
    with db() as conn:
        c = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if not c or c["company_type"] != "supplier":
            return None
        _create_trial_row(conn, company_id, c["account_kind"] == "manufacturer")
        row = conn.execute("SELECT * FROM billing_subscriptions WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None


def subscription_for(conn, company_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM billing_subscriptions WHERE company_id=?", (company_id,)).fetchone()
    if not row:
        company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if company and company["company_type"] == "supplier":
            _create_trial_row(conn, company_id, company["account_kind"] == "manufacturer")
        else:
            conn.execute("INSERT INTO billing_subscriptions(company_id,plan_key,status,created_at,updated_at) VALUES(?,?,?,?,?)", (company_id, "buyer_free", "active", now_iso(), now_iso()))
        row = conn.execute("SELECT * FROM billing_subscriptions WHERE company_id=?", (company_id,)).fetchone()
    data = dict(row)
    data["access_active"] = subscription_access_active(data)
    data["plan"] = PLANS.get(data.get("plan_key"), PLANS["buyer_free"])
    return data


def subscription_access_active(sub: dict[str, Any] | Any) -> bool:
    plan_key = sub.get("plan_key") if isinstance(sub, dict) else sub["plan_key"]
    status = sub.get("status") if isinstance(sub, dict) else sub["status"]
    if plan_key == "buyer_free":
        return True
    if status in {"active", "past_due"}:  # allow grace during Stripe retries
        return True
    if status == "trialing":
        end = parse_iso(sub.get("trial_ends_at") if isinstance(sub, dict) else sub["trial_ends_at"])
        return bool(end and end > datetime.now(timezone.utc))
    return False


def effective_plan(conn, company_id: int) -> dict[str, Any]:
    sub = subscription_for(conn, company_id)
    plan = dict(PLANS.get(sub["plan_key"], PLANS["buyer_free"]))
    plan.update({"key": sub["plan_key"], "subscription": sub})
    return plan



def catalogue_management_allowed(conn, company_id: int) -> bool:
    sub = subscription_for(conn, company_id)
    return bool(sub["access_active"] or sub.get("status") == "pending_verification")

def commercial_access(conn, company_id: int) -> bool:
    return subscription_for(conn, company_id)["access_active"]


def record_marketplace_event(conn, event_type: str, *, user_id: int | None = None, user_company_id: int | None = None,
                             supplier_company_id: int | None = None, inventory_id: int | None = None,
                             query: str = "", metadata: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO marketplace_events(event_type,user_id,user_company_id,supplier_company_id,inventory_id,query,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (event_type, user_id, user_company_id, supplier_company_id, inventory_id, query, json.dumps(metadata or {}, separators=(",", ":")), now_iso()))


def count_active_promotions(conn, company_id: int) -> int:
    return int(scalar(conn, "SELECT COUNT(*) FROM inventory_promotions WHERE company_id=? AND active=1 AND (ends_at IS NULL OR ends_at>?)", (company_id, now_iso())) or 0)


def _active_api_key_count(conn, company_id: int) -> int:
    try:
        return int(scalar(conn, "SELECT COUNT(*) FROM api_keys WHERE company_id=? AND active=1", (company_id,)) or 0)
    except Exception:
        return 0


def plan_usage(conn, company_id: int) -> dict[str, int]:
    return {
        "inventory": int(scalar(conn, "SELECT COUNT(*) FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL", (company_id,)) or 0),
        "users": int(scalar(conn, "SELECT COUNT(*) FROM users WHERE company_id=? AND active=1", (company_id,)) or 0),
        "feeds": int(scalar(conn, "SELECT COUNT(*) FROM inventory_feeds WHERE company_id=? AND enabled=1", (company_id,)) or 0),
        "promotions": count_active_promotions(conn, company_id),
        "campaigns": int(scalar(conn, "SELECT COUNT(*) FROM manufacturer_campaigns WHERE company_id=? AND active=1", (company_id,)) or 0),
        "api_keys": _active_api_key_count(conn, company_id),
    }


def check_limit(conn, company_id: int, resource: str, additional: int = 1) -> tuple[bool, int, int]:
    plan = effective_plan(conn, company_id)
    usage = plan_usage(conn, company_id)
    limit_key = {"users": "user_limit", "feeds": "feed_limit", "promotions": "promotion_limit", "campaigns": "campaign_limit", "inventory": "inventory_limit", "api_keys": "api_key_limit"}.get(resource, f"{resource}_limit")
    limit = int(plan.get(limit_key, 0) or 0)
    current = int(usage.get(resource, 0))
    if limit <= 0:
        return False, current, limit
    return current + additional <= limit, current, limit


def trial_days_remaining(sub: dict[str, Any]) -> int | None:
    if sub.get("status") != "trialing":
        return None
    end = parse_iso(sub.get("trial_ends_at"))
    if not end:
        return None
    return max(0, int((end - datetime.now(timezone.utc)).total_seconds() // 86400) + 1)


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _stripe_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {STRIPE_SECRET_KEY}", "Content-Type": "application/x-www-form-urlencoded"}


def _stripe_post(path: str, data: list[tuple[str, str]] | dict[str, str]) -> dict[str, Any]:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe is not configured")
    with httpx.Client(timeout=20.0) as client:
        response = client.post(f"https://api.stripe.com/v1/{path.lstrip('/')}", headers=_stripe_headers(), data=data)
        response.raise_for_status()
        return response.json()


def ensure_stripe_customer(conn, company: Any, owner_email: str) -> str:
    sub = subscription_for(conn, company["id"])
    if sub.get("stripe_customer_id"):
        return sub["stripe_customer_id"]
    customer_data: list[tuple[str, str]] = [("name", company["name"]), ("email", owner_email), ("metadata[company_id]", str(company["id"]))]
    if STRIPE_TEST_CLOCK_ID:
        customer_data.append(("test_clock", STRIPE_TEST_CLOCK_ID))
    customer = _stripe_post("customers", customer_data)
    conn.execute("UPDATE billing_subscriptions SET stripe_customer_id=?,updated_at=? WHERE company_id=?", (customer["id"], now_iso(), company["id"]))
    return customer["id"]


def create_checkout_session(conn, company: Any, owner_email: str, plan_key: str, interval: str) -> str:
    if plan_key not in PLANS or plan_key == "buyer_free":
        raise ValueError("Invalid paid plan")
    if interval not in {"monthly", "annual"}:
        raise ValueError("Invalid interval")
    price_id = plan_price_env(plan_key, interval)
    if not price_id:
        raise RuntimeError(f"Stripe price ID is not configured for {plan_key} {interval}")
    customer_id = ensure_stripe_customer(conn, company, owner_email)
    sub = subscription_for(conn, company["id"])
    data: list[tuple[str, str]] = [
        ("mode", "subscription"), ("customer", customer_id), ("line_items[0][price]", price_id), ("line_items[0][quantity]", "1"),
        ("success_url", f"{PUBLIC_BASE_URL}/billing?checkout=success"), ("cancel_url", f"{PUBLIC_BASE_URL}/billing?checkout=cancelled"),
        ("client_reference_id", str(company["id"])), ("metadata[company_id]", str(company["id"])), ("metadata[plan_key]", plan_key),
        ("subscription_data[metadata][company_id]", str(company["id"])), ("subscription_data[metadata][plan_key]", plan_key),
    ]
    # Preserve the remaining no-card local trial when a founding company chooses a plan early.
    trial_end = parse_iso(sub.get("trial_ends_at")) if sub.get("status") == "trialing" else None
    now = datetime.now(timezone.utc)
    if trial_end and trial_end > now:
        if trial_end >= now + timedelta(hours=48):
            data.append(("subscription_data[trial_end]", str(int(trial_end.timestamp()))))
        else:
            # Checkout requires an explicit trial_end to be >=48h away. Do not charge a
            # founding supplier early just because they subscribe during the final day;
            # grant Stripe's minimum practical two-day trial and let the webhook become
            # the source of truth for the slightly extended end time.
            data.append(("subscription_data[trial_period_days]", "2"))
        # Stripe Checkout otherwise asks for a payment method even though nothing is due.
        # Founding trials are intentionally no-card; the supplier can add payment details
        # later from the Billing Portal.
        data.append(("payment_method_collection", "if_required"))
        data.append(("subscription_data[trial_settings][end_behavior][missing_payment_method]", STRIPE_TRIAL_END_BEHAVIOR))
    session = _stripe_post("checkout/sessions", data)
    return session["url"]


def create_portal_session(conn, company_id: int) -> str:
    sub = subscription_for(conn, company_id)
    if not sub.get("stripe_customer_id"):
        raise RuntimeError("No Stripe customer exists yet")
    session = _stripe_post("billing_portal/sessions", {"customer": sub["stripe_customer_id"], "return_url": STRIPE_PORTAL_RETURN_URL})
    return session["url"]


def verify_stripe_signature(payload: bytes, signature_header: str, tolerance: int = 300) -> bool:
    if not STRIPE_WEBHOOK_SECRET:
        return False
    parts = {}
    for pair in (signature_header or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            parts.setdefault(k, []).append(v)
    try:
        timestamp = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > tolerance:
        return False
    signed = str(timestamp).encode() + b"." + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def process_stripe_event(event: dict[str, Any]) -> bool:
    event_id = str(event.get("id", ""))
    event_type = str(event.get("type", ""))
    if not event_id:
        return False
    payload_hash = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    obj = ((event.get("data") or {}).get("object") or {})
    with db() as conn:
        if scalar(conn, "SELECT COUNT(*) FROM billing_events WHERE event_id=?", (event_id,)):
            return True
        company_id = None
        metadata = obj.get("metadata") or {}
        if metadata.get("company_id"):
            try: company_id = int(metadata["company_id"])
            except Exception: pass
        if event_type == "checkout.session.completed":
            if not company_id and obj.get("client_reference_id"):
                try: company_id = int(obj["client_reference_id"])
                except Exception: pass
            if company_id:
                conn.execute("UPDATE billing_subscriptions SET stripe_customer_id=COALESCE(?,stripe_customer_id),stripe_subscription_id=COALESCE(?,stripe_subscription_id),updated_at=? WHERE company_id=?",
                             (obj.get("customer"), obj.get("subscription"), now_iso(), company_id))
        elif event_type.startswith("customer.subscription."):
            if not company_id and obj.get("id"):
                row = conn.execute("SELECT company_id FROM billing_subscriptions WHERE stripe_subscription_id=?", (obj.get("id"),)).fetchone()
                company_id = row["company_id"] if row else None
            if company_id:
                current = subscription_for(conn, company_id)
                trial_end = datetime.fromtimestamp(obj["trial_end"], timezone.utc).isoformat(timespec="seconds") if obj.get("trial_end") else None
                period_end = datetime.fromtimestamp(obj["current_period_end"], timezone.utc).isoformat(timespec="seconds") if obj.get("current_period_end") else None
                price_id = None
                items = ((obj.get("items") or {}).get("data") or [])
                if items:
                    price_id = ((items[0].get("price") or {}).get("id"))
                candidate_plan = plan_key_for_price_id(price_id) or metadata.get("plan_key") or current["plan_key"]
                company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
                plan_key = candidate_plan if _plan_allowed_for_company(company, candidate_plan) else current["plan_key"]
                if candidate_plan != plan_key:
                    logger.warning("stripe_plan_not_allowed company_id=%s candidate_plan=%s price_id=%s", company_id, candidate_plan, price_id)
                conn.execute("""UPDATE billing_subscriptions SET plan_key=?,status=?,stripe_customer_id=COALESCE(?,stripe_customer_id),
                    stripe_subscription_id=?,stripe_price_id=COALESCE(?,stripe_price_id),trial_ends_at=COALESCE(?,trial_ends_at),
                    current_period_end=?,cancel_at_period_end=?,updated_at=? WHERE company_id=?""",
                    (plan_key, obj.get("status", "active"), obj.get("customer"), obj.get("id"), price_id, trial_end, period_end,
                     1 if obj.get("cancel_at_period_end") else 0, now_iso(), company_id))
                reconcile_plan_limits(conn, company_id, plan_key)
        elif event_type == "invoice.payment_failed":
            subscription_id = obj.get("subscription")
            if subscription_id:
                conn.execute("UPDATE billing_subscriptions SET status='past_due',updated_at=? WHERE stripe_subscription_id=?", (now_iso(), subscription_id))
        conn.execute("INSERT INTO billing_events(provider,event_id,event_type,payload_hash,processed_at) VALUES('stripe',?,?,?,?)", (event_id, event_type, payload_hash, now_iso()))
    return True


def notify_trial_ending(company_id: int) -> None:
    with db() as conn:
        company = conn.execute("SELECT name FROM companies WHERE id=?", (company_id,)).fetchone()
        users = conn.execute("SELECT email FROM users WHERE company_id=? AND active=1 AND company_role IN ('owner','admin')", (company_id,)).fetchall()
    for u in users:
        send_email(u["email"], "Your Controls Exchange trial is ending", f"Your {company['name']} trial is nearing its end. Choose a plan at {PUBLIC_BASE_URL}/billing to keep inventory live and continue receiving RFQs.")


def supplier_analytics(conn, company_id: int, days: int) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat(timespec="seconds")
    searches = int(scalar(conn, "SELECT COUNT(*) FROM marketplace_events WHERE supplier_company_id=? AND event_type='search_match' AND created_at>=?", (company_id, since)) or 0)
    profile_views = int(scalar(conn, "SELECT COUNT(*) FROM marketplace_events WHERE supplier_company_id=? AND event_type='supplier_profile_view' AND created_at>=?", (company_id, since)) or 0)
    rfqs = int(scalar(conn, "SELECT COUNT(*) FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.supplier_company_id=? AND r.created_at>=?", (company_id, since)) or 0)
    quotes = int(scalar(conn, "SELECT COUNT(*) FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.supplier_company_id=? AND rr.responded_at IS NOT NULL AND r.created_at>=?", (company_id, since)) or 0)
    wins = int(scalar(conn, "SELECT COUNT(*) FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.supplier_company_id=? AND rr.status='accepted' AND r.created_at>=?", (company_id, since)) or 0)
    campaign_impressions = int(scalar(conn, "SELECT COUNT(*) FROM marketplace_events WHERE supplier_company_id=? AND event_type='campaign_impression' AND created_at>=?", (company_id, since)) or 0)
    campaign_clicks = int(scalar(conn, "SELECT COUNT(*) FROM marketplace_events WHERE supplier_company_id=? AND event_type='campaign_click' AND created_at>=?", (company_id, since)) or 0)
    promoted_impressions = int(scalar(conn, "SELECT COUNT(*) FROM marketplace_events WHERE supplier_company_id=? AND event_type='promoted_impression' AND created_at>=?", (company_id, since)) or 0)
    top_searches = conn.execute("SELECT query,COUNT(*) AS hits FROM marketplace_events WHERE supplier_company_id=? AND event_type='search_match' AND created_at>=? AND query<>'' GROUP BY query ORDER BY hits DESC LIMIT 10", (company_id, since)).fetchall()
    top_rfqs = conn.execute("SELECT r.part_number,COUNT(*) AS hits FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.supplier_company_id=? AND r.created_at>=? GROUP BY r.part_number ORDER BY hits DESC LIMIT 10", (company_id, since)).fetchall()
    return {"days": days, "search_matches": searches, "profile_views": profile_views, "rfqs": rfqs, "quotes": quotes, "wins": wins,
            "quote_rate": round(quotes / rfqs * 100) if rfqs else None, "win_rate": round(wins / quotes * 100) if quotes else None,
            "campaign_impressions": campaign_impressions, "campaign_clicks": campaign_clicks,
            "campaign_ctr": round(campaign_clicks / campaign_impressions * 100, 1) if campaign_impressions else None,
            "promoted_impressions": promoted_impressions, "top_searches": top_searches, "top_rfqs": top_rfqs}


def process_trial_reminders() -> int:
    """Send deduplicated local-trial reminders. Safe to call repeatedly from a worker."""
    now = datetime.now(timezone.utc)
    sent = 0
    with db() as conn:
        rows = conn.execute("""SELECT bs.*,c.name FROM billing_subscriptions bs JOIN companies c ON c.id=bs.company_id
            WHERE c.company_type='supplier' AND c.active=1 AND bs.status='trialing' AND bs.trial_ends_at IS NOT NULL""").fetchall()
        outgoing = []
        for row in rows:
            end = parse_iso(row["trial_ends_at"])
            if not end:
                continue
            remaining = (end-now).total_seconds()/86400
            if remaining <= 0:
                key, subject = "trial_expired", "Your Controls Exchange trial has ended"
                body = f"Your {row['name']} trial has ended. Your inventory is no longer visible in marketplace search until you choose a plan. Choose a plan: {PUBLIC_BASE_URL}/billing"
            elif remaining <= 3:
                key, subject = "trial_3d", "3 days left on your Controls Exchange trial"
                body = f"Your {row['name']} trial ends on {row['trial_ends_at'][:10]}. Choose a plan to keep inventory live: {PUBLIC_BASE_URL}/billing"
            elif remaining <= 7:
                key, subject = "trial_7d", "7 days left on your Controls Exchange trial"
                body = f"Your {row['name']} trial ends on {row['trial_ends_at'][:10]}. Review plans: {PUBLIC_BASE_URL}/billing"
            elif remaining <= 14:
                key, subject = "trial_14d", "Your Controls Exchange trial ends in two weeks"
                body = f"Your {row['name']} trial ends on {row['trial_ends_at'][:10]}. Review usage and plans: {PUBLIC_BASE_URL}/billing"
            else:
                continue
            if scalar(conn, "SELECT COUNT(*) FROM billing_notifications WHERE company_id=? AND notification_key=?", (row["company_id"], key)):
                continue
            emails = conn.execute("SELECT email FROM users WHERE company_id=? AND active=1 AND company_role IN ('owner','admin')", (row["company_id"],)).fetchall()
            conn.execute("INSERT INTO billing_notifications(company_id,notification_key,sent_at) VALUES(?,?,?)", (row["company_id"], key, now_iso()))
            outgoing.extend((u["email"], subject, body) for u in emails)
    for email, subject, body in outgoing:
        send_email(email, subject, body)
        sent += 1
    return sent
