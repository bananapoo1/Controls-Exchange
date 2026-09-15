from __future__ import annotations

import base64
import json
import logging
import math
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from platform_core import (
    BASE_DIR,
    COMPANY_ROLES,
    ENVIRONMENT,
    IS_POSTGRES,
    MAX_UPLOAD_BYTES,
    PUBLIC_BASE_URL,
    SECRET_KEY,
    audit,
    can,
    decrypt_secret_dict,
    encrypt_secret_dict,
    generate_ingest_token,
    consume_token,
    db,
    get_valid_token,
    hash_password,
    init_db,
    insert_id,
    issue_token,
    freshness_cutoff_iso,
    freshness_details,
    inventory_match_score,
    logger,
    normalize,
    now_iso,
    parse_iso,
    parse_inventory_file,
    scalar,
    send_email,
    supplier_trust_metrics,
    verify_password,
    token_hash,
)

from catalog_intelligence import (
    CATALOG_RELATION_TYPES,
    LIFECYCLE_STATUSES,
    catalog_candidates,
    identify_photo,
    link_inventory_ids,
    match_identification_to_catalog,
    part_detail,
    seed_demo_catalog,
    source_assistant,
    technical_recommendations,
)

from billing import (
    PLANS, FOUNDING_TRIAL_DAYS, STANDARD_TRIAL_DAYS, FOUNDING_TRIAL_ENABLED,
    init_billing_schema, start_trial_if_needed, subscription_for, effective_plan, commercial_access,
    plan_usage, check_limit, trial_days_remaining, stripe_enabled, create_checkout_session,
    create_portal_session, verify_stripe_signature, process_stripe_event, record_marketplace_event,
    supplier_analytics, count_active_promotions,
)


from intelligence import (
    API_SCOPES, MAX_CONTRIBUTOR_SHARE, MIN_BUYER_COMPANIES, MIN_DEMAND_EVENTS, MIN_QUOTE_BUYER_COMPANIES, MIN_QUOTE_SAMPLES, MIN_QUOTE_SUPPLIERS,
    api_key_rows, api_usage_summary, authenticate_api_key, create_api_key, init_intelligence_schema,
    network_demand, network_summary, part_intelligence, pricing_intelligence, record_api_usage,
    record_demand_event, revoke_api_key, supplier_opportunities,
)

from procurement import (
    ORDER_STATUSES, WEBHOOK_EVENTS, ORDER_DOCUMENT_MAX_BYTES,
    create_order_from_selected_quote, create_webhook_endpoint, document_path,
    init_procurement_schema, order_for_company, order_payload, queue_order_webhooks,
    record_order_event, rotate_webhook_secret, save_order_document, validate_webhook_url,
)

from inventory_ingestion import (
    CANONICAL_FIELDS,
    FIELD_LABELS,
    create_staging,
    delete_staging,
    feed_public_config,
    feed_secrets,
    guess_column_mapping,
    inbound_address,
    inbound_feed_by_token,
    load_staging,
    make_ingest_token,
    mapping_from_form,
    preview_duplicates,
    read_tabular_file,
    run_feed,
    mark_feed_failed,
    run_import,
)

# ---------- logging / monitoring ----------
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENVIRONMENT,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
        )
    except Exception:
        logger.exception("sentry_initialization_failed")

app = FastAPI(title="Controls Exchange", docs_url=None if ENVIRONMENT == "production" else "/docs")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "true" if ENVIRONMENT == "production" else "false").lower() == "true",
    max_age=60 * 60 * 24 * 14,
)
allowed_hosts = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h.strip()]
if allowed_hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)


@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))[:128]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        error_path = request.url.path
        if error_path.startswith("/api/inventory-feed/"):
            error_path = "/api/inventory-feed/[redacted]"
        elif error_path.startswith("/api/inventory-email/"):
            error_path = "/api/inventory-email/[redacted]"
        logger.exception("request_failed id=%s method=%s path=%s", request_id, request.method, error_path)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    log_path = request.url.path
    if log_path.startswith("/api/inventory-feed/"):
        log_path = "/api/inventory-feed/[redacted]"
    elif log_path.startswith("/api/inventory-email/"):
        log_path = "/api/inventory-email/[redacted]"
    logger.info("request id=%s method=%s path=%s status=%s duration_ms=%s", request_id, request.method, log_path, response.status_code, elapsed_ms)
    return response


# ---------- session / rendering helpers ----------
def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        user = conn.execute(
            """
            SELECT u.*, c.name AS company_name, c.location AS company_location,
                   c.verified AS company_verified, c.company_type, c.account_kind
            FROM users u JOIN companies c ON c.id=u.company_id
            WHERE u.id=? AND u.active=1 AND c.active=1
            """,
            (user_id,),
        ).fetchone()
    if not user:
        request.session.clear()
        return None
    if request.session.get("session_version") != user["session_version"]:
        request.session.clear()
        return None
    return user


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf_token"] = token
    return token


def check_csrf(request: Request, token: str) -> None:
    import hmac
    expected = request.session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, token or ""):
        raise HTTPException(status_code=400, detail="Invalid form token. Refresh and try again.")


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    user = current_user(request)
    billing = None
    if user and user["role"] != "admin":
        with db() as conn:
            billing = subscription_for(conn, user["company_id"])
            billing["days_remaining"] = trial_days_remaining(billing)
    context.update(
        request=request,
        user=user,
        csrf_token=ensure_csrf(request),
        flash=request.session.pop("flash", None),
        company_roles=COMPANY_ROLES,
        can=can,
        environment=ENVIRONMENT,
        public_base_url=PUBLIC_BASE_URL,
        billing=billing,
        plans=PLANS,
        founding_trial_days=FOUNDING_TRIAL_DAYS,
    )
    return HTMLResponse(env.get_template(template).render(**context), status_code=status_code)


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def require_verified_email(request: Request):
    user = require_user(request)
    if not user["email_verified"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Verify your email address before using this feature.")
    return user


def require_permission(request: Request, permission: str):
    user = require_verified_email(request)
    if not can(user, permission):
        raise HTTPException(status_code=403, detail="Your company role does not permit this action.")
    return user


def is_last_owner(conn, company_id: int, user_id: int) -> bool:
    owners = scalar(conn, "SELECT COUNT(*) FROM users WHERE company_id=? AND company_role='owner' AND active=1", (company_id,)) or 0
    target = conn.execute("SELECT company_role, active FROM users WHERE id=? AND company_id=?", (user_id, company_id)).fetchone()
    return bool(target and target["active"] and target["company_role"] == "owner" and int(owners) <= 1)


def safe_next(value: str, default: str = "/dashboard") -> str:
    return value if value.startswith("/") and not value.startswith("//") else default


def safe_external_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("https://") or (ENVIRONMENT != "production" and value.startswith("http://")):
        return value[:500]
    raise ValueError("External links must use HTTPS." if ENVIRONMENT == "production" else "External links must use http:// or https://.")



def search_inventory(conn, q: str, brand: str = "", condition: str = "", *, exclude_company_id: int | None = None, limit: int = 100):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    nq = normalize(q)
    if not nq:
        return []
    q_lower = q.lower()
    broad = nq[:3] if len(nq) >= 3 else nq
    now = now_iso()
    params: list = [now, now, now, freshness_cutoff_iso(), f"%{nq}%", f"%{q_lower}%", f"%{q_lower}%", f"%{q_lower}%", f"%{broad}%"]
    extra = ""
    if exclude_company_id is not None:
        extra += " AND i.company_id != ?"
        params.append(exclude_company_id)
    if brand:
        extra += " AND lower(i.brand)=?"
        params.append(brand.lower())
    if condition:
        extra += " AND lower(i.condition)=?"
        params.append(condition.lower())
    rows = conn.execute(
        f"""
        SELECT i.id, i.company_id, i.brand, i.part_number, i.normalized_part, i.description, i.condition,
               i.quantity, i.location, i.updated_at, i.last_confirmed_at, i.canonical_part_id,
               i.catalog_match_method, i.catalog_match_confidence, c.name AS supplier_name,
               CASE WHEN bs.plan_key IN ('supplier_pro','supplier_premium','manufacturer') AND EXISTS(
                    SELECT 1 FROM inventory_promotions ip WHERE ip.inventory_id=i.id AND ip.active=1
                      AND ip.starts_at<=? AND (ip.ends_at IS NULL OR ip.ends_at>?)
               ) THEN 1 ELSE 0 END AS promoted
        FROM inventory i JOIN companies c ON c.id=i.company_id
        JOIN billing_subscriptions bs ON bs.company_id=c.id
        WHERE i.active=1 AND i.deleted_at IS NULL AND i.quantity>0
          AND c.active=1 AND c.verified=1
          AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))
          AND i.last_confirmed_at IS NOT NULL AND i.last_confirmed_at>=?
          AND (i.normalized_part LIKE ? OR lower(i.part_number) LIKE ? OR lower(i.brand) LIKE ?
               OR lower(i.description) LIKE ? OR i.normalized_part LIKE ?)
          {extra}
        ORDER BY i.last_confirmed_at DESC
        LIMIT 600
        """,
        tuple(params),
    ).fetchall()
    ranked = []
    for row in rows:
        score, reason = inventory_match_score(q, row)
        if score < 47:
            continue
        item = dict(row)
        item["match_score"] = score
        item["match_reason"] = reason
        item["freshness"] = freshness_details(item.get("last_confirmed_at"))
        ranked.append(item)
    # Promotion only breaks ties among similarly relevant results; it never buys relevance.
    ranked.sort(key=lambda item: (-item["match_score"], -int(item.get("promoted") or 0), item["freshness"].get("age_days") if item["freshness"].get("age_days") is not None else 99999, item["part_number"]))
    return ranked[:limit]


def process_inventory_match_alerts(inventory_ids: list[int]) -> dict[str, int]:
    """Send saved-search and wanted-request alerts for newly changed searchable stock.

    Unique match tables prevent duplicate email for the same search/request + inventory line.
    """
    ids = sorted({int(i) for i in inventory_ids if i})
    if not ids:
        return {"saved_search_alerts": 0, "wanted_alerts": 0}
    placeholders = ",".join("?" for _ in ids)
    outgoing: list[tuple[str, str, str]] = []
    saved_emails = wanted_emails = 0
    with db() as conn:
        items = [dict(r) for r in conn.execute(
            f"""SELECT i.*,c.name AS supplier_name FROM inventory i JOIN companies c ON c.id=i.company_id
            JOIN billing_subscriptions bs ON bs.company_id=c.id
            WHERE i.id IN ({placeholders}) AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0
              AND i.last_confirmed_at>=? AND c.active=1 AND c.verified=1
              AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))""",
            tuple(ids + [freshness_cutoff_iso(), now_iso()]),
        ).fetchall()]
        if not items:
            return {"saved_search_alerts": 0, "wanted_alerts": 0}

        searches = conn.execute(
            """SELECT s.*,u.email,u.company_id,u.name FROM saved_searches s JOIN users u ON u.id=s.user_id
            JOIN companies c ON c.id=u.company_id
            WHERE s.active=1 AND u.active=1 AND u.email_verified=1 AND c.active=1"""
        ).fetchall()
        for search in searches:
            matched = []
            for item in items:
                if item["company_id"] == search["company_id"]:
                    continue
                if search["brand"] and item["brand"].lower() != search["brand"].lower():
                    continue
                if search["condition"] and item["condition"].lower() != search["condition"].lower():
                    continue
                score, _ = inventory_match_score(search["query"], item)
                if score < 67:
                    continue
                exists = scalar(conn, "SELECT COUNT(*) FROM saved_search_matches WHERE saved_search_id=? AND inventory_id=?", (search["id"], item["id"]))
                if exists:
                    continue
                conn.execute("INSERT INTO saved_search_matches(saved_search_id,inventory_id,notified_at) VALUES(?,?,?)", (search["id"], item["id"], now_iso()))
                matched.append(item)
            if matched:
                conn.execute("UPDATE saved_searches SET last_alerted_at=? WHERE id=?", (now_iso(), search["id"]))
                lines = "\n".join(f"- {m['brand']} {m['part_number']} · {m['condition']} · qty {m['quantity']} · {m['supplier_name']}" for m in matched[:8])
                outgoing.append((search["email"], f"New Controls Exchange match: {search['query']}", f"New verified inventory matches your saved search for {search['query']}:\n\n{lines}\n\nSearch again: {PUBLIC_BASE_URL}/?q={search['normalized_query']}#search"))
                saved_emails += 1

        wanted = conn.execute(
            """SELECT w.*,u.email,u.company_id,u.name FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id
            JOIN companies c ON c.id=u.company_id WHERE w.status='open' AND u.active=1 AND u.email_verified=1 AND c.active=1"""
        ).fetchall()
        for request_row in wanted:
            matched = []
            for item in items:
                if item["company_id"] == request_row["company_id"]:
                    continue
                score, _ = inventory_match_score(request_row["query"], item)
                if score < 72:
                    continue
                exists = scalar(conn, "SELECT COUNT(*) FROM wanted_matches WHERE wanted_id=? AND inventory_id=?", (request_row["id"], item["id"]))
                if exists:
                    continue
                conn.execute("INSERT INTO wanted_matches(wanted_id,inventory_id,notified_at) VALUES(?,?,?)", (request_row["id"], item["id"], now_iso()))
                matched.append(item)
            if matched:
                lines = "\n".join(f"- {m['brand']} {m['part_number']} · {m['condition']} · qty {m['quantity']} · {m['supplier_name']}" for m in matched[:8])
                outgoing.append((request_row["email"], f"Inventory found for wanted request #{request_row['id']}", f"New verified inventory may match your wanted request for {request_row['query']}:\n\n{lines}\n\nReview it: {PUBLIC_BASE_URL}/wanted/{request_row['id']}"))
                wanted_emails += 1
    for email, subject, body in outgoing:
        send_email(email, subject, body)
    return {"saved_search_alerts": saved_emails, "wanted_alerts": wanted_emails}


# ---------- startup ----------
@app.on_event("startup")
def startup() -> None:
    init_db()
    init_billing_schema()
    init_intelligence_schema()
    init_procurement_schema()
    if os.getenv("SEED_DEMO_DATA", "true" if ENVIRONMENT != "production" else "false").lower() == "true":
        seed_demo_catalog()
    link_inventory_ids()
    logger.info("startup environment=%s database=%s", ENVIRONMENT, "postgresql" if IS_POSTGRES else "sqlite")


# ---------- public ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = ""):
    with db() as conn:
        stats = conn.execute(
            """SELECT
                (SELECT COUNT(*) FROM inventory i JOIN companies c ON c.id=i.company_id JOIN billing_subscriptions bs ON bs.company_id=c.id
                 WHERE i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND i.last_confirmed_at>=? AND c.active=1 AND c.verified=1
                   AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))) AS inventory_count,
                (SELECT COUNT(*) FROM companies c JOIN billing_subscriptions bs ON bs.company_id=c.id
                 WHERE c.company_type='supplier' AND c.verified=1 AND c.active=1
                   AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))) AS supplier_count,
                (SELECT COUNT(*) FROM rfqs) AS rfq_count
            """, (freshness_cutoff_iso(), now_iso(), now_iso())
        ).fetchone()
    return render(request, "index.html", stats=stats, prefill_query=q.strip())


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page(request: Request):
    return render(request, "suppliers.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return render(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return render(request, "terms.html")


# ---------- authentication ----------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    return render(request, "login.html", next=safe_next(next))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    next_url = safe_next(str(form.get("next", "/dashboard")))
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            audit(conn, request, user, "auth.login_failed", "user", user["id"] if user else "", {"email": email}, company_id=user["company_id"] if user else None)
            flash(request, "Incorrect email or password.", "error")
            return RedirectResponse(f"/login?next={next_url}", status_code=303)
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
        audit(conn, request, user, "auth.login", "user", user["id"])
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["session_version"] = user["session_version"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    flash(request, "Welcome back.")
    return RedirectResponse(next_url, status_code=303)


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = current_user(request)
    if user:
        with db() as conn:
            audit(conn, request, user, "auth.logout", "user", user["id"])
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, account_type: str = "buyer"):
    account_type = account_type if account_type in {"buyer", "supplier", "manufacturer"} else "buyer"
    return render(request, "register.html", account_type=account_type)


@app.post("/register")
async def register(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    account_type = str(form.get("account_type", "buyer"))
    if account_type not in {"buyer", "supplier", "manufacturer"}:
        account_type = "buyer"
    account_kind = "manufacturer" if account_type == "manufacturer" else "trade"
    platform_type = "supplier" if account_type == "manufacturer" else account_type
    company_name = str(form.get("company_name", "")).strip()
    location = str(form.get("location", "")).strip()
    name = str(form.get("name", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    if not company_name or not name or "@" not in email or len(password) < 10:
        flash(request, "Complete all fields and use a password of at least 10 characters.", "error")
        return RedirectResponse(f"/register?account_type={account_type}", status_code=303)
    try:
        with db() as conn:
            if scalar(conn, "SELECT COUNT(*) FROM users WHERE lower(email)=?", (email,)):
                flash(request, "That email address is already registered.", "error")
                return RedirectResponse("/login", status_code=303)
            company_id = insert_id(conn,
                "INSERT INTO companies(name,company_type,location,verified,active,created_at,account_kind) VALUES(?,?,?,?,?,?,?)",
                (company_name, platform_type, location, 1 if platform_type == "buyer" else 0, 1, now_iso(), account_kind))
            user_id = insert_id(conn,
                "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (company_id, name, email, hash_password(password), platform_type, "owner", 0, 1, now_iso()))
            token = issue_token(conn, "verify_email", user_id=user_id, company_id=company_id, email=email, hours=24)
            created_user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            audit(conn, request, created_user, "company.created", "company", company_id, {"company_type": platform_type, "account_kind": account_kind})
    except Exception:
        logger.exception("registration_failed")
        flash(request, "We could not create that account. Please try again.", "error")
        return RedirectResponse(f"/register?account_type={account_type}", status_code=303)

    verify_url = f"{PUBLIC_BASE_URL}/verify-email/{token}"
    send_email(email, "Verify your Controls Exchange email", f"Verify your work email to activate trading features:\n\n{verify_url}\n\nThis link expires in 24 hours.")
    if platform_type == "supplier":
        with db() as conn:
            admins = conn.execute("SELECT email FROM users WHERE role='admin' AND active=1").fetchall()
        for admin in admins:
            send_email(admin["email"], "New supplier awaiting verification", f"{company_name} ({email}) registered as a supplier and is awaiting company verification.")
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["session_version"] = created_user["session_version"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    flash(request, "Account created. Check your email to verify your address before trading.")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/verify-email/{token}")
def verify_email(request: Request, token: str):
    with db() as conn:
        record = get_valid_token(conn, token, "verify_email")
        if not record or not record["user_id"]:
            flash(request, "That verification link is invalid or has expired.", "error")
            return RedirectResponse("/dashboard", status_code=303)
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (record["user_id"],)).fetchone()
        if not user:
            flash(request, "That account is no longer active.", "error")
            return RedirectResponse("/", status_code=303)
        conn.execute("UPDATE users SET email_verified=1,email_verified_at=? WHERE id=?", (now_iso(), user["id"]))
        consume_token(conn, record["id"])
        conn.execute("UPDATE auth_tokens SET used_at=? WHERE user_id=? AND purpose='verify_email' AND used_at IS NULL", (now_iso(), user["id"]))
        audit(conn, request, user, "auth.email_verified", "user", user["id"])
    flash(request, "Email verified. Your trading features are now active.")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/verification/resend")
async def resend_verification(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["email_verified"]:
        flash(request, "Your email is already verified.")
        return RedirectResponse("/dashboard", status_code=303)
    with db() as conn:
        conn.execute("UPDATE auth_tokens SET used_at=? WHERE user_id=? AND purpose='verify_email' AND used_at IS NULL", (now_iso(), user["id"]))
        token = issue_token(conn, "verify_email", user_id=user["id"], company_id=user["company_id"], email=user["email"], hours=24)
        audit(conn, request, user, "auth.verification_resent", "user", user["id"])
    send_email(user["email"], "Verify your Controls Exchange email", f"Verify your work email:\n\n{PUBLIC_BASE_URL}/verify-email/{token}\n\nThis link expires in 24 hours.")
    flash(request, "A new verification email has been sent.")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return render(request, "forgot_password.html")


@app.post("/forgot-password")
async def forgot_password(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    email = str(form.get("email", "")).strip().lower()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
        if user:
            conn.execute("UPDATE auth_tokens SET used_at=? WHERE user_id=? AND purpose='password_reset' AND used_at IS NULL", (now_iso(), user["id"]))
            token = issue_token(conn, "password_reset", user_id=user["id"], company_id=user["company_id"], email=email, hours=1)
            audit(conn, request, user, "auth.password_reset_requested", "user", user["id"])
        else:
            token = None
    if token:
        send_email(email, "Reset your Controls Exchange password", f"Reset your password:\n\n{PUBLIC_BASE_URL}/reset-password/{token}\n\nThis link expires in 1 hour.")
    flash(request, "If that email is registered, a password-reset link has been sent.")
    return RedirectResponse("/login", status_code=303)


@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str):
    with db() as conn:
        valid = get_valid_token(conn, token, "password_reset")
    return render(request, "reset_password.html", token=token, valid=bool(valid))


@app.post("/reset-password/{token}")
async def reset_password(request: Request, token: str):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm_password", ""))
    if len(password) < 10 or password != confirm:
        flash(request, "Use a password of at least 10 characters and make sure both entries match.", "error")
        return RedirectResponse(f"/reset-password/{token}", status_code=303)
    with db() as conn:
        record = get_valid_token(conn, token, "password_reset")
        if not record or not record["user_id"]:
            flash(request, "That reset link is invalid or has expired.", "error")
            return RedirectResponse("/forgot-password", status_code=303)
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (record["user_id"],)).fetchone()
        if not user:
            raise HTTPException(status_code=404)
        conn.execute(
            "UPDATE users SET password_hash=?,session_version=session_version+1,email_verified=1,email_verified_at=COALESCE(email_verified_at,?) WHERE id=?",
            (hash_password(password), now_iso(), user["id"]),
        )
        consume_token(conn, record["id"])
        conn.execute("UPDATE auth_tokens SET used_at=? WHERE user_id=? AND purpose='password_reset' AND used_at IS NULL", (now_iso(), user["id"]))
        audit(conn, request, user, "auth.password_reset_completed", "user", user["id"])
    request.session.clear()
    flash(request, "Password updated. Sign in with your new password.")
    return RedirectResponse("/login", status_code=303)


# ---------- search / directory ----------
@app.get("/api/search")
def api_search(request: Request, q: str = "", brand: str = "", condition: str = ""):
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "count": 0}
    user = current_user(request)
    exclude_company_id = user["company_id"] if user and user["role"] == "supplier" else None
    with db() as conn:
        ranked = search_inventory(conn, q, brand, condition, exclude_company_id=exclude_company_id, limit=100)
        record_demand_event(
            conn, "search", company_id=user["company_id"] if user else None, query=q,
            quantity=1, result_count=len(ranked),
        )
        seen_suppliers = set()
        for match in ranked:
            sid = match.get("company_id")
            if sid and sid not in seen_suppliers:
                record_marketplace_event(conn, "search_match", user_id=user["id"] if user else None, user_company_id=user["company_id"] if user else None, supplier_company_id=sid, query=q, metadata={"brand": brand, "condition": condition})
                seen_suppliers.add(sid)
            if sid and match.get("promoted"):
                record_marketplace_event(conn, "promoted_impression", user_id=user["id"] if user else None, user_company_id=user["company_id"] if user else None, supplier_company_id=sid, inventory_id=match.get("id"), query=q)
        trust_cache = {}
        catalog_cache = {}
        results = []
        for item in ranked:
            company_id = item.pop("company_id", None)
            freshness = item.pop("freshness")
            item.update({
                "freshness_state": freshness["state"],
                "freshness_label": freshness["label"],
                "age_days": freshness["age_days"],
            })
            catalog_id = item.get("canonical_part_id")
            if catalog_id:
                if catalog_id not in catalog_cache:
                    cp = conn.execute("SELECT id,manufacturer,part_number,name,product_family,lifecycle_status,verified FROM catalog_parts WHERE id=?", (catalog_id,)).fetchone()
                    rel_count = scalar(conn, "SELECT COUNT(*) FROM catalog_relations WHERE from_part_id=?", (catalog_id,)) or 0
                    catalog_cache[catalog_id] = {**dict(cp), "relation_count": int(rel_count)} if cp else None
                item["catalog_part"] = catalog_cache[catalog_id]
            if user:
                item["supplier_id"] = company_id
                if company_id not in trust_cache:
                    trust_cache[company_id] = supplier_trust_metrics(conn, company_id)
                item["supplier_trust"] = trust_cache[company_id]
            else:
                item["supplier_name"] = "Verified supplier"
            results.append(item)
    return {"results": results, "count": len(results)}


@app.get("/saved-searches", response_class=HTMLResponse)
def saved_searches_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/saved-searches", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse("/admin", status_code=303)
    with db() as conn:
        rows = conn.execute("""SELECT s.*,
            (SELECT COUNT(*) FROM saved_search_matches sm WHERE sm.saved_search_id=s.id) AS match_count
            FROM saved_searches s WHERE s.user_id=? AND s.active=1 ORDER BY s.created_at DESC""", (user["id"],)).fetchall()
    return render(request, "saved_searches.html", rows=rows)


@app.post("/saved-searches")
async def save_search(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin": raise HTTPException(status_code=403)
    query = str(form.get("query", "")).strip()
    brand = str(form.get("brand", "")).strip()
    condition = str(form.get("condition", "")).strip()
    if len(query) < 2:
        flash(request, "Enter at least two characters to save a search.", "error")
        return RedirectResponse("/#search", status_code=303)
    with db() as conn:
        existing = conn.execute("SELECT id FROM saved_searches WHERE user_id=? AND normalized_query=? AND lower(brand)=? AND lower(condition)=? AND active=1", (user["id"], normalize(query), brand.lower(), condition.lower())).fetchone()
        if existing:
            flash(request, "You already have an active alert for that search.")
            return RedirectResponse("/saved-searches", status_code=303)
        search_id = insert_id(conn, "INSERT INTO saved_searches(user_id,query,normalized_query,brand,condition,active,created_at) VALUES(?,?,?,?,?,?,?)", (user["id"], query, normalize(query), brand, condition, 1, now_iso()))
        current = search_inventory(conn, query, brand, condition, exclude_company_id=user["company_id"] if user["role"] == "supplier" else None, limit=100)
        for item in current:
            conn.execute("INSERT INTO saved_search_matches(saved_search_id,inventory_id,notified_at) VALUES(?,?,?)", (search_id, item["id"], now_iso()))
        audit(conn, request, user, "search.saved", "saved_search", search_id, {"query": query, "brand": brand, "condition": condition})
    flash(request, f"Saved search for {query}. We'll email you when newly confirmed stock matches.")
    return RedirectResponse("/saved-searches", status_code=303)


@app.post("/saved-searches/{search_id}/delete")
async def delete_saved_search(request: Request, search_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    with db() as conn:
        row = conn.execute("SELECT * FROM saved_searches WHERE id=? AND user_id=? AND active=1", (search_id, user["id"])).fetchone()
        if not row: raise HTTPException(status_code=404)
        conn.execute("UPDATE saved_searches SET active=0 WHERE id=?", (search_id,))
        audit(conn, request, user, "search.unsaved", "saved_search", search_id, {"query": row["query"]})
    flash(request, "Saved-search alert removed.")
    return RedirectResponse("/saved-searches", status_code=303)


@app.get("/directory", response_class=HTMLResponse)
def directory_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/directory", status_code=303)
    brand_expr = "STRING_AGG(DISTINCT NULLIF(i.brand,''), ',')" if IS_POSTGRES else "GROUP_CONCAT(DISTINCT NULLIF(i.brand,''))"
    with db() as conn:
        raw_companies = conn.execute(
            f"""
            SELECT c.id, c.name, c.location, COUNT(i.id) AS inventory_count,
                   {brand_expr} AS brands
            FROM companies c JOIN billing_subscriptions bs ON bs.company_id=c.id
            LEFT JOIN inventory i ON i.company_id=c.id AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND i.last_confirmed_at>=?
            WHERE c.company_type='supplier' AND c.verified=1 AND c.active=1
              AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))
            GROUP BY c.id, c.name, c.location ORDER BY c.name
            """, (freshness_cutoff_iso(), now_iso())
        ).fetchall()
        companies = []
        for row in raw_companies:
            company = dict(row)
            company["trust"] = supplier_trust_metrics(conn, company["id"])
            companies.append(company)
    return render(request, "directory.html", companies=companies)


@app.get("/directory/{company_id}", response_class=HTMLResponse)
def supplier_profile(request: Request, company_id: int):
    if not current_user(request):
        return RedirectResponse(f"/login?next=/directory/{company_id}", status_code=303)
    with db() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id=? AND company_type='supplier' AND verified=1 AND active=1", (company_id,)).fetchone()
        if not company or not commercial_access(conn, company_id):
            raise HTTPException(status_code=404)
        viewer = current_user(request)
        record_marketplace_event(conn, "supplier_profile_view", user_id=viewer["id"] if viewer else None, user_company_id=viewer["company_id"] if viewer else None, supplier_company_id=company_id)
        cutoff = freshness_cutoff_iso()
        brands = conn.execute("SELECT brand, COUNT(*) AS item_count FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL AND quantity>0 AND last_confirmed_at>=? GROUP BY brand ORDER BY item_count DESC, brand LIMIT 20", (company_id, cutoff)).fetchall()
        raw_items = conn.execute("SELECT * FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL AND quantity>0 AND last_confirmed_at>=? ORDER BY last_confirmed_at DESC LIMIT 100", (company_id, cutoff)).fetchall()
        items = []
        for row in raw_items:
            item = dict(row); item["freshness"] = freshness_details(item.get("last_confirmed_at")); items.append(item)
        trust = supplier_trust_metrics(conn, company_id)
    return render(request, "supplier_profile.html", company=company, brands=brands, items=items, trust=trust)


# ---------- dashboard ----------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard", status_code=303)
    with db() as conn:
        if user["role"] == "supplier":
            inventory_count = scalar(conn, "SELECT COUNT(*) FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL", (user["company_id"],)) or 0
            open_rfqs = scalar(conn, "SELECT COUNT(*) FROM rfq_recipients WHERE supplier_company_id=? AND status IN ('sent','viewed')", (user["company_id"],)) or 0
            recent = conn.execute(
                """SELECT rr.id AS recipient_id, rr.status AS recipient_status, r.*, u.name AS buyer_name, c.name AS buyer_company
                FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id
                JOIN users u ON u.id=r.buyer_user_id JOIN companies c ON c.id=u.company_id
                WHERE rr.supplier_company_id=? ORDER BY r.created_at DESC LIMIT 5""", (user["company_id"],)).fetchall()
            team_count = scalar(conn, "SELECT COUNT(*) FROM users WHERE company_id=? AND active=1", (user["company_id"],)) or 0
            saved_search_count = scalar(conn, "SELECT COUNT(*) FROM saved_searches WHERE user_id=? AND active=1", (user["id"],)) or 0
            trust = supplier_trust_metrics(conn, user["company_id"])
            sub = subscription_for(conn, user["company_id"])
            plan = effective_plan(conn, user["company_id"])
            usage = plan_usage(conn, user["company_id"])
            data = {"inventory_count": inventory_count, "open_rfqs": open_rfqs, "recent": recent, "team_count": team_count, "saved_search_count": saved_search_count, "trust": trust, "subscription": sub, "plan": plan, "usage": usage}
        elif user["role"] == "admin":
            pending = scalar(conn, "SELECT COUNT(*) FROM companies WHERE company_type='supplier' AND verified=0 AND active=1") or 0
            data = {"pending_suppliers": pending}
        else:
            rfq_count = scalar(conn, "SELECT COUNT(*) FROM rfqs WHERE buyer_user_id=?", (user["id"],)) or 0
            recent = conn.execute("SELECT * FROM rfqs WHERE buyer_user_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)).fetchall()
            team_count = scalar(conn, "SELECT COUNT(*) FROM users WHERE company_id=? AND active=1", (user["company_id"],)) or 0
            saved_search_count = scalar(conn, "SELECT COUNT(*) FROM saved_searches WHERE user_id=? AND active=1", (user["id"],)) or 0
            data = {"rfq_count": rfq_count, "recent": recent, "team_count": team_count, "saved_search_count": saved_search_count}
    return render(request, "dashboard.html", data=data)


# ---------- company team ----------
@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/team", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse("/admin", status_code=303)
    with db() as conn:
        members = conn.execute("SELECT id,name,email,company_role,email_verified,active,last_login_at,created_at FROM users WHERE company_id=? ORDER BY active DESC, company_role, name", (user["company_id"],)).fetchall()
        invitations = conn.execute("SELECT * FROM auth_tokens WHERE company_id=? AND purpose='invite' AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC", (user["company_id"], now_iso())).fetchall()
    return render(request, "team.html", members=members, invitations=invitations)


@app.post("/team/invite")
async def team_invite(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_team")
    email = str(form.get("email", "")).strip().lower()
    company_role = str(form.get("company_role", "viewer")).strip().lower()
    if "@" not in email or company_role not in COMPANY_ROLES:
        flash(request, "Enter a valid work email and company role.", "error")
        return RedirectResponse("/team", status_code=303)
    if company_role == "owner" and user["company_role"] != "owner":
        flash(request, "Only a company owner can invite another owner.", "error")
        return RedirectResponse("/team", status_code=303)
    with db() as conn:
        if user["role"] == "supplier":
            ok, current, limit = check_limit(conn, user["company_id"], "users", 1)
            if not ok:
                flash(request, f"Your plan allows {limit} active team member(s). Upgrade before inviting another user.", "error")
                return RedirectResponse("/billing", status_code=303)
        if scalar(conn, "SELECT COUNT(*) FROM users WHERE lower(email)=?", (email,)):
            flash(request, "That email already belongs to a Controls Exchange account.", "error")
            return RedirectResponse("/team", status_code=303)
        conn.execute("UPDATE auth_tokens SET used_at=? WHERE company_id=? AND lower(email)=? AND purpose='invite' AND used_at IS NULL", (now_iso(), user["company_id"], email))
        token = issue_token(conn, "invite", company_id=user["company_id"], email=email, company_role=company_role, hours=72)
        audit(conn, request, user, "team.invite_created", "company", user["company_id"], {"email": email, "company_role": company_role})
    send_email(email, f"Join {user['company_name']} on Controls Exchange", f"{user['name']} invited you to join {user['company_name']} as {company_role}.\n\nAccept the invitation:\n{PUBLIC_BASE_URL}/invite/{token}\n\nThis invitation expires in 72 hours.")
    flash(request, f"Invitation sent to {email}.")
    return RedirectResponse("/team", status_code=303)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    with db() as conn:
        record = get_valid_token(conn, token, "invite")
        company = conn.execute("SELECT * FROM companies WHERE id=? AND active=1", (record["company_id"],)).fetchone() if record else None
    return render(request, "invite.html", token=token, invitation=record, company=company, valid=bool(record and company))


@app.post("/invite/{token}")
async def accept_invite(request: Request, token: str):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    password = str(form.get("password", ""))
    if not name or len(password) < 10:
        flash(request, "Enter your name and a password of at least 10 characters.", "error")
        return RedirectResponse(f"/invite/{token}", status_code=303)
    with db() as conn:
        record = get_valid_token(conn, token, "invite")
        if not record:
            flash(request, "That invitation is invalid or has expired.", "error")
            return RedirectResponse("/login", status_code=303)
        company = conn.execute("SELECT * FROM companies WHERE id=? AND active=1", (record["company_id"],)).fetchone()
        if not company or scalar(conn, "SELECT COUNT(*) FROM users WHERE lower(email)=?", (record["email"],)):
            flash(request, "That invitation can no longer be accepted.", "error")
            return RedirectResponse("/login", status_code=303)
        # Re-check the team entitlement at acceptance time. Several outstanding invitations
        # must not be able to bypass a plan's active-user limit.
        if company["company_type"] == "supplier":
            ok, current, limit = check_limit(conn, company["id"], "users", 1)
            if not ok:
                flash(request, f"{company['name']} has reached its {limit}-user plan limit. Ask a company owner to upgrade before accepting this invitation.", "error")
                return RedirectResponse(f"/invite/{token}", status_code=303)
        user_id = insert_id(conn,
            "INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (company["id"], name, record["email"], hash_password(password), company["company_type"], record["company_role"], 1, now_iso(), 1, now_iso()))
        consume_token(conn, record["id"])
        joined = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        audit(conn, request, joined, "team.invite_accepted", "user", user_id, {"company_role": record["company_role"]})
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["session_version"] = joined["session_version"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    flash(request, f"You joined {company['name']}.")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/team/users/{member_id}/role")
async def team_change_role(request: Request, member_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_team")
    new_role = str(form.get("company_role", "viewer"))
    if new_role not in COMPANY_ROLES:
        raise HTTPException(status_code=400)
    with db() as conn:
        member = conn.execute("SELECT * FROM users WHERE id=? AND company_id=?", (member_id, user["company_id"])).fetchone()
        if not member:
            raise HTTPException(status_code=404)
        if user["company_role"] != "owner" and (member["company_role"] == "owner" or new_role == "owner"):
            flash(request, "Only a company owner can change owner-level access.", "error")
            return RedirectResponse("/team", status_code=303)
        if member["company_role"] == "owner" and new_role != "owner" and is_last_owner(conn, user["company_id"], member_id):
            flash(request, "A company must keep at least one active owner.", "error")
            return RedirectResponse("/team", status_code=303)
        conn.execute("UPDATE users SET company_role=?,session_version=session_version+1 WHERE id=?", (new_role, member_id))
        audit(conn, request, user, "team.role_changed", "user", member_id, {"from": member["company_role"], "to": new_role})
    if member_id == user["id"]:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    flash(request, f"Updated {member['name']}'s role.")
    return RedirectResponse("/team", status_code=303)


@app.post("/team/users/{member_id}/deactivate")
async def team_deactivate(request: Request, member_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_team")
    if member_id == user["id"]:
        flash(request, "You cannot deactivate your own account.", "error")
        return RedirectResponse("/team", status_code=303)
    with db() as conn:
        member = conn.execute("SELECT * FROM users WHERE id=? AND company_id=?", (member_id, user["company_id"])).fetchone()
        if not member:
            raise HTTPException(status_code=404)
        if member["company_role"] == "owner" and user["company_role"] != "owner":
            flash(request, "Only a company owner can deactivate another owner.", "error")
            return RedirectResponse("/team", status_code=303)
        if is_last_owner(conn, user["company_id"], member_id):
            flash(request, "A company must keep at least one active owner.", "error")
            return RedirectResponse("/team", status_code=303)
        conn.execute("UPDATE users SET active=0,session_version=session_version+1 WHERE id=?", (member_id,))
        audit(conn, request, user, "team.user_deactivated", "user", member_id)
    flash(request, f"{member['name']} has been deactivated.")
    return RedirectResponse("/team", status_code=303)


@app.post("/team/users/{member_id}/reactivate")
async def team_reactivate(request: Request, member_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_team")
    with db() as conn:
        member = conn.execute("SELECT * FROM users WHERE id=? AND company_id=?", (member_id, user["company_id"])).fetchone()
        if not member:
            raise HTTPException(status_code=404)
        if user["role"] == "supplier" and not member["active"]:
            ok, current, limit = check_limit(conn, user["company_id"], "users", 1)
            if not ok:
                flash(request, f"Your plan allows {limit} active team member(s).", "error")
                return RedirectResponse("/billing", status_code=303)
        conn.execute("UPDATE users SET active=1,session_version=session_version+1 WHERE id=?", (member_id,))
        audit(conn, request, user, "team.user_reactivated", "user", member_id)
    flash(request, f"{member['name']} reactivated.")
    return RedirectResponse("/team", status_code=303)


# ---------- audit log ----------
@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/audit", status_code=303)
    if user["role"] != "admin" and not user["email_verified"]:
        raise HTTPException(status_code=403, detail="Verify your email before viewing the audit log.")
    if user["role"] != "admin" and not can(user, "view_audit"):
        raise HTTPException(status_code=403)
    with db() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                """SELECT a.*, u.name AS actor_name, c.name AS company_name FROM audit_logs a
                LEFT JOIN users u ON u.id=a.actor_user_id LEFT JOIN companies c ON c.id=a.company_id
                ORDER BY a.created_at DESC LIMIT 500""").fetchall()
        else:
            rows = conn.execute(
                """SELECT a.*, u.name AS actor_name, c.name AS company_name FROM audit_logs a
                LEFT JOIN users u ON u.id=a.actor_user_id LEFT JOIN companies c ON c.id=a.company_id
                WHERE a.company_id=? ORDER BY a.created_at DESC LIMIT 500""", (user["company_id"],)).fetchall()
    return render(request, "audit.html", rows=rows)


# ---------- inventory ----------
@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request, view: str = "active"):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/inventory", status_code=303)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    if view not in {"active", "archived", "all"}:
        view = "active"
    where = "deleted_at IS NULL"
    if view == "active": where += " AND active=1"
    if view == "archived": where += " AND active=0 AND archived_at IS NOT NULL"
    with db() as conn:
        raw_rows = conn.execute(f"SELECT * FROM inventory WHERE company_id=? AND {where} ORDER BY last_confirmed_at DESC, updated_at DESC LIMIT 1000", (user["company_id"],)).fetchall()
        rows = []
        for row in raw_rows:
            item = dict(row)
            item["freshness"] = freshness_details(item.get("last_confirmed_at"))
            rows.append(item)
        counts = conn.execute(
            """SELECT
            SUM(CASE WHEN deleted_at IS NULL AND active=1 THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN deleted_at IS NULL AND active=0 AND archived_at IS NOT NULL THEN 1 ELSE 0 END) AS archived_count
            FROM inventory WHERE company_id=?""", (user["company_id"],)).fetchone()
        trust = supplier_trust_metrics(conn, user["company_id"])
    return render(request, "inventory.html", rows=rows, view=view, counts=counts, trust=trust)


@app.post("/inventory/upload")
async def inventory_upload(request: Request, file: UploadFile = File(...), replace_existing: str = Form("no"), csrf_token: str = Form(...)):
    """Backwards-compatible quick import; Phase 3's preview/mapping workflow is preferred."""
    check_csrf(request, csrf_token)
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    raw = await file.read()
    try:
        parsed = read_tabular_file(file.filename or "", raw)
        mapping = guess_column_mapping(parsed.headers)
        if not mapping.get("part_number"):
            flash(request, "We could not confidently identify the part-number column. Use Preview & map columns instead.", "error")
            return RedirectResponse("/inventory", status_code=303)
        mode = "replace" if replace_existing == "yes" else "upsert"
        job_id, changed_ids, counts = run_import(
            company_id=user["company_id"], user_id=user["id"], source_type="manual_quick", source_name=file.filename or "upload",
            raw_rows=parsed.rows, mapping=mapping, import_mode=mode, duplicate_policy="merge", default_location=user["company_location"] or "",
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inventory", status_code=303)
    alert_counts = process_inventory_match_alerts(changed_ids)
    alert_note = f" {alert_counts['saved_search_alerts'] + alert_counts['wanted_alerts']} buyer alert(s) were triggered." if sum(alert_counts.values()) else ""
    flash(request, f"Import complete: {counts['inserted']} added, {counts['updated']} updated, {counts['errors']} row error(s)." + alert_note)
    return RedirectResponse(f"/inventory-imports/{job_id}", status_code=303)

# ---------- Phase 3 inventory imports / feed automation ----------
def _supplier_inventory_user(request: Request):
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    return user


def _feed_view(feed_row):
    item = dict(feed_row)
    item["config"] = feed_public_config(feed_row)
    secret_values = feed_secrets(feed_row)
    item["inbound_address"] = inbound_address(feed_row)
    raw_token = secret_values.get("inbound_token", "")
    item["push_url"] = f"{PUBLIC_BASE_URL}/api/inventory-feed/{raw_token}" if raw_token and feed_row["feed_type"] == "api_push" else ""
    item["email_webhook_url"] = f"{PUBLIC_BASE_URL}/api/inventory-email/{raw_token}" if raw_token and feed_row["feed_type"] == "email" else ""
    try:
        item["mapping"] = json.loads(feed_row["mapping_json"] or "{}")
    except Exception:
        item["mapping"] = {}
    item["has_secret"] = bool(feed_row["secret_blob"])
    return item


@app.get("/inventory-imports", response_class=HTMLResponse)
def inventory_imports_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/inventory-imports", status_code=303)
    if user["role"] == "admin":
        raise HTTPException(status_code=403)
    with db() as conn:
        jobs = conn.execute(
            "SELECT j.*,f.name AS feed_name FROM inventory_import_jobs j LEFT JOIN inventory_feeds f ON f.id=j.feed_id WHERE j.company_id=? ORDER BY j.started_at DESC LIMIT 200",
            (user["company_id"],),
        ).fetchall()
    return render(request, "inventory_imports.html", jobs=jobs)


@app.post("/inventory-imports/preview", response_class=HTMLResponse)
async def inventory_import_preview(request: Request, file: UploadFile = File(...), csrf_token: str = Form(...)):
    check_csrf(request, csrf_token)
    user = _supplier_inventory_user(request)
    raw = await file.read()
    try:
        stage_id, parsed, guessed = create_staging(user["company_id"], user["id"], file.filename or "inventory.csv", raw)
        from inventory_ingestion import transform_rows
        transformed, errors, warnings = transform_rows(parsed.rows, guessed) if guessed.get("part_number") else ([], [], [])
        duplicates = preview_duplicates(transformed, user["company_id"]) if transformed else {"within_file": 0, "existing_matches": 0, "unique_rows": 0}
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inventory", status_code=303)
    return render(
        request,
        "inventory_mapping.html",
        stage_id=stage_id,
        filename=file.filename or "inventory.csv",
        headers=parsed.headers,
        rows=parsed.rows[:10],
        guessed=guessed,
        fields=CANONICAL_FIELDS,
        field_labels=FIELD_LABELS,
        row_count=len(parsed.rows),
        errors=errors[:10],
        warnings=warnings[:10],
        duplicates=duplicates,
    )


@app.post("/inventory-imports/stage/{stage_id}/execute")
async def inventory_import_execute(request: Request, stage_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    mapping = mapping_from_form(form)
    import_mode = str(form.get("import_mode", "upsert"))
    duplicate_policy = str(form.get("duplicate_policy", "merge"))
    try:
        stage, parsed = load_staging(stage_id, user["company_id"], user["id"])
        job_id, changed_ids, counts = run_import(
            company_id=user["company_id"],
            user_id=user["id"],
            source_type="manual_mapped",
            source_name=stage["filename"],
            raw_rows=parsed.rows,
            mapping=mapping,
            import_mode=import_mode,
            duplicate_policy=duplicate_policy,
            default_location=user["company_location"] or "",
        )
        delete_staging(stage_id)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inventory", status_code=303)
    process_inventory_match_alerts(changed_ids)
    flash(request, f"Import finished: {counts['inserted']} added, {counts['updated']} updated, {counts['archived']} archived, {counts['errors']} errors.")
    return RedirectResponse(f"/inventory-imports/{job_id}", status_code=303)


@app.get("/inventory-imports/{job_id}", response_class=HTMLResponse)
def inventory_import_report(request: Request, job_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/inventory-imports/{job_id}", status_code=303)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    with db() as conn:
        job = conn.execute(
            "SELECT j.*,f.name AS feed_name FROM inventory_import_jobs j LEFT JOIN inventory_feeds f ON f.id=j.feed_id WHERE j.id=? AND j.company_id=?",
            (job_id, user["company_id"]),
        ).fetchone()
        if not job:
            raise HTTPException(status_code=404)
        issues = conn.execute(
            "SELECT * FROM inventory_import_errors WHERE job_id=? ORDER BY CASE severity WHEN 'error' THEN 0 ELSE 1 END,row_number,id LIMIT 500",
            (job_id,),
        ).fetchall()
    try:
        report = json.loads(job["report_json"] or "{}")
    except Exception:
        report = {}
    return render(request, "inventory_import_report.html", job=job, issues=issues, report=report)


@app.get("/inventory-feeds", response_class=HTMLResponse)
def inventory_feeds_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/inventory-feeds", status_code=303)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    with db() as conn:
        rows = conn.execute("SELECT * FROM inventory_feeds WHERE company_id=? ORDER BY created_at DESC", (user["company_id"],)).fetchall()
    return render(request, "inventory_feeds.html", feeds=[_feed_view(row) for row in rows])


def _feed_form_values(form, existing=None):
    feed_type = str(form.get("feed_type", (existing["feed_type"] if existing else "url")) or "url")
    if feed_type not in {"url", "api_pull", "sftp", "api_push", "email"}:
        raise ValueError("Unsupported feed type")
    name = str(form.get("name", "") or "").strip()
    if not name:
        raise ValueError("Feed name is required")
    try:
        schedule_minutes = max(15, int(str(form.get("schedule_minutes", "360") or "360")))
    except Exception:
        schedule_minutes = 360
    import_mode = str(form.get("import_mode", "sync") or "sync")
    if feed_type in {"api_push", "email"} and import_mode == "sync":
        import_mode = "upsert"
    if import_mode not in {"add", "upsert", "sync"}:
        import_mode = "sync" if feed_type in {"url", "api_pull", "sftp"} else "upsert"
    duplicate_policy = str(form.get("duplicate_policy", "merge") or "merge")
    if duplicate_policy not in {"merge", "skip", "keep"}:
        duplicate_policy = "merge"
    config = {}
    if feed_type in {"url", "api_pull"}:
        config = {
            "url": str(form.get("url", "") or "").strip(),
            "filename_hint": str(form.get("filename_hint", "") or "").strip(),
        }
        if feed_type == "api_pull":
            config["data_path"] = str(form.get("data_path", "") or "").strip()
        if not config["url"]:
            raise ValueError("Feed URL is required")
    elif feed_type == "sftp":
        try:
            port = int(str(form.get("port", "22") or "22"))
        except Exception:
            raise ValueError("SFTP port must be a number")
        config = {
            "host": str(form.get("host", "") or "").strip(),
            "port": port,
            "username": str(form.get("username", "") or "").strip(),
            "remote_path": str(form.get("remote_path", "") or "").strip(),
            "host_key_sha256": str(form.get("host_key_sha256", "") or "").strip(),
            "filename_hint": str(form.get("filename_hint", "") or "").strip(),
        }
        if not config["host"] or not config["username"] or not config["remote_path"]:
            raise ValueError("SFTP host, username and remote path are required")
    mapping = mapping_from_form(form)
    return name, feed_type, schedule_minutes, import_mode, duplicate_policy, config, mapping


def _feed_secret_values(form, existing=None):
    old = feed_secrets(existing) if existing else {}
    for key in ("bearer_token", "header_name", "header_value", "password", "private_key", "private_key_passphrase"):
        value = str(form.get(key, "") or "")
        if value:
            old[key] = value
    return old


@app.get("/inventory-feeds/new", response_class=HTMLResponse)
def inventory_feed_new_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/inventory-feeds/new", status_code=303)
    if user["role"] != "supplier" or not can(user, "manage_inventory"):
        raise HTTPException(status_code=403)
    return render(request, "inventory_feed_form.html", feed=None, fields=CANONICAL_FIELDS, field_labels=FIELD_LABELS)


@app.post("/inventory-feeds/new")
async def inventory_feed_new(request: Request):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    try:
        name, feed_type, schedule_minutes, import_mode, duplicate_policy, config, mapping = _feed_form_values(form)
        secret_values = _feed_secret_values(form)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inventory-feeds/new", status_code=303)
    ingest_hash = ""
    if feed_type in {"api_push", "email"}:
        raw_token = make_ingest_token()
        secret_values["inbound_token"] = raw_token
        ingest_hash = token_hash(raw_token)
    next_run = now_iso() if feed_type in {"url", "api_pull", "sftp"} else None
    with db() as conn:
        ok, current, limit = check_limit(conn, user["company_id"], "feeds", 1)
        if not ok:
            flash(request, f"Your plan allows {limit} active automated feed(s). Upgrade before adding another.", "error")
            return RedirectResponse("/billing", status_code=303)
        feed_id = insert_id(
            conn,
            """INSERT INTO inventory_feeds(company_id,name,feed_type,enabled,schedule_minutes,next_run_at,config_json,secret_blob,mapping_json,import_mode,duplicate_policy,ingest_token_hash,created_by_user_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["company_id"], name, feed_type, 1, schedule_minutes, next_run, json.dumps(config), encrypt_secret_dict(secret_values), json.dumps(mapping), import_mode, duplicate_policy, ingest_hash, user["id"], now_iso(), now_iso()),
        )
        audit(conn, request, user, "inventory_feed.created", "inventory_feed", feed_id, {"name": name, "feed_type": feed_type})
    flash(request, "Inventory feed created.")
    return RedirectResponse("/inventory-feeds", status_code=303)


@app.get("/inventory-feeds/{feed_id}/edit", response_class=HTMLResponse)
def inventory_feed_edit_page(request: Request, feed_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/inventory-feeds/{feed_id}/edit", status_code=303)
    if user["role"] != "supplier" or not can(user, "manage_inventory"):
        raise HTTPException(status_code=403)
    with db() as conn:
        row = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404)
    return render(request, "inventory_feed_form.html", feed=_feed_view(row), fields=CANONICAL_FIELDS, field_labels=FIELD_LABELS)


@app.post("/inventory-feeds/{feed_id}/edit")
async def inventory_feed_edit(request: Request, feed_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    with db() as conn:
        existing = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
    if not existing:
        raise HTTPException(status_code=404)
    try:
        name, feed_type, schedule_minutes, import_mode, duplicate_policy, config, mapping = _feed_form_values(form, existing)
        secret_values = _feed_secret_values(form, existing)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/inventory-feeds/{feed_id}/edit", status_code=303)
    ingest_hash = existing["ingest_token_hash"] or ""
    if feed_type in {"api_push", "email"} and not secret_values.get("inbound_token"):
        raw_token = make_ingest_token()
        secret_values["inbound_token"] = raw_token
        ingest_hash = token_hash(raw_token)
    if feed_type not in {"api_push", "email"}:
        secret_values.pop("inbound_token", None)
        ingest_hash = ""
    with db() as conn:
        conn.execute(
            "UPDATE inventory_feeds SET name=?,feed_type=?,schedule_minutes=?,config_json=?,secret_blob=?,mapping_json=?,import_mode=?,duplicate_policy=?,ingest_token_hash=?,next_run_at=?,updated_at=? WHERE id=? AND company_id=?",
            (name, feed_type, schedule_minutes, json.dumps(config), encrypt_secret_dict(secret_values), json.dumps(mapping), import_mode, duplicate_policy, ingest_hash, now_iso() if feed_type in {"url", "api_pull", "sftp"} else None, now_iso(), feed_id, user["company_id"]),
        )
        audit(conn, request, user, "inventory_feed.updated", "inventory_feed", feed_id, {"name": name, "feed_type": feed_type})
    flash(request, "Feed settings updated.")
    return RedirectResponse("/inventory-feeds", status_code=303)


@app.post("/inventory-feeds/{feed_id}/run")
async def inventory_feed_run(request: Request, feed_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
    if not feed:
        raise HTTPException(status_code=404)
    if feed["feed_type"] not in {"url", "api_pull", "sftp"}:
        flash(request, "Push/email feeds run when data is received.", "error")
        return RedirectResponse("/inventory-feeds", status_code=303)
    try:
        job_id, changed_ids, counts = run_feed(feed_id, actor_user_id=user["id"])
        process_inventory_match_alerts(changed_ids)
        flash(request, f"Feed synced: {counts['inserted']} added, {counts['updated']} updated, {counts['archived']} retired.")
        return RedirectResponse(f"/inventory-imports/{job_id}", status_code=303)
    except Exception as exc:
        logger.exception("manual_feed_run_failed feed_id=%s", feed_id)
        mark_feed_failed(feed_id, str(exc))
        flash(request, f"Feed failed: {exc}", "error")
        return RedirectResponse("/inventory-feeds", status_code=303)


@app.post("/inventory-feeds/{feed_id}/toggle")
async def inventory_feed_toggle(request: Request, feed_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
        if not feed:
            raise HTTPException(status_code=404)
        new_value = 0 if feed["enabled"] else 1
        if new_value:
            ok, current, limit = check_limit(conn, user["company_id"], "feeds", 1)
            if not ok:
                flash(request, f"Your plan allows {limit} active automated feed(s).", "error")
                return RedirectResponse("/billing", status_code=303)
        conn.execute(
            "UPDATE inventory_feeds SET enabled=?,next_run_at=?,updated_at=? WHERE id=?",
            (new_value, now_iso() if new_value and feed["feed_type"] in {"url", "api_pull", "sftp"} else feed["next_run_at"], now_iso(), feed_id),
        )
        audit(conn, request, user, "inventory_feed.enabled" if new_value else "inventory_feed.disabled", "inventory_feed", feed_id)
    flash(request, "Feed enabled." if new_value else "Feed paused.")
    return RedirectResponse("/inventory-feeds", status_code=303)


@app.post("/inventory-feeds/{feed_id}/regenerate-token")
async def inventory_feed_regenerate_token(request: Request, feed_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
        if not feed or feed["feed_type"] not in {"api_push", "email"}:
            raise HTTPException(status_code=404)
        secret_values = feed_secrets(feed)
        raw_token = make_ingest_token()
        secret_values["inbound_token"] = raw_token
        conn.execute(
            "UPDATE inventory_feeds SET ingest_token_hash=?,secret_blob=?,updated_at=? WHERE id=?",
            (token_hash(raw_token), encrypt_secret_dict(secret_values), now_iso(), feed_id),
        )
        audit(conn, request, user, "inventory_feed.token_regenerated", "inventory_feed", feed_id)
    flash(request, "Inbound token regenerated. Update the sending system to the new endpoint/address.")
    return RedirectResponse("/inventory-feeds", status_code=303)


@app.post("/inventory-feeds/{feed_id}/delete")
async def inventory_feed_delete(request: Request, feed_id: int):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user = _supplier_inventory_user(request)
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=? AND company_id=?", (feed_id, user["company_id"])).fetchone()
        if not feed:
            raise HTTPException(status_code=404)
        now = now_iso()
        conn.execute(
            "UPDATE inventory SET active=0,archived_at=?,updated_at=?,updated_by_user_id=?,source_feed_id=NULL WHERE company_id=? AND source_feed_id=? AND active=1 AND deleted_at IS NULL",
            (now, now, user["id"], user["company_id"], feed_id),
        )
        conn.execute("DELETE FROM inventory_feeds WHERE id=?", (feed_id,))
        audit(conn, request, user, "inventory_feed.deleted", "inventory_feed", feed_id, {"name": feed["name"]})
    flash(request, "Feed deleted; inventory sourced by it was archived.")
    return RedirectResponse("/inventory-feeds", status_code=303)


async def _run_inbound_feed(feed, request: Request, source_type: str):
    mapping = json.loads(feed["mapping_json"] or "{}")
    content_type = request.headers.get("content-type", "")
    raw_rows = None
    filename = "inbound.csv"
    raw_file = None
    if content_type.startswith("application/json"):
        body = await request.body()
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Payload is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise ValueError("JSON payload is invalid.")
        if isinstance(payload, dict) and isinstance(payload.get("attachments"), list) and payload.get("attachments"):
            attachment = payload["attachments"][0]
            filename = str(attachment.get("filename") or "inventory.csv")
            try:
                raw_file = base64.b64decode(str(attachment.get("content_base64") or ""), validate=True)
            except Exception:
                raise ValueError("Attachment content_base64 is invalid.")
        else:
            raw_rows = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(raw_rows, list) or not all(isinstance(r, dict) for r in raw_rows):
                raise ValueError("JSON payload must be an array of inventory objects or {items:[...]}.")
    else:
        form = await request.form()
        upload = next((value for value in form.values() if hasattr(value, "filename") and getattr(value, "filename", "")), None)
        if upload is None:
            raise ValueError("Attach a CSV/XLSX inventory file.")
        filename = upload.filename or "inventory.csv"
        raw_file = await upload.read()
        if len(raw_file) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Attachment is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
    if raw_file is not None:
        parsed = read_tabular_file(filename, raw_file)
        raw_rows = parsed.rows
        mapping = mapping or guess_column_mapping(parsed.headers)
    else:
        headers = list(dict.fromkeys(str(k) for row in raw_rows[:100] for k in row.keys()))
        mapping = mapping or guess_column_mapping(headers)
    if not mapping.get("part_number"):
        raise ValueError("No part-number mapping is configured and it could not be auto-detected.")
    job_id, changed_ids, counts = run_import(
        company_id=feed["company_id"],
        user_id=None,
        feed_id=feed["id"],
        source_type=source_type,
        source_name=filename,
        raw_rows=raw_rows,
        mapping=mapping,
        import_mode=feed["import_mode"],
        duplicate_policy=feed["duplicate_policy"],
        default_location=feed["company_location"] or "",
    )
    with db() as conn:
        conn.execute("UPDATE inventory_feeds SET last_run_at=?,last_status='success',updated_at=? WHERE id=?", (now_iso(), now_iso(), feed["id"]))
    process_inventory_match_alerts(changed_ids)
    return job_id, counts


@app.post("/api/inventory-feed/{token}")
async def inventory_api_push(request: Request, token: str):
    feed = inbound_feed_by_token(token, ("api_push",))
    if not feed:
        raise HTTPException(status_code=404)
    try:
        job_id, counts = await _run_inbound_feed(feed, request, "api_push")
        return JSONResponse({"ok": True, "job_id": job_id, "counts": counts})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("api_push_import_failed feed_id=%s", feed["id"])
        mark_feed_failed(feed["id"], str(exc))
        return JSONResponse({"ok": False, "error": "Import failed"}, status_code=500)


@app.post("/api/inventory-email/{token}")
async def inventory_email_webhook(request: Request, token: str):
    feed = inbound_feed_by_token(token, ("email",))
    if not feed:
        raise HTTPException(status_code=404)
    try:
        job_id, counts = await _run_inbound_feed(feed, request, "email")
        return JSONResponse({"ok": True, "job_id": job_id, "counts": counts})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("email_import_failed feed_id=%s", feed["id"])
        mark_feed_failed(feed["id"], str(exc))
        return JSONResponse({"ok": False, "error": "Import failed"}, status_code=500)


@app.get("/inventory/{item_id}/edit", response_class=HTMLResponse)
def inventory_edit_page(request: Request, item_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/inventory/{item_id}/edit", status_code=303)
    if user["role"] != "supplier" or not can(user, "manage_inventory"):
        raise HTTPException(status_code=403)
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
    if not item: raise HTTPException(status_code=404)
    return render(request, "inventory_edit.html", item=item)


@app.post("/inventory/{item_id}/edit")
async def inventory_edit(request: Request, item_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier": raise HTTPException(status_code=403)
    brand = str(form.get("brand", "")).strip(); part = str(form.get("part_number", "")).strip()
    description = str(form.get("description", "")).strip(); condition = str(form.get("condition", "Not stated")).strip() or "Not stated"
    location = str(form.get("location", "")).strip()
    try: qty = max(0, int(str(form.get("quantity", "0"))))
    except Exception: qty = 0
    if not part:
        flash(request, "Part number is required.", "error"); return RedirectResponse(f"/inventory/{item_id}/edit", status_code=303)
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        confirmed_at = now_iso()
        conn.execute("UPDATE inventory SET brand=?,part_number=?,normalized_part=?,description=?,condition=?,quantity=?,location=?,last_confirmed_at=?,updated_at=?,updated_by_user_id=? WHERE id=?", (brand, part, normalize(part), description, condition, qty, location, confirmed_at, confirmed_at, user["id"], item_id))
        audit(conn, request, user, "inventory.edited", "inventory", item_id, {"part_number": part, "reconfirmed": True})
    link_inventory_ids([item_id])
    process_inventory_match_alerts([item_id])
    flash(request, "Inventory line updated, catalogue linkage refreshed and stock freshness reconfirmed.")
    return RedirectResponse("/inventory", status_code=303)


@app.post("/inventory/{item_id}/archive")
async def inventory_archive(request: Request, item_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        conn.execute("UPDATE inventory SET active=0,archived_at=?,updated_at=?,updated_by_user_id=? WHERE id=?", (now_iso(), now_iso(), user["id"], item_id))
        audit(conn, request, user, "inventory.archived", "inventory", item_id, {"part_number": item["part_number"]})
    flash(request, "Inventory line archived and removed from search.")
    return RedirectResponse("/inventory", status_code=303)


@app.post("/inventory/{item_id}/restore")
async def inventory_restore(request: Request, item_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        confirmed_at = now_iso()
        conn.execute("UPDATE inventory SET active=1,archived_at=NULL,last_confirmed_at=?,updated_at=?,updated_by_user_id=? WHERE id=?", (confirmed_at, confirmed_at, user["id"], item_id))
        audit(conn, request, user, "inventory.restored", "inventory", item_id, {"part_number": item["part_number"], "reconfirmed": True})
    process_inventory_match_alerts([item_id])
    flash(request, "Inventory line restored and reconfirmed.")
    return RedirectResponse("/inventory?view=archived", status_code=303)


@app.post("/inventory/{item_id}/delete")
async def inventory_delete(request: Request, item_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        conn.execute("UPDATE inventory SET active=0,deleted_at=?,updated_at=?,updated_by_user_id=? WHERE id=?", (now_iso(), now_iso(), user["id"], item_id))
        audit(conn, request, user, "inventory.deleted", "inventory", item_id, {"part_number": item["part_number"]})
    flash(request, "Inventory line deleted. The audit record is retained.")
    return RedirectResponse("/inventory", status_code=303)


@app.post("/inventory/{item_id}/confirm")
async def inventory_confirm(request: Request, item_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier": raise HTTPException(status_code=403)
    confirmed_at = now_iso()
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND active=1 AND deleted_at IS NULL", (item_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        conn.execute("UPDATE inventory SET last_confirmed_at=?,updated_at=?,updated_by_user_id=? WHERE id=?", (confirmed_at, confirmed_at, user["id"], item_id))
        audit(conn, request, user, "inventory.confirmed", "inventory", item_id, {"part_number": item["part_number"]})
    process_inventory_match_alerts([item_id])
    flash(request, f"{item['part_number']} reconfirmed as in stock.")
    return RedirectResponse("/inventory", status_code=303)


@app.post("/inventory/confirm-all")
async def inventory_confirm_all(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier": raise HTTPException(status_code=403)
    confirmed_at = now_iso()
    with db() as conn:
        rows = conn.execute("SELECT id FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL AND quantity>0", (user["company_id"],)).fetchall()
        ids = [r["id"] for r in rows]
        conn.execute("UPDATE inventory SET last_confirmed_at=?,updated_at=?,updated_by_user_id=? WHERE company_id=? AND active=1 AND deleted_at IS NULL AND quantity>0", (confirmed_at, confirmed_at, user["id"], user["company_id"]))
        audit(conn, request, user, "inventory.confirmed_all", "inventory", "bulk", {"rows": len(ids)})
    process_inventory_match_alerts(ids)
    flash(request, f"Reconfirmed {len(ids):,} active stock lines.")
    return RedirectResponse("/inventory", status_code=303)


# Backwards-compatible old MVP endpoint.
@app.post("/inventory/{item_id}/deactivate")
async def inventory_deactivate(request: Request, item_id: int):
    return await inventory_archive(request, item_id)


# ---------- RFQs ----------
@app.post("/rfqs")
async def create_rfq(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "trade")
    if user["role"] == "admin": raise HTTPException(status_code=403)
    inventory_ids = list(dict.fromkeys(int(x) for x in form.getlist("inventory_ids") if str(x).isdigit()))[:20]
    part_number = str(form.get("part_number", "")).strip()
    try: quantity = max(1, int(str(form.get("quantity", "1"))))
    except Exception: quantity = 1
    required_by = str(form.get("required_by", "")).strip()
    delivery_location = str(form.get("delivery_location", "")).strip()
    notes = str(form.get("notes", "")).strip()
    if not inventory_ids or not part_number:
        flash(request, "Choose at least one supplier result before sending the RFQ.", "error")
        return RedirectResponse("/", status_code=303)
    placeholders = ",".join("?" for _ in inventory_ids)
    with db() as conn:
        rows = conn.execute(
            f"""SELECT i.id,i.company_id,i.part_number,c.name AS supplier_name
            FROM inventory i JOIN companies c ON c.id=i.company_id JOIN billing_subscriptions bs ON bs.company_id=c.id
            WHERE i.id IN ({placeholders}) AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0
              AND i.last_confirmed_at>=? AND c.active=1 AND c.verified=1
              AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))""",
            tuple(inventory_ids + [freshness_cutoff_iso(), now_iso()]),
        ).fetchall()
        if not rows:
            flash(request, "Those inventory results are no longer current or available.", "error")
            return RedirectResponse("/", status_code=303)
        created_at = now_iso()
        rfq_id = insert_id(conn, "INSERT INTO rfqs(buyer_user_id,part_number,normalized_part,quantity,required_by,delivery_location,notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], part_number, normalize(part_number), quantity, required_by, delivery_location, notes, "open", created_at))
        record_demand_event(conn, "rfq", company_id=user["company_id"], query=part_number, quantity=quantity, source_id=rfq_id, created_at=created_at)
        seen = set(); recipients = []
        for row in rows:
            if row["company_id"] in seen or row["company_id"] == user["company_id"]: continue
            seen.add(row["company_id"]); recipients.append(row)
            conn.execute("INSERT INTO rfq_recipients(rfq_id,supplier_company_id,inventory_id,status) VALUES(?,?,?,?)", (rfq_id,row["company_id"],row["id"],"sent"))
        supplier_users = []
        if seen:
            supplier_users = conn.execute(f"SELECT u.email,c.name FROM users u JOIN companies c ON c.id=u.company_id WHERE u.active=1 AND u.email_verified=1 AND u.role='supplier' AND u.company_id IN ({','.join('?' for _ in seen)}) AND u.company_role IN ('owner','admin','sales')", list(seen)).fetchall()
        audit(conn, request, user, "rfq.created", "rfq", rfq_id, {"part_number": part_number, "supplier_count": len(recipients)})
    for su in supplier_users:
        send_email(su["email"], f"New RFQ #{rfq_id}: {part_number}", f"A buyer has sent an RFQ for {quantity} x {part_number}.\nRequired by: {required_by or 'Not specified'}\nDelivery: {delivery_location or 'Not specified'}\nNotes: {notes or 'None'}\n\n{PUBLIC_BASE_URL}/rfqs/{rfq_id}")
    flash(request, f"RFQ #{rfq_id} sent to {len(recipients)} supplier{'s' if len(recipients) != 1 else ''}.")
    return RedirectResponse(f"/rfqs/{rfq_id}", status_code=303)


@app.get("/rfqs", response_class=HTMLResponse)
def rfqs_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/rfqs", status_code=303)
    with db() as conn:
        if user["role"] == "supplier":
            inbound = [dict(r) for r in conn.execute("""SELECT rr.id AS recipient_id,rr.status AS recipient_status,rr.quoted_price,rr.quoted_currency,r.*,u.name AS buyer_name,c.name AS buyer_company
                FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id JOIN users u ON u.id=r.buyer_user_id JOIN companies c ON c.id=u.company_id
                WHERE rr.supplier_company_id=? ORDER BY r.created_at DESC""", (user["company_id"],)).fetchall()]
            for row in inbound: row["direction"]="Inbound"
            outbound = [dict(r) for r in conn.execute("""SELECT r.*,r.status AS recipient_status,c.name AS buyer_company
                FROM rfqs r JOIN users u ON u.id=r.buyer_user_id JOIN companies c ON c.id=u.company_id
                WHERE u.company_id=? ORDER BY r.created_at DESC""", (user["company_id"],)).fetchall()]
            for row in outbound: row["direction"]="Outbound"
            rows = sorted(inbound+outbound,key=lambda r:r["created_at"],reverse=True)
        elif user["role"] == "admin":
            rows = conn.execute("SELECT r.*,u.name AS buyer_name,c.name AS buyer_company FROM rfqs r JOIN users u ON u.id=r.buyer_user_id JOIN companies c ON c.id=u.company_id ORDER BY r.created_at DESC").fetchall()
        else:
            rows = conn.execute("""SELECT r.* FROM rfqs r JOIN users u ON u.id=r.buyer_user_id
                WHERE u.company_id=? ORDER BY r.created_at DESC""", (user["company_id"],)).fetchall()
    return render(request,"rfqs.html",rows=rows)


@app.get("/rfqs/{rfq_id}", response_class=HTMLResponse)
def rfq_detail(request: Request, rfq_id: int, recipient: Optional[int] = None):
    user=current_user(request)
    if not user: return RedirectResponse(f"/login?next=/rfqs/{rfq_id}",status_code=303)
    with db() as conn:
        rfq=conn.execute("""SELECT r.*,u.name AS buyer_name,u.email AS buyer_email,u.company_id AS buyer_company_id,c.name AS buyer_company
            FROM rfqs r JOIN users u ON u.id=r.buyer_user_id JOIN companies c ON c.id=u.company_id WHERE r.id=?""",(rfq_id,)).fetchone()
        if not rfq: raise HTTPException(status_code=404)
        is_buyer_company = rfq["buyer_company_id"] == user["company_id"]
        raw_recipients=conn.execute("""SELECT rr.*,c.name AS supplier_name,i.part_number AS inventory_part,i.condition,i.quantity AS inventory_quantity,i.last_confirmed_at
            FROM rfq_recipients rr JOIN companies c ON c.id=rr.supplier_company_id LEFT JOIN inventory i ON i.id=rr.inventory_id
            WHERE rr.rfq_id=? ORDER BY CASE rr.status WHEN 'accepted' THEN 0 WHEN 'quoted' THEN 1 WHEN 'viewed' THEN 2 WHEN 'sent' THEN 3 ELSE 4 END, rr.quoted_currency, rr.quoted_price, c.name""",(rfq_id,)).fetchall()
        recipients=[]
        for r in raw_recipients:
            item=dict(r)
            created=parse_iso(rfq["created_at"]); responded=parse_iso(item.get("responded_at"))
            item["response_hours"] = round((responded-created).total_seconds()/3600,1) if created and responded and responded>=created else None
            item["trust"] = supplier_trust_metrics(conn, item["supplier_company_id"])
            recipients.append(item)
        my_recipient=None
        if user["role"]=="supplier" and not is_buyer_company:
            my_recipient=conn.execute("SELECT * FROM rfq_recipients WHERE rfq_id=? AND supplier_company_id=?",(rfq_id,user["company_id"])).fetchone()
            if not my_recipient: raise HTTPException(status_code=403)
            if my_recipient["status"]=="sent":
                conn.execute("UPDATE rfq_recipients SET status='viewed' WHERE id=?",(my_recipient["id"],))
                my_recipient=conn.execute("SELECT * FROM rfq_recipients WHERE id=?",(my_recipient["id"],)).fetchone()
        elif not is_buyer_company and user["role"] != "admin":
            raise HTTPException(status_code=403)

        selected_recipient = None
        if is_buyer_company or user["role"] == "admin":
            if recipient:
                selected_recipient = next((r for r in recipients if r["id"] == recipient), None)
            if selected_recipient is None and recipients:
                selected_recipient = next((r for r in recipients if r["status"] in {"accepted","quoted"}), recipients[0])
        elif my_recipient:
            selected_recipient = next((r for r in recipients if r["id"] == my_recipient["id"]), dict(my_recipient))
        messages=[]
        if selected_recipient:
            messages=conn.execute("""SELECT m.*,u.name AS sender_name,c.name AS sender_company,u.company_id AS sender_company_id
                FROM rfq_messages m JOIN users u ON u.id=m.sender_user_id JOIN companies c ON c.id=u.company_id
                WHERE m.recipient_id=? ORDER BY m.created_at""",(selected_recipient["id"],)).fetchall()
        can_respond=user["role"]=="supplier" and my_recipient is not None and not is_buyer_company and can(user,"trade") and user["email_verified"] and user["company_verified"] and commercial_access(conn,user["company_id"]) and rfq["status"]=="open" and my_recipient["status"] not in {"accepted","not_selected"}
        can_accept=is_buyer_company and can(user,"trade") and user["email_verified"] and rfq["status"]=="open"
        can_message=(is_buyer_company or my_recipient is not None) and user["email_verified"] and (can(user,"trade") or user["company_role"] in {"owner","admin"})
    return render(request,"rfq_detail.html",rfq=rfq,recipients=recipients,my_recipient=my_recipient,can_respond=can_respond,is_owner=is_buyer_company,selected_recipient=selected_recipient,messages=messages,can_accept=can_accept,can_message=can_message)


@app.post("/rfqs/{rfq_id}/respond")
async def rfq_respond(request: Request, rfq_id: int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_permission(request,"trade")
    if user["role"]!="supplier" or not user["company_verified"]: raise HTTPException(status_code=403)
    with db() as access_conn:
        if not commercial_access(access_conn,user["company_id"]):
            flash(request,"Supplier access is paused. Choose a plan in Billing before responding to RFQs.","error")
            return RedirectResponse("/billing",status_code=303)
    status=str(form.get("status","quoted")); status=status if status in {"quoted","declined"} else "quoted"
    message=str(form.get("supplier_message","" )).strip(); currency=str(form.get("quoted_currency","GBP")).upper()[:3]
    price_raw=str(form.get("quoted_price","" )).strip(); price=None
    if status == "quoted" and not price_raw:
        flash(request,"Enter a quote price or choose Unable to supply.","error"); return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
    if price_raw:
        try:
            price=float(price_raw)
            if not math.isfinite(price) or price <= 0: raise ValueError
        except ValueError:
            flash(request,"Quote price must be a positive number.","error")
            return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
    with db() as conn:
        rec=conn.execute("SELECT * FROM rfq_recipients WHERE rfq_id=? AND supplier_company_id=?",(rfq_id,user["company_id"])).fetchone()
        rfq=conn.execute("""SELECT r.*,u.email AS buyer_email,u.company_id AS buyer_company_id FROM rfqs r JOIN users u ON u.id=r.buyer_user_id WHERE r.id=?""",(rfq_id,)).fetchone()
        if not rec or not rfq or rfq["buyer_company_id"]==user["company_id"]: raise HTTPException(status_code=403)
        if rfq["status"] != "open" or rec["status"] in {"accepted","not_selected"}:
            flash(request,"This RFQ is already closed.","error"); return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
        responded_at=now_iso()
        conn.execute("UPDATE rfq_recipients SET status=?,quoted_price=?,quoted_currency=?,supplier_message=?,responded_at=? WHERE id=?",(status,price,currency,message,responded_at,rec["id"]))
        audit(conn,request,user,"rfq.responded","rfq",rfq_id,{"status":status,"price":price,"currency":currency})
    body=f"{user['company_name']} has {status} your RFQ for {rfq['part_number']}."
    if price is not None: body+=f"\nPrice: {currency} {price:.2f}"
    if message: body+=f"\nQuote note: {message}"
    send_email(rfq["buyer_email"],f"Supplier response to RFQ #{rfq_id}",body+f"\n\n{PUBLIC_BASE_URL}/rfqs/{rfq_id}?recipient={rec['id']}")
    flash(request,"Response sent to the buyer."); return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)


@app.post("/rfqs/{rfq_id}/messages")
async def rfq_message(request: Request, rfq_id: int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_verified_email(request)
    if not can(user,"trade") and user["role"] != "admin": raise HTTPException(status_code=403)
    try: recipient_id=int(str(form.get("recipient_id","0")))
    except Exception: raise HTTPException(status_code=400)
    body=str(form.get("body","" )).strip()
    if not body:
        flash(request,"Enter a message before sending.","error"); return RedirectResponse(f"/rfqs/{rfq_id}?recipient={recipient_id}",status_code=303)
    if len(body)>4000:
        flash(request,"Messages are limited to 4,000 characters.","error"); return RedirectResponse(f"/rfqs/{rfq_id}?recipient={recipient_id}",status_code=303)
    with db() as conn:
        row=conn.execute("""SELECT rr.*,r.part_number,r.buyer_user_id,bu.company_id AS buyer_company_id,bu.email AS buyer_email,c.name AS supplier_name
            FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id JOIN users bu ON bu.id=r.buyer_user_id JOIN companies c ON c.id=rr.supplier_company_id
            WHERE rr.id=? AND rr.rfq_id=?""",(recipient_id,rfq_id)).fetchone()
        if not row: raise HTTPException(status_code=404)
        is_buyer_company=row["buyer_company_id"]==user["company_id"]
        is_supplier_company=row["supplier_company_id"]==user["company_id"]
        if not (is_buyer_company or is_supplier_company or user["role"]=="admin"): raise HTTPException(status_code=403)
        message_id=insert_id(conn,"INSERT INTO rfq_messages(recipient_id,sender_user_id,body,created_at) VALUES(?,?,?,?)",(recipient_id,user["id"],body,now_iso()))
        if is_buyer_company:
            recipients=conn.execute("SELECT email FROM users WHERE company_id=? AND active=1 AND email_verified=1 AND company_role IN ('owner','admin','sales')",(row["supplier_company_id"],)).fetchall()
        else:
            recipients=[{"email":row["buyer_email"]}]
        audit(conn,request,user,"rfq.message_sent","rfq_message",message_id,{"rfq_id":rfq_id,"recipient_id":recipient_id})
    for recipient_user in recipients:
        send_email(recipient_user["email"],f"New message on RFQ #{rfq_id}: {row['part_number']}",f"{user['name']} from {user['company_name']} wrote:\n\n{body}\n\nReply: {PUBLIC_BASE_URL}/rfqs/{rfq_id}?recipient={recipient_id}")
    flash(request,"Message sent.")
    return RedirectResponse(f"/rfqs/{rfq_id}?recipient={recipient_id}#conversation",status_code=303)


@app.post("/rfqs/{rfq_id}/accept/{recipient_id}")
async def accept_quote(request: Request, rfq_id: int, recipient_id: int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_permission(request,"trade")
    with db() as conn:
        rfq=conn.execute("""SELECT r.*,u.company_id AS buyer_company_id FROM rfqs r JOIN users u ON u.id=r.buyer_user_id WHERE r.id=?""",(rfq_id,)).fetchone()
        winner=conn.execute("""SELECT rr.*,c.name AS supplier_name FROM rfq_recipients rr JOIN companies c ON c.id=rr.supplier_company_id WHERE rr.id=? AND rr.rfq_id=?""",(recipient_id,rfq_id)).fetchone()
        if not rfq or not winner: raise HTTPException(status_code=404)
        if rfq["buyer_company_id"] != user["company_id"] or rfq["status"] != "open": raise HTTPException(status_code=403)
        if winner["status"] != "quoted" or winner["quoted_price"] is None:
            flash(request,"Only a submitted quote can be selected.","error"); return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
        accepted_at=now_iso()
        conn.execute("UPDATE rfq_recipients SET status='accepted',accepted_at=? WHERE id=?",(accepted_at,recipient_id))
        conn.execute("UPDATE rfq_recipients SET status='not_selected' WHERE rfq_id=? AND id!=? AND status NOT IN ('declined','accepted')",(rfq_id,recipient_id))
        conn.execute("UPDATE rfqs SET status='awarded' WHERE id=?",(rfq_id,))
        winner_users=conn.execute("SELECT email FROM users WHERE company_id=? AND active=1 AND email_verified=1 AND company_role IN ('owner','admin','sales')",(winner["supplier_company_id"],)).fetchall()
        loser_users=conn.execute("""SELECT DISTINCT u.email FROM rfq_recipients rr JOIN users u ON u.company_id=rr.supplier_company_id
            WHERE rr.rfq_id=? AND rr.id!=? AND u.active=1 AND u.email_verified=1 AND u.company_role IN ('owner','admin','sales')""",(rfq_id,recipient_id)).fetchall()
        audit(conn,request,user,"rfq.quote_selected","rfq",rfq_id,{"recipient_id":recipient_id,"supplier_company_id":winner["supplier_company_id"],"price":winner["quoted_price"],"currency":winner["quoted_currency"]})
        order_id, order_created = create_order_from_selected_quote(conn, rfq_id, recipient_id, user["id"])
        if order_created:
            queue_order_webhooks(conn, order_id, "order.created")
            audit(conn, request, user, "order.created", "order", order_id, {"rfq_id": rfq_id, "recipient_id": recipient_id})
    for recipient_user in winner_users:
        send_email(recipient_user["email"],f"Your quote was selected — RFQ #{rfq_id}",f"The buyer selected {winner['supplier_name']}'s quote of {winner['quoted_currency']} {winner['quoted_price']:.2f}.\n\nOrder record: {PUBLIC_BASE_URL}/orders/{order_id}\nRFQ: {PUBLIC_BASE_URL}/rfqs/{rfq_id}")
    for recipient_user in loser_users:
        send_email(recipient_user["email"],f"RFQ #{rfq_id} has been awarded",f"The buyer has selected another quote for RFQ #{rfq_id}. Thank you for responding.\n\n{PUBLIC_BASE_URL}/rfqs/{rfq_id}")
    flash(request,f"Selected {winner['supplier_name']}'s quote. Order #{order_id} is ready for your PO details.")
    return RedirectResponse(f"/orders/{order_id}",status_code=303)


# ---------- wanted ----------
@app.post("/wanted")
async def create_wanted(request: Request):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_permission(request,"trade")
    if user["role"]=="admin": raise HTTPException(status_code=403)
    query=str(form.get("query","")).strip(); notes=str(form.get("notes","")).strip()
    try: quantity=max(1,int(str(form.get("quantity","1"))))
    except Exception: quantity=1
    if not query: flash(request,"Enter the part you need.","error"); return RedirectResponse("/",status_code=303)
    with db() as conn:
        created_at=now_iso()
        wanted_id=insert_id(conn,"INSERT INTO wanted_requests(buyer_user_id,query,normalized_query,quantity,notes,status,created_at) VALUES(?,?,?,?,?,?,?)",(user["id"],query,normalize(query),quantity,notes,"open",created_at))
        record_demand_event(conn,"wanted",company_id=user["company_id"],query=query,quantity=quantity,source_id=wanted_id,created_at=created_at)
        suppliers=conn.execute("SELECT u.email FROM users u JOIN companies c ON c.id=u.company_id WHERE u.role='supplier' AND u.active=1 AND u.email_verified=1 AND u.company_role IN ('owner','admin','sales') AND c.verified=1 AND c.active=1 AND c.id!=?",(user["company_id"],)).fetchall()
        current_matches=search_inventory(conn,query,exclude_company_id=user["company_id"],limit=100)
        for item in current_matches:
            conn.execute("INSERT INTO wanted_matches(wanted_id,inventory_id,notified_at) VALUES(?,?,?)",(wanted_id,item["id"],now_iso()))
        audit(conn,request,user,"wanted.created","wanted",wanted_id,{"query":query,"quantity":quantity,"current_matches":len(current_matches)})
    for supplier in suppliers: send_email(supplier["email"],f"Wanted part #{wanted_id}: {query}",f"A buyer is looking for {quantity} x {query}.\nNotes: {notes or 'None'}\n\n{PUBLIC_BASE_URL}/wanted/{wanted_id}")
    flash(request,f"Wanted request #{wanted_id} sent to verified suppliers."); return RedirectResponse("/dashboard",status_code=303)


@app.get("/wanted", response_class=HTMLResponse)
def wanted_board(request: Request):
    user=current_user(request)
    if not user:return RedirectResponse("/login?next=/wanted",status_code=303)
    with db() as conn:
        if user["role"]=="supplier": rows=conn.execute("""SELECT w.*,c.name AS buyer_company,(SELECT COUNT(*) FROM wanted_responses wr WHERE wr.wanted_id=w.id) AS response_count FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id JOIN companies c ON c.id=u.company_id WHERE w.status='open' AND u.company_id!=? ORDER BY w.created_at DESC""",(user["company_id"],)).fetchall()
        elif user["role"]=="admin": rows=conn.execute("SELECT w.*,c.name AS buyer_company,(SELECT COUNT(*) FROM wanted_responses wr WHERE wr.wanted_id=w.id) AS response_count FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id JOIN companies c ON c.id=u.company_id ORDER BY w.created_at DESC").fetchall()
        else: rows=conn.execute("SELECT w.*,c.name AS buyer_company,(SELECT COUNT(*) FROM wanted_responses wr WHERE wr.wanted_id=w.id) AS response_count FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id JOIN companies c ON c.id=u.company_id WHERE u.company_id=? ORDER BY w.created_at DESC",(user["company_id"],)).fetchall()
    return render(request,"wanted.html",rows=rows)


@app.get("/wanted/{wanted_id}", response_class=HTMLResponse)
def wanted_detail(request: Request,wanted_id:int):
    user=current_user(request)
    if not user:return RedirectResponse(f"/login?next=/wanted/{wanted_id}",status_code=303)
    with db() as conn:
        wanted=conn.execute("SELECT w.*,u.id AS owner_user_id,u.company_id AS buyer_company_id,c.name AS buyer_company FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id JOIN companies c ON c.id=u.company_id WHERE w.id=?",(wanted_id,)).fetchone()
        if not wanted: raise HTTPException(status_code=404)
        is_owner=wanted["buyer_company_id"]==user["company_id"]
        if user["role"]=="buyer" and not is_owner: raise HTTPException(status_code=403)
        if user["role"]=="supplier" and is_owner: pass
        responses=conn.execute("SELECT wr.*,c.name AS supplier_company FROM wanted_responses wr JOIN users u ON u.id=wr.supplier_user_id JOIN companies c ON c.id=u.company_id WHERE wr.wanted_id=? ORDER BY wr.created_at DESC",(wanted_id,)).fetchall()
        matches=[]
        if is_owner or user["role"]=="admin":
            matches=conn.execute("""SELECT wm.notified_at,i.id AS inventory_id,i.brand,i.part_number,i.condition,i.quantity,i.location,i.last_confirmed_at,c.id AS supplier_id,c.name AS supplier_name
                FROM wanted_matches wm JOIN inventory i ON i.id=wm.inventory_id JOIN companies c ON c.id=i.company_id
                WHERE wm.wanted_id=? AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND i.last_confirmed_at>=?
                ORDER BY wm.notified_at DESC""",(wanted_id,freshness_cutoff_iso())).fetchall()
            matches=[m for m in matches if commercial_access(conn, int(m["supplier_id"]))]
    return render(request,"wanted_detail.html",wanted=wanted,responses=responses,is_owner=is_owner,matches=matches)


@app.post("/wanted/{wanted_id}/respond")
async def wanted_respond(request:Request,wanted_id:int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_permission(request,"trade")
    if user["role"]!="supplier" or not user["company_verified"]: raise HTTPException(status_code=403)
    message=str(form.get("message","")).strip()
    if not message: flash(request,"Add a short message for the buyer.","error"); return RedirectResponse(f"/wanted/{wanted_id}",status_code=303)
    with db() as conn:
        if not commercial_access(conn, user["company_id"]):
            flash(request, "Your supplier access is paused. Choose a plan or ask an administrator to extend your trial before responding to new buyer leads.", "error")
            return RedirectResponse("/billing", status_code=303)
        wanted=conn.execute("SELECT w.*,u.email AS buyer_email,u.company_id AS buyer_company_id FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id WHERE w.id=? AND w.status='open'",(wanted_id,)).fetchone()
        if not wanted or wanted["buyer_company_id"]==user["company_id"]: raise HTTPException(status_code=404)
        conn.execute("INSERT INTO wanted_responses(wanted_id,supplier_user_id,message,created_at) VALUES(?,?,?,?)",(wanted_id,user["id"],message,now_iso()))
        audit(conn,request,user,"wanted.responded","wanted",wanted_id)
    send_email(wanted["buyer_email"],f"Supplier response to wanted request #{wanted_id}",f"{user['company_name']} may be able to help with {wanted['query']}.\n\n{message}\n\n{PUBLIC_BASE_URL}/wanted/{wanted_id}")
    flash(request,"Response sent to the buyer."); return RedirectResponse(f"/wanted/{wanted_id}",status_code=303)



# ---------- Phase 7: procurement / order workflow ----------
def _order_party_emails(conn, company_id: int):
    return conn.execute(
        "SELECT email FROM users WHERE company_id=? AND active=1 AND email_verified=1 AND company_role IN ('owner','admin','sales')",
        (company_id,),
    ).fetchall()


def _order_access(request: Request, order_id: int, *, permission: str | None = None):
    user = require_verified_email(request)
    if permission and user["role"] != "admin" and not can(user, permission):
        raise HTTPException(status_code=403)
    with db() as conn:
        order = order_for_company(conn, order_id, user["company_id"], admin=user["role"] == "admin")
    if not order:
        raise HTTPException(status_code=404)
    return user, order


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, status: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/orders", status_code=303)
    with db() as conn:
        params = []
        where = "1=1"
        if user["role"] != "admin":
            where += " AND (o.buyer_company_id=? OR o.supplier_company_id=?)"
            params.extend([user["company_id"], user["company_id"]])
        if status in ORDER_STATUSES:
            where += " AND o.status=?"
            params.append(status)
        rows = conn.execute(
            f"""SELECT o.*,bc.name AS buyer_company,sc.name AS supplier_company
                FROM orders o JOIN companies bc ON bc.id=o.buyer_company_id JOIN companies sc ON sc.id=o.supplier_company_id
                WHERE {where} ORDER BY o.updated_at DESC,o.id DESC LIMIT 500""",
            tuple(params),
        ).fetchall()
    return render(request, "orders.html", rows=rows, selected_status=status, order_statuses=ORDER_STATUSES)


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail_page(request: Request, order_id: int):
    user, order = _order_access(request, order_id)
    with db() as conn:
        messages = conn.execute(
            """SELECT m.*,u.name AS sender_name,c.name AS sender_company
               FROM order_messages m LEFT JOIN users u ON u.id=m.sender_user_id LEFT JOIN companies c ON c.id=u.company_id
               WHERE m.order_id=? ORDER BY m.created_at""",
            (order_id,),
        ).fetchall()
        documents = conn.execute(
            """SELECT d.*,u.name AS uploader_name,c.name AS uploader_company
               FROM order_documents d LEFT JOIN users u ON u.id=d.uploaded_by_user_id LEFT JOIN companies c ON c.id=u.company_id
               WHERE d.order_id=? ORDER BY d.created_at DESC""",
            (order_id,),
        ).fetchall()
        events = conn.execute(
            """SELECT e.*,u.name AS actor_name,c.name AS actor_company
               FROM order_events e LEFT JOIN users u ON u.id=e.actor_user_id LEFT JOIN companies c ON c.id=u.company_id
               WHERE e.order_id=? ORDER BY e.created_at DESC""",
            (order_id,),
        ).fetchall()
    is_buyer = user["role"] != "admin" and user["company_id"] == order["buyer_company_id"]
    is_supplier = user["role"] != "admin" and user["company_id"] == order["supplier_company_id"]
    can_trade = user["role"] != "admin" and (can(user, "trade") or user["company_role"] in {"owner", "admin"})
    return render(request, "order_detail.html", order=order, messages=messages, documents=documents, events=events,
                  is_buyer=is_buyer, is_supplier=is_supplier, can_trade=can_trade,
                  order_statuses=ORDER_STATUSES, document_max_mb=ORDER_DOCUMENT_MAX_BYTES // (1024*1024))


@app.post("/orders/{order_id}/buyer-details")
async def order_buyer_details(request: Request, order_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user, order = _order_access(request, order_id, permission="trade")
    if user["role"] == "admin" or user["company_id"] != order["buyer_company_id"]:
        raise HTTPException(status_code=403)
    if order["status"] not in {"awaiting_buyer_po", "submitted"}:
        flash(request, "Buyer PO details are locked after the supplier acknowledges the order.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    po = str(form.get("buyer_po_number", "")).strip()[:120]
    reference = str(form.get("buyer_reference", "")).strip()[:160]
    address = str(form.get("delivery_address", "")).strip()[:1500]
    contact = str(form.get("delivery_contact", "")).strip()[:300]
    if not po:
        flash(request, "Add your PO number before submitting the order to the supplier.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    with db() as conn:
        conn.execute(
            "UPDATE orders SET buyer_po_number=?,buyer_reference=?,delivery_address=?,delivery_contact=?,status='submitted',updated_at=? WHERE id=?",
            (po, reference, address, contact, now_iso(), order_id),
        )
        record_order_event(conn, order_id, user["id"], "order.submitted", {"buyer_po_number": po})
        queue_order_webhooks(conn, order_id, "order.submitted")
        supplier_emails = _order_party_emails(conn, order["supplier_company_id"])
        audit(conn, request, user, "order.submitted", "order", order_id, {"buyer_po_number": po})
    for recipient in supplier_emails:
        send_email(recipient["email"], f"Order #{order_id} submitted · PO {po}", f"{user['company_name']} has submitted the order for {order['quantity']} × {order['part_number']}.\n\nReview: {PUBLIC_BASE_URL}/orders/{order_id}")
    flash(request, "PO details sent to the supplier.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/supplier-status")
async def order_supplier_status(request: Request, order_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user, order = _order_access(request, order_id, permission="trade")
    if user["role"] == "admin" or user["company_id"] != order["supplier_company_id"]:
        raise HTTPException(status_code=403)
    action = str(form.get("status", "")).strip()
    if action not in {"acknowledged", "processing", "dispatched", "delivered"}:
        raise HTTPException(status_code=400)
    allowed = {
        "acknowledged": {"submitted"},
        "processing": {"submitted", "acknowledged", "processing"},
        "dispatched": {"submitted", "acknowledged", "processing"},
        "delivered": {"dispatched"},
    }
    if order["status"] not in allowed[action]:
        flash(request, f"Cannot move an order from {order['status']} to {action}.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    supplier_ref = (str(form.get("supplier_order_reference", "")).strip()[:160] or order["supplier_order_reference"] or "")
    dispatch_date = (str(form.get("expected_dispatch_date", "")).strip()[:40] or order["expected_dispatch_date"] or "")
    carrier = (str(form.get("carrier", "")).strip()[:120] or order["carrier"] or "")
    tracking = (str(form.get("tracking_number", "")).strip()[:160] or order["tracking_number"] or "")
    raw_tracking_url = str(form.get("tracking_url", "")).strip() or (order["tracking_url"] or "")
    try:
        tracking_url = safe_external_url(raw_tracking_url) if raw_tracking_url else ""
    except ValueError as exc:
        flash(request, str(exc), "error"); return RedirectResponse(f"/orders/{order_id}", status_code=303)
    timestamps = {"acknowledged": "acknowledged_at", "dispatched": "dispatched_at", "delivered": "delivered_at"}
    with db() as conn:
        conn.execute(
            "UPDATE orders SET status=?,supplier_order_reference=?,expected_dispatch_date=?,carrier=?,tracking_number=?,tracking_url=?,updated_at=? WHERE id=?",
            (action, supplier_ref, dispatch_date, carrier, tracking, tracking_url, now_iso(), order_id),
        )
        if action in timestamps:
            conn.execute(f"UPDATE orders SET {timestamps[action]}=? WHERE id=?", (now_iso(), order_id))
        record_order_event(conn, order_id, user["id"], f"order.{action}", {"carrier": carrier, "tracking_number": tracking})
        queue_order_webhooks(conn, order_id, f"order.{action}")
        buyer_emails = _order_party_emails(conn, order["buyer_company_id"])
        audit(conn, request, user, f"order.{action}", "order", order_id, {"supplier_order_reference": supplier_ref})
    for recipient in buyer_emails:
        send_email(recipient["email"], f"Order #{order_id} {action}", f"{user['company_name']} updated order #{order_id} to {action}.\n\n{PUBLIC_BASE_URL}/orders/{order_id}")
    flash(request, f"Order marked {action}.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/messages")
async def order_message(request: Request, order_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user, order = _order_access(request, order_id)
    if user["role"] == "admin" or not (can(user, "trade") or user["company_role"] in {"owner", "admin"}):
        raise HTTPException(status_code=403)
    body = str(form.get("body", "")).strip()
    if not body or len(body) > 5000:
        flash(request, "Enter a message up to 5,000 characters.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    with db() as conn:
        message_id = insert_id(conn, "INSERT INTO order_messages(order_id,sender_user_id,body,created_at) VALUES(?,?,?,?)", (order_id, user["id"], body, now_iso()))
        record_order_event(conn, order_id, user["id"], "order.message_added", {"message_id": message_id})
        queue_order_webhooks(conn, order_id, "order.message_added")
        other_company = order["supplier_company_id"] if user["company_id"] == order["buyer_company_id"] else order["buyer_company_id"]
        recipients = _order_party_emails(conn, other_company)
        audit(conn, request, user, "order.message_added", "order_message", message_id, {"order_id": order_id})
    for recipient in recipients:
        send_email(recipient["email"], f"New message on order #{order_id}", f"{user['name']} from {user['company_name']} wrote:\n\n{body}\n\n{PUBLIC_BASE_URL}/orders/{order_id}")
    flash(request, "Order message sent.")
    return RedirectResponse(f"/orders/{order_id}#conversation", status_code=303)


@app.post("/orders/{order_id}/documents")
async def order_document_upload(request: Request, order_id: int, document: UploadFile = File(...), document_type: str = Form("other"), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user, order = _order_access(request, order_id)
    if user["role"] == "admin" or not (can(user, "trade") or user["company_role"] in {"owner", "admin"}):
        raise HTTPException(status_code=403)
    raw = await document.read(ORDER_DOCUMENT_MAX_BYTES + 1)
    try:
        saved = save_order_document(order_id, user["id"], document.filename or "document", raw, document_type)
    except ValueError as exc:
        flash(request, str(exc), "error"); return RedirectResponse(f"/orders/{order_id}", status_code=303)
    with db() as conn:
        record_order_event(conn, order_id, user["id"], "order.document_added", {"document_id": saved["id"], "filename": saved["original_filename"], "sha256": saved["sha256"]})
        queue_order_webhooks(conn, order_id, "order.document_added")
        audit(conn, request, user, "order.document_added", "order_document", saved["id"], {"order_id": order_id, "filename": saved["original_filename"], "sha256": saved["sha256"]})
    flash(request, "Document attached to the order.")
    return RedirectResponse(f"/orders/{order_id}#documents", status_code=303)


@app.get("/orders/{order_id}/documents/{document_id}")
def order_document_download(request: Request, order_id: int, document_id: int):
    user, order = _order_access(request, order_id)
    with db() as conn:
        doc = conn.execute("SELECT * FROM order_documents WHERE id=? AND order_id=?", (document_id, order_id)).fetchone()
    if not doc:
        raise HTTPException(status_code=404)
    path = document_path(doc["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document file is unavailable")
    return FileResponse(path=str(path), filename=doc["original_filename"], media_type=doc["content_type"], headers={"Cache-Control": "private, no-store"})


@app.post("/orders/{order_id}/cancel")
async def order_cancel(request: Request, order_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user, order = _order_access(request, order_id, permission="trade")
    if user["role"] == "admin":
        raise HTTPException(status_code=403)
    if order["status"] in {"delivered", "cancelled"}:
        flash(request, "This order can no longer be cancelled.", "error"); return RedirectResponse(f"/orders/{order_id}", status_code=303)
    if user["company_id"] == order["buyer_company_id"] and order["status"] == "dispatched":
        flash(request, "A dispatched order cannot be cancelled through the platform; contact the supplier directly.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    reason = str(form.get("reason", "")).strip()[:1000]
    if not reason:
        flash(request, "Give a cancellation reason.", "error"); return RedirectResponse(f"/orders/{order_id}", status_code=303)
    with db() as conn:
        conn.execute("UPDATE orders SET status='cancelled',cancellation_reason=?,cancelled_at=?,updated_at=? WHERE id=?", (reason, now_iso(), now_iso(), order_id))
        record_order_event(conn, order_id, user["id"], "order.cancelled", {"reason": reason})
        queue_order_webhooks(conn, order_id, "order.cancelled")
        other_company = order["supplier_company_id"] if user["company_id"] == order["buyer_company_id"] else order["buyer_company_id"]
        recipients = _order_party_emails(conn, other_company)
        audit(conn, request, user, "order.cancelled", "order", order_id, {"reason": reason})
    for recipient in recipients:
        send_email(recipient["email"], f"Order #{order_id} cancelled", f"{user['company_name']} cancelled order #{order_id}.\nReason: {reason}\n\n{PUBLIC_BASE_URL}/orders/{order_id}")
    flash(request, "Order cancelled.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.get("/integrations/webhooks", response_class=HTMLResponse)
def webhook_integrations_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/integrations/webhooks", status_code=303)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        endpoints = conn.execute("SELECT * FROM webhook_endpoints WHERE company_id=? ORDER BY created_at DESC", (user["company_id"],)).fetchall()
        deliveries = conn.execute("""SELECT d.*,e.name AS endpoint_name FROM webhook_deliveries d LEFT JOIN webhook_endpoints e ON e.id=d.endpoint_id
                                     WHERE d.company_id=? ORDER BY d.created_at DESC LIMIT 50""", (user["company_id"],)).fetchall()
    new_secret = request.session.pop("new_webhook_secret", None)
    return render(request, "webhook_integrations.html", endpoints=endpoints, deliveries=deliveries, event_types=sorted(WEBHOOK_EVENTS), new_secret=new_secret)


@app.post("/integrations/webhooks")
async def webhook_endpoint_create(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    name = str(form.get("name", "Order integration")).strip()[:120]
    url = str(form.get("url", "")).strip()
    event_types = set(form.getlist("event_types"))
    try:
        with db() as conn:
            endpoint_id, secret = create_webhook_endpoint(conn, user["company_id"], user["id"], name, url, event_types)
            audit(conn, request, user, "webhook.created", "webhook_endpoint", endpoint_id, {"name": name, "events": sorted(event_types)})
    except ValueError as exc:
        flash(request, str(exc), "error"); return RedirectResponse("/integrations/webhooks", status_code=303)
    request.session["new_webhook_secret"] = secret
    flash(request, "Webhook endpoint created. Copy the signing secret now.")
    return RedirectResponse("/integrations/webhooks", status_code=303)


@app.post("/integrations/webhooks/{endpoint_id}/toggle")
async def webhook_endpoint_toggle(request: Request, endpoint_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        row = conn.execute("SELECT * FROM webhook_endpoints WHERE id=? AND company_id=?", (endpoint_id, user["company_id"])).fetchone()
        if not row: raise HTTPException(status_code=404)
        active = 0 if row["active"] else 1
        conn.execute("UPDATE webhook_endpoints SET active=?,updated_at=? WHERE id=?", (active, now_iso(), endpoint_id))
        audit(conn, request, user, "webhook.toggled", "webhook_endpoint", endpoint_id, {"active": active})
    flash(request, "Webhook endpoint updated.")
    return RedirectResponse("/integrations/webhooks", status_code=303)


@app.post("/integrations/webhooks/{endpoint_id}/rotate")
async def webhook_endpoint_rotate(request: Request, endpoint_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        secret = rotate_webhook_secret(conn, user["company_id"], endpoint_id)
        if not secret: raise HTTPException(status_code=404)
        audit(conn, request, user, "webhook.secret_rotated", "webhook_endpoint", endpoint_id)
    request.session["new_webhook_secret"] = secret
    flash(request, "Signing secret rotated. The previous secret is invalid immediately.")
    return RedirectResponse("/integrations/webhooks", status_code=303)


@app.post("/integrations/webhooks/{endpoint_id}/delete")
async def webhook_endpoint_delete(request: Request, endpoint_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        row = conn.execute("SELECT id FROM webhook_endpoints WHERE id=? AND company_id=?", (endpoint_id, user["company_id"])).fetchone()
        if not row: raise HTTPException(status_code=404)
        conn.execute("DELETE FROM webhook_endpoints WHERE id=?", (endpoint_id,))
        audit(conn, request, user, "webhook.deleted", "webhook_endpoint", endpoint_id)
    flash(request, "Webhook endpoint deleted.")
    return RedirectResponse("/integrations/webhooks", status_code=303)




# ---------- Phase 6: market intelligence / ERP API ----------
def _api_raw_key(request: Request) -> str:
    bearer = request.headers.get("authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _require_api_principal(request: Request, scope: str) -> dict:
    raw = _api_raw_key(request)
    with db() as conn:
        key, error = authenticate_api_key(conn, raw, scope, request.url.path)
    if error:
        status = 429 if error == "rate_limited" else (403 if error in {"missing_scope", "commercial_access_paused", "plan_no_api_access"} else 401)
        raise HTTPException(status_code=status, detail={
            "error": error,
            "message": {
                "rate_limited": "API rate limit reached for the current rolling hour.",
                "missing_scope": "This API key does not include the required scope.",
                "commercial_access_paused": "Commercial access is paused for this company.",
                "plan_no_api_access": "The current plan does not include API access.",
                "expired_key": "This API key has expired.",
            }.get(error, "Invalid API key."),
        })
    return key


@app.get("/market-intelligence", response_class=HTMLResponse)
def market_intelligence_page(request: Request, days: int = 90, q: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/market-intelligence", status_code=303)
    if user["role"] not in {"supplier", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        is_admin = user["role"] == "admin"
        if is_admin:
            plan = {"network_intelligence": True, "analytics_days": 3650, "name": "Platform admin"}
            allowed_days = [30, 90, 365]
            max_days = 3650
        else:
            plan = effective_plan(conn, user["company_id"])
            max_days = int(plan.get("analytics_days", 0) or 0)
            allowed_days = [d for d in [30, 90, 365] if d <= max_days]
            if max_days and max_days not in allowed_days:
                allowed_days.append(max_days)
        if not is_admin and (not commercial_access(conn, user["company_id"]) or not plan.get("network_intelligence")):
            return render(request, "market_intelligence.html", intelligence=None, opportunities=[], demand=[], part=None,
                          days=days, q=q, allowed_days=allowed_days, plan=plan,
                          privacy={"demand_events": MIN_DEMAND_EVENTS, "buyer_companies": MIN_BUYER_COMPANIES,
                                   "quote_samples": MIN_QUOTE_SAMPLES, "quote_suppliers": MIN_QUOTE_SUPPLIERS, "quote_buyer_companies": MIN_QUOTE_BUYER_COMPANIES, "max_contributor_share": MAX_CONTRIBUTOR_SHARE})
        days = max(1, min(int(days or 90), max_days or 90))
        summary = network_summary(conn, days, admin=is_admin)
        demand = network_demand(conn, days, admin=is_admin, limit=30)
        opportunities = supplier_opportunities(conn, user["company_id"], days, admin=is_admin, limit=15) if not is_admin else demand[:15]
        part = part_intelligence(conn, q, days, admin=is_admin) if q.strip() else None
    return render(request, "market_intelligence.html", intelligence=summary, opportunities=opportunities, demand=demand,
                  part=part, days=days, q=q, allowed_days=allowed_days, plan=plan,
                  privacy={"demand_events": MIN_DEMAND_EVENTS, "buyer_companies": MIN_BUYER_COMPANIES,
                           "quote_samples": MIN_QUOTE_SAMPLES, "quote_suppliers": MIN_QUOTE_SUPPLIERS, "quote_buyer_companies": MIN_QUOTE_BUYER_COMPANIES, "max_contributor_share": MAX_CONTRIBUTOR_SHARE})


@app.get("/integrations/api", response_class=HTMLResponse)
def api_integrations_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/integrations/api", status_code=303)
    if user["role"] == "admin":
        raise HTTPException(status_code=403)
    with db() as conn:
        plan = effective_plan(conn, user["company_id"])
        keys = api_key_rows(conn, user["company_id"])
        usage = api_usage_summary(conn, user["company_id"], 30)
        active_count = sum(1 for key in keys if key["active"])
    new_key = request.session.pop("new_api_key", None)
    return render(request, "api_integrations.html", keys=keys, usage=usage, plan=plan, active_count=active_count,
                  new_key=new_key, api_scopes=sorted(API_SCOPES))


@app.post("/integrations/api/keys")
async def api_key_create(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    name = str(form.get("name", "ERP integration")).strip()[:100] or "ERP integration"
    requested_scopes = {scope for scope in form.getlist("scopes") if scope in API_SCOPES}
    if user["role"] == "buyer":
        requested_scopes &= {"orders:read", "orders:write"}
        if not requested_scopes:
            requested_scopes = {"orders:read", "orders:write"}
    elif not requested_scopes:
        requested_scopes = {"inventory:read", "catalog:read"}
    with db() as conn:
        if not commercial_access(conn, user["company_id"]):
            flash(request, "Commercial access is paused. Choose or reactivate a plan before creating API keys.", "error")
            return RedirectResponse("/billing", status_code=303)
        plan = effective_plan(conn, user["company_id"])
        if int(plan.get("api_key_limit", 0) or 0) <= 0:
            flash(request, "This plan does not include API/ERP access.", "error")
            return RedirectResponse("/billing", status_code=303)
        ok, current, limit = check_limit(conn, user["company_id"], "api_keys", 1)
        if not ok:
            flash(request, f"Your plan allows {limit} active API key(s). Revoke an old key or upgrade.", "error")
            return RedirectResponse("/integrations/api", status_code=303)
        raw, key_id = create_api_key(conn, user["company_id"], user["id"], name, requested_scopes,
                                     rate_limit=int(plan.get("api_rate_per_hour", 1000) or 1000))
        audit(conn, request, user, "api_key.created", "api_key", key_id, {"name": name, "scopes": sorted(requested_scopes)})
    request.session["new_api_key"] = raw
    flash(request, "API key created. Copy it now — Controls Exchange stores only its hash.")
    return RedirectResponse("/integrations/api", status_code=303)


@app.post("/integrations/api/keys/{key_id}/revoke")
async def api_key_revoke(request: Request, key_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    if user["role"] == "admin" or user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403)
    with db() as conn:
        if not revoke_api_key(conn, user["company_id"], key_id):
            raise HTTPException(status_code=404)
        audit(conn, request, user, "api_key.revoked", "api_key", key_id)
    flash(request, "API key revoked immediately.")
    return RedirectResponse("/integrations/api", status_code=303)


@app.get("/v1/inventory/search")
def external_inventory_search(request: Request, q: str = "", brand: str = "", condition: str = "", limit: int = 50):
    key = _require_api_principal(request, "inventory:read")
    q = q.strip()
    if len(normalize(q)) < 2:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": "q must contain at least two searchable characters."})
    limit = max(1, min(int(limit or 50), 100))
    with db() as conn:
        ranked = search_inventory(conn, q, brand, condition, exclude_company_id=int(key["company_id"]), limit=limit)
        record_demand_event(conn, "search", company_id=int(key["company_id"]), query=q, result_count=len(ranked))
        results = []
        trust_cache = {}
        for row in ranked:
            company_id = int(row["company_id"])
            if company_id not in trust_cache:
                trust_cache[company_id] = supplier_trust_metrics(conn, company_id)
            fresh = row.get("freshness") or freshness_details(row.get("last_confirmed_at"))
            results.append({
                "inventory_id": row["id"], "manufacturer": row["brand"], "part_number": row["part_number"],
                "description": row["description"], "condition": row["condition"], "quantity": row["quantity"],
                "location": row["location"], "supplier": {"id": company_id, "name": row["supplier_name"], "trust": trust_cache[company_id]},
                "freshness": {"state": fresh["state"], "age_days": fresh["age_days"]},
                "canonical_part_id": row.get("canonical_part_id"), "match_score": row.get("match_score"), "match_reason": row.get("match_reason"),
            })
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": results, "meta": {"count": len(results), "query": q, "rate_limit_per_hour": key["rate_limit"], "rate_used_before_request": key["rate_used"]}})


@app.get("/v1/catalog/resolve")
def external_catalog_resolve(request: Request, q: str = "", manufacturer: str = "", limit: int = 10):
    key = _require_api_principal(request, "catalog:read")
    if len(normalize(q)) < 2:
        raise HTTPException(status_code=400, detail={"error": "invalid_query", "message": "q must contain at least two searchable characters."})
    with db() as conn:
        candidates = catalog_candidates(conn, q, manufacturer=manufacturer, limit=max(1, min(limit, 25)))
        data = [{k: c.get(k) for k in ("id", "manufacturer", "part_number", "name", "product_family", "lifecycle_status", "verified", "match_score", "match_reason", "aliases")} for c in candidates]
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": data, "meta": {"count": len(data), "query": q}})


@app.get("/v1/intelligence/part")
def external_part_intelligence(request: Request, q: str = "", days: int = 90):
    key = _require_api_principal(request, "intelligence:read")
    with db() as conn:
        plan = effective_plan(conn, int(key["company_id"]))
        if not plan.get("network_intelligence"):
            raise HTTPException(status_code=403, detail={"error": "plan_no_intelligence", "message": "The current plan does not include network intelligence."})
        days = max(1, min(int(days or 90), int(plan.get("analytics_days", 90) or 90)))
        data = part_intelligence(conn, q, days, admin=False)
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": data, "meta": {"privacy_thresholds": {"demand_events": MIN_DEMAND_EVENTS, "buyer_companies": MIN_BUYER_COMPANIES, "quote_samples": MIN_QUOTE_SAMPLES, "quote_suppliers": MIN_QUOTE_SUPPLIERS, "quote_buyer_companies": MIN_QUOTE_BUYER_COMPANIES, "max_contributor_share": MAX_CONTRIBUTOR_SHARE}}})

@app.get("/v1/orders")
def external_orders(request: Request, status: str = "", limit: int = 100):
    key = _require_api_principal(request, "orders:read")
    company_id = int(key["company_id"])
    limit = max(1, min(int(limit or 100), 250))
    params: list = [company_id, company_id]
    extra = ""
    if status in ORDER_STATUSES:
        extra = " AND o.status=?"
        params.append(status)
    params.append(limit)
    with db() as conn:
        rows = conn.execute(
            f"""SELECT o.id FROM orders o WHERE (o.buyer_company_id=? OR o.supplier_company_id=?) {extra}
                ORDER BY o.updated_at DESC,o.id DESC LIMIT ?""", tuple(params)
        ).fetchall()
        data = [order_payload(conn, int(row["id"])) for row in rows]
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": data, "meta": {"count": len(data)}})


@app.get("/v1/orders/{order_id}")
def external_order_detail(request: Request, order_id: int):
    key = _require_api_principal(request, "orders:read")
    with db() as conn:
        order = order_for_company(conn, order_id, int(key["company_id"]))
        if not order:
            raise HTTPException(status_code=404, detail={"error": "order_not_found"})
        payload = order_payload(conn, order_id)
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": payload})


@app.post("/v1/orders/{order_id}/buyer-details")
async def external_order_buyer_details(request: Request, order_id: int):
    key = _require_api_principal(request, "orders:write")
    body = await request.json()
    with db() as conn:
        order = order_for_company(conn, order_id, int(key["company_id"]))
        if not order or int(order["buyer_company_id"]) != int(key["company_id"]):
            raise HTTPException(status_code=404, detail={"error": "order_not_found"})
        if order["status"] not in {"awaiting_buyer_po", "submitted"}:
            raise HTTPException(status_code=409, detail={"error": "order_locked", "message": "Buyer PO details are locked after supplier acknowledgement."})
        po = str(body.get("buyer_po_number", "")).strip()[:120]
        if not po:
            raise HTTPException(status_code=422, detail={"error": "buyer_po_number_required"})
        reference = str(body.get("buyer_reference", "")).strip()[:160]
        address = str(body.get("delivery_address", "")).strip()[:1500]
        contact = str(body.get("delivery_contact", "")).strip()[:300]
        conn.execute("UPDATE orders SET buyer_po_number=?,buyer_reference=?,delivery_address=?,delivery_contact=?,status='submitted',updated_at=? WHERE id=?",
                     (po, reference, address, contact, now_iso(), order_id))
        actor = int(key["created_by_user_id"]) if key.get("created_by_user_id") else None
        record_order_event(conn, order_id, actor, "order.submitted", {"source": "api", "buyer_po_number": po})
        queue_order_webhooks(conn, order_id, "order.submitted")
        payload = order_payload(conn, order_id)
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": payload})


@app.post("/v1/orders/{order_id}/status")
async def external_order_status(request: Request, order_id: int):
    key = _require_api_principal(request, "orders:write")
    body = await request.json()
    action = str(body.get("status", "")).strip()
    if action not in {"acknowledged", "processing", "dispatched", "delivered"}:
        raise HTTPException(status_code=422, detail={"error": "invalid_status"})
    with db() as conn:
        order = order_for_company(conn, order_id, int(key["company_id"]))
        if not order or int(order["supplier_company_id"]) != int(key["company_id"]):
            raise HTTPException(status_code=404, detail={"error": "order_not_found"})
        allowed = {"acknowledged": {"submitted"}, "processing": {"submitted", "acknowledged", "processing"},
                   "dispatched": {"submitted", "acknowledged", "processing"}, "delivered": {"dispatched"}}
        if order["status"] not in allowed[action]:
            raise HTTPException(status_code=409, detail={"error": "invalid_transition", "from": order["status"], "to": action})
        supplier_ref = (str(body.get("supplier_order_reference", "")).strip()[:160] or order["supplier_order_reference"] or "")
        dispatch_date = (str(body.get("expected_dispatch_date", "")).strip()[:40] or order["expected_dispatch_date"] or "")
        carrier = (str(body.get("carrier", "")).strip()[:120] or order["carrier"] or "")
        tracking = (str(body.get("tracking_number", "")).strip()[:160] or order["tracking_number"] or "")
        tracking_url = (str(body.get("tracking_url", "")).strip()[:500] or order["tracking_url"] or "")
        if tracking_url:
            try: tracking_url = safe_external_url(tracking_url)
            except ValueError as exc: raise HTTPException(status_code=422, detail={"error": "invalid_tracking_url", "message": str(exc)})
        conn.execute("UPDATE orders SET status=?,supplier_order_reference=?,expected_dispatch_date=?,carrier=?,tracking_number=?,tracking_url=?,updated_at=? WHERE id=?",
                     (action, supplier_ref, dispatch_date, carrier, tracking, tracking_url, now_iso(), order_id))
        timestamp_column = {"acknowledged": "acknowledged_at", "dispatched": "dispatched_at", "delivered": "delivered_at"}.get(action)
        if timestamp_column:
            conn.execute(f"UPDATE orders SET {timestamp_column}=? WHERE id=?", (now_iso(), order_id))
        actor = int(key["created_by_user_id"]) if key.get("created_by_user_id") else None
        record_order_event(conn, order_id, actor, f"order.{action}", {"source": "api", "tracking_number": tracking})
        queue_order_webhooks(conn, order_id, f"order.{action}")
        payload = order_payload(conn, order_id)
        record_api_usage(conn, key, request.url.path, 200)
    return JSONResponse({"data": payload})


# ---------- platform admin ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request:Request):
    user=current_user(request)
    if not user or user["role"]!="admin": raise HTTPException(status_code=403)
    with db() as conn:
        pending=conn.execute("SELECT * FROM companies WHERE company_type='supplier' AND active=1 ORDER BY verified ASC,created_at DESC").fetchall()
        stats=conn.execute("""SELECT (SELECT COUNT(*) FROM users WHERE active=1) AS users,(SELECT COUNT(*) FROM companies WHERE company_type='supplier' AND verified=1 AND active=1) AS suppliers,(SELECT COUNT(*) FROM inventory WHERE active=1 AND deleted_at IS NULL) AS inventory,(SELECT COUNT(*) FROM rfqs) AS rfqs""").fetchone()
    return render(request,"admin.html",pending=pending,stats=stats)


@app.post("/admin/companies/{company_id}/verify")
async def admin_verify(request:Request,company_id:int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_user(request)
    if user["role"]!="admin": raise HTTPException(status_code=403)
    with db() as conn:
        company=conn.execute("SELECT * FROM companies WHERE id=? AND company_type='supplier'",(company_id,)).fetchone()
        if not company:raise HTTPException(status_code=404)
        conn.execute("UPDATE companies SET verified=1 WHERE id=?",(company_id,)); recipients=conn.execute("SELECT email FROM users WHERE company_id=? AND active=1",(company_id,)).fetchall()
        inventory_ids=[r["id"] for r in conn.execute("SELECT id FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL AND quantity>0",(company_id,)).fetchall()]
        audit(conn,request,user,"supplier.verified","company",company_id,{"company":company["name"]},company_id=company_id)
    trial = start_trial_if_needed(company_id)
    process_inventory_match_alerts(inventory_ids)
    trial_note = ""
    if trial and trial.get("trial_ends_at"):
        trial_note = f" Your {str(FOUNDING_TRIAL_DAYS) + '-day founding' if trial.get('founding_trial') else 'free'} trial is active until {trial['trial_ends_at'][:10]}."
    for recipient in recipients: send_email(recipient["email"],"Controls Exchange supplier approved",f"{company['name']} has been verified. Your active inventory is now searchable by buyers.{trial_note}")
    flash(request,f"{company['name']} verified. Its active inventory is now live."); return RedirectResponse("/admin",status_code=303)


@app.post("/admin/companies/{company_id}/unverify")
async def admin_unverify(request:Request,company_id:int):
    form=await request.form(); check_csrf(request,str(form.get("csrf_token","")))
    user=require_user(request)
    if user["role"]!="admin": raise HTTPException(status_code=403)
    with db() as conn:
        company=conn.execute("SELECT * FROM companies WHERE id=? AND company_type='supplier'",(company_id,)).fetchone()
        if not company:raise HTTPException(status_code=404)
        conn.execute("UPDATE companies SET verified=0 WHERE id=?",(company_id,)); audit(conn,request,user,"supplier.unverified","company",company_id,{"company":company["name"]},company_id=company_id)
    flash(request,"Supplier removed from live search pending re-verification."); return RedirectResponse("/admin",status_code=303)


# ---------- Phase 4: technical catalogue / identification / sourcing ----------
@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, q: str = "", manufacturer: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/catalog", status_code=303)
    with db() as conn:
        if q.strip():
            parts = catalog_candidates(conn, q, manufacturer=manufacturer, limit=100)
        else:
            params = []
            extra = ""
            if manufacturer:
                extra = " WHERE lower(manufacturer)=?"; params.append(manufacturer.lower())
            parts = [dict(r) for r in conn.execute(f"SELECT * FROM catalog_parts{extra} ORDER BY manufacturer,part_number LIMIT 200", params).fetchall()]
            for part in parts:
                part["match_score"] = None; part["match_reason"] = "Catalogue"
        manufacturers = [r["manufacturer"] for r in conn.execute("SELECT DISTINCT manufacturer FROM catalog_parts WHERE manufacturer<>'' ORDER BY manufacturer").fetchall()]
        campaigns = conn.execute("""SELECT mc.id,mc.company_id,mc.title,mc.body,mc.cta_label,mc.cta_url,COALESCE(NULLIF(mp.brand_name,''),c.name) AS brand_name
            FROM manufacturer_campaigns mc JOIN companies c ON c.id=mc.company_id
            JOIN billing_subscriptions bs ON bs.company_id=c.id LEFT JOIN manufacturer_profiles mp ON mp.company_id=c.id
            WHERE mc.active=1 AND mc.starts_at<=? AND (mc.ends_at IS NULL OR mc.ends_at>?) AND c.active=1 AND c.verified=1 AND c.account_kind='manufacturer'
              AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))
            ORDER BY mc.created_at DESC LIMIT 3""", (now_iso(), now_iso(), now_iso())).fetchall()
        viewer = current_user(request)
        for campaign in campaigns:
            record_marketplace_event(conn, "campaign_impression", user_id=viewer["id"] if viewer else None, user_company_id=viewer["company_id"] if viewer else None, supplier_company_id=campaign["company_id"], metadata={"campaign_id": campaign["id"]})
        for part in parts:
            part["stock_count"] = int(scalar(conn, """SELECT COUNT(*) FROM inventory i JOIN companies c ON c.id=i.company_id JOIN billing_subscriptions bs ON bs.company_id=c.id
                WHERE i.canonical_part_id=? AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND i.last_confirmed_at>=? AND c.active=1 AND c.verified=1
                  AND (bs.status IN ('active','past_due') OR (bs.status='trialing' AND bs.trial_ends_at>?))""", (part["id"], freshness_cutoff_iso(), now_iso())) or 0)
            part["relation_count"] = int(scalar(conn, "SELECT COUNT(*) FROM catalog_relations WHERE from_part_id=?", (part["id"],)) or 0)
    return render(request, "catalog.html", parts=parts, q=q, manufacturer=manufacturer, manufacturers=manufacturers, campaigns=campaigns)


@app.get("/catalog/{part_id}", response_class=HTMLResponse)
def catalog_detail_page(request: Request, part_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/catalog/{part_id}", status_code=303)
    with db() as conn:
        part = part_detail(conn, part_id)
        if not part:
            raise HTTPException(status_code=404)
        recs = technical_recommendations(conn, part_id)
    return render(request, "catalog_detail.html", part=part, recs=recs, relation_labels=CATALOG_RELATION_TYPES)



@app.get("/campaigns/{campaign_id}/go")
def campaign_go(request: Request, campaign_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM manufacturer_campaigns WHERE id=? AND active=1", (campaign_id,)).fetchone()
        if not row or not row["cta_url"]:
            raise HTTPException(status_code=404)
        try:
            url = safe_external_url(row["cta_url"])
        except ValueError:
            raise HTTPException(status_code=404)
        viewer = current_user(request)
        record_marketplace_event(conn, "campaign_click", user_id=viewer["id"] if viewer else None, user_company_id=viewer["company_id"] if viewer else None, supplier_company_id=row["company_id"], metadata={"campaign_id": campaign_id})
    return RedirectResponse(url, status_code=302)


@app.get("/identify", response_class=HTMLResponse)
def identify_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login?next=/identify", status_code=303)
    return render(request, "identify.html", result=None, ai_configured=bool(os.getenv("OPENAI_API_KEY", "").strip()))


@app.post("/identify", response_class=HTMLResponse)
async def identify_submit(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    photo = form.get("photo")
    visible_text = str(form.get("visible_text", "") or "").strip()
    if not photo or not hasattr(photo, "read"):
        flash(request, "Choose a photo to identify.", "error")
        return RedirectResponse("/identify", status_code=303)
    raw = await photo.read()
    content_type = getattr(photo, "content_type", "") or "application/octet-stream"
    filename = getattr(photo, "filename", "") or "control-photo"
    try:
        result = identify_photo(raw, content_type, filename, visible_text)
        result = match_identification_to_catalog(result)
    except httpx.HTTPStatusError as exc:
        logger.exception("photo_identification_http_error status=%s", exc.response.status_code)
        result = {"error": "The photo-identification service rejected the request. Check the API key/model in the setup guide."}
    except Exception as exc:
        logger.exception("photo_identification_failed")
        result = {"error": str(exc)}
    if not result.get("error"):
        with db() as conn:
            identification_id = insert_id(conn, """INSERT INTO photo_identifications(user_id,company_id,provider,original_filename,visible_text,identified_manufacturer,identified_part_number,confidence,matched_part_id,result_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (user["id"], user["company_id"], result.get("provider", ""), filename[:255], result.get("visible_text", "")[:4000], result.get("manufacturer", "")[:255], result.get("part_number", "")[:255], result.get("confidence"), result.get("matched_part_id"), json.dumps(result, separators=(",", ":")), now_iso()))
            audit(conn, request, user, "catalog.photo_identified", "photo_identification", identification_id, {"provider": result.get("provider"), "matched_part_id": result.get("matched_part_id")})
    return render(request, "identify.html", result=result, ai_configured=bool(os.getenv("OPENAI_API_KEY", "").strip()))


@app.get("/source", response_class=HTMLResponse)
def source_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login?next=/source", status_code=303)
    return render(request, "source.html", result=None, query="")


@app.post("/source", response_class=HTMLResponse)
async def source_submit(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_verified_email(request)
    query = str(form.get("query", "") or "").strip()
    if len(query) < 2:
        return render(request, "source.html", result=None, query=query, form_error="Describe the part or enter a model number.")
    with db() as conn:
        result = source_assistant(conn, query, company_id=user["company_id"] if user["role"] == "supplier" else None)
        qid = insert_id(conn, "INSERT INTO sourcing_queries(user_id,company_id,query,result_json,created_at) VALUES(?,?,?,?,?)", (user["id"], user["company_id"], query, json.dumps(result, separators=(",", ":")), now_iso()))
        audit(conn, request, user, "catalog.sourcing_query", "sourcing_query", qid, {"primary_part_id": (result.get("primary") or {}).get("id")})
    return render(request, "source.html", result=result, query=query)


@app.get("/admin/catalog", response_class=HTMLResponse)
def admin_catalog_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/admin/catalog", status_code=303)
    if user["role"] != "admin":
        raise HTTPException(status_code=403)
    with db() as conn:
        parts = [dict(r) for r in conn.execute("SELECT * FROM catalog_parts ORDER BY updated_at DESC LIMIT 300").fetchall()]
        relations = [dict(r) for r in conn.execute("""SELECT r.*,f.manufacturer AS from_manufacturer,f.part_number AS from_part,t.manufacturer AS to_manufacturer,t.part_number AS to_part
            FROM catalog_relations r JOIN catalog_parts f ON f.id=r.from_part_id JOIN catalog_parts t ON t.id=r.to_part_id ORDER BY r.updated_at DESC LIMIT 300""").fetchall()]
    return render(request, "admin_catalog.html", parts=parts, relations=relations, lifecycle_statuses=LIFECYCLE_STATUSES, relation_types=CATALOG_RELATION_TYPES)


@app.post("/admin/catalog/parts")
async def admin_catalog_create_part(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    manufacturer = str(form.get("manufacturer", "")).strip(); part_number = str(form.get("part_number", "")).strip()
    if not manufacturer or not part_number:
        flash(request, "Manufacturer and part number are required.", "error"); return RedirectResponse("/admin/catalog", status_code=303)
    lifecycle = str(form.get("lifecycle_status", "unknown")); lifecycle = lifecycle if lifecycle in LIFECYCLE_STATUSES else "unknown"
    try:
        with db() as conn:
            part_id = insert_id(conn, """INSERT INTO catalog_parts(manufacturer,part_number,normalized_part,name,product_family,description,lifecycle_status,notes,source,verified,created_by_user_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (manufacturer, part_number, normalize(part_number), str(form.get("name", "")).strip(), str(form.get("product_family", "")).strip(), str(form.get("description", "")).strip(), lifecycle, str(form.get("notes", "")).strip(), str(form.get("source", "")).strip(), 1 if form.get("verified") else 0, user["id"], now_iso(), now_iso()))
            audit(conn, request, user, "catalog.part_created", "catalog_part", part_id, {"manufacturer": manufacturer, "part_number": part_number})
        link_inventory_ids()
        flash(request, f"Added {manufacturer} {part_number} to the canonical catalogue.")
    except Exception as exc:
        logger.exception("catalog_part_create_failed")
        flash(request, "Could not add that part. It may already exist for that manufacturer.", "error")
    return RedirectResponse("/admin/catalog", status_code=303)


@app.post("/admin/catalog/{part_id}/edit")
async def admin_catalog_edit_part(request: Request, part_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    lifecycle = str(form.get("lifecycle_status", "unknown")); lifecycle = lifecycle if lifecycle in LIFECYCLE_STATUSES else "unknown"
    with db() as conn:
        part = conn.execute("SELECT * FROM catalog_parts WHERE id=?", (part_id,)).fetchone()
        if not part: raise HTTPException(status_code=404)
        conn.execute("""UPDATE catalog_parts SET name=?,product_family=?,description=?,lifecycle_status=?,notes=?,source=?,verified=?,updated_at=? WHERE id=?""",
            (str(form.get("name", "")).strip(), str(form.get("product_family", "")).strip(), str(form.get("description", "")).strip(), lifecycle, str(form.get("notes", "")).strip(), str(form.get("source", "")).strip(), 1 if form.get("verified") else 0, now_iso(), part_id))
        audit(conn, request, user, "catalog.part_updated", "catalog_part", part_id)
    flash(request, "Catalogue part updated.")
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


@app.post("/admin/catalog/{part_id}/aliases")
async def admin_catalog_add_alias(request: Request, part_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    alias = str(form.get("alias", "")).strip()
    if not alias:
        flash(request, "Alias is required.", "error"); return RedirectResponse(f"/catalog/{part_id}", status_code=303)
    try:
        with db() as conn:
            if not conn.execute("SELECT id FROM catalog_parts WHERE id=?", (part_id,)).fetchone(): raise HTTPException(status_code=404)
            alias_id = insert_id(conn, "INSERT INTO catalog_aliases(part_id,alias,normalized_alias,source,verified,created_at) VALUES(?,?,?,?,?,?)", (part_id, alias, normalize(alias), str(form.get("source", "")).strip(), 1 if form.get("verified") else 0, now_iso()))
            audit(conn, request, user, "catalog.alias_created", "catalog_alias", alias_id, {"part_id": part_id, "alias": alias})
        link_inventory_ids()
        flash(request, "Alias added and inventory linkage refreshed.")
    except HTTPException:
        raise
    except Exception:
        flash(request, "That alias already exists for this part.", "error")
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


@app.post("/admin/catalog/relations")
async def admin_catalog_add_relation(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    try:
        from_id = int(str(form.get("from_part_id", "0"))); to_id = int(str(form.get("to_part_id", "0")))
        confidence = max(0.0, min(1.0, float(str(form.get("confidence", "1")))))
    except Exception:
        flash(request, "Choose valid source/target parts and confidence.", "error"); return RedirectResponse("/admin/catalog", status_code=303)
    relation_type = str(form.get("relation_type", ""))
    source = str(form.get("source", "")).strip()
    wants_verified = bool(form.get("verified"))
    if relation_type not in CATALOG_RELATION_TYPES or from_id == to_id:
        flash(request, "Choose a valid relationship between two different parts.", "error"); return RedirectResponse("/admin/catalog", status_code=303)
    if wants_verified and not source:
        flash(request, "A source is required before a technical relationship can be marked Reviewed.", "error"); return RedirectResponse("/admin/catalog", status_code=303)
    try:
        with db() as conn:
            if not conn.execute("SELECT id FROM catalog_parts WHERE id=?", (from_id,)).fetchone() or not conn.execute("SELECT id FROM catalog_parts WHERE id=?", (to_id,)).fetchone(): raise ValueError("Part missing")
            relation_id = insert_id(conn, """INSERT INTO catalog_relations(from_part_id,to_part_id,relation_type,confidence,notes,source,verified,created_by_user_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (from_id, to_id, relation_type, confidence, str(form.get("notes", "")).strip(), source, 1 if wants_verified else 0, user["id"], now_iso(), now_iso()))
            audit(conn, request, user, "catalog.relation_created", "catalog_relation", relation_id, {"from_part_id": from_id, "to_part_id": to_id, "type": relation_type})
        flash(request, "Technical relationship added.")
    except Exception:
        flash(request, "Could not add that relationship; it may already exist.", "error")
    return RedirectResponse("/admin/catalog", status_code=303)


@app.post("/admin/catalog/relations/{relation_id}/verify")
async def admin_catalog_verify_relation(request: Request, relation_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    with db() as conn:
        rel = conn.execute("SELECT * FROM catalog_relations WHERE id=?", (relation_id,)).fetchone()
        if not rel: raise HTTPException(status_code=404)
        verified = 0 if rel["verified"] else 1
        if verified and not (rel["source"] or "").strip():
            flash(request, "Add a source before marking a technical relationship Reviewed.", "error")
            return RedirectResponse("/admin/catalog", status_code=303)
        conn.execute("UPDATE catalog_relations SET verified=?,updated_at=? WHERE id=?", (verified, now_iso(), relation_id))
        audit(conn, request, user, "catalog.relation_verification_changed", "catalog_relation", relation_id, {"verified": bool(verified)})
    flash(request, "Relationship verification updated.")
    return RedirectResponse("/admin/catalog", status_code=303)



# ---------- Phase 5: billing / monetisation / analytics ----------
def _commercial_company_user(request: Request):
    user = require_verified_email(request)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    return user


def _billing_manager(request: Request):
    user = _commercial_company_user(request)
    if user["company_role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only company owners/admins can manage billing.")
    return user


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, checkout: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/billing", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse("/admin/billing", status_code=303)
    with db() as conn:
        sub = subscription_for(conn, user["company_id"])
        sub["days_remaining"] = trial_days_remaining(sub)
        usage = plan_usage(conn, user["company_id"])
        current_plan = effective_plan(conn, user["company_id"])
        if user["role"] == "buyer":
            available = {"buyer_free": PLANS["buyer_free"]}
        elif user["account_kind"] == "manufacturer":
            available = {"manufacturer": PLANS["manufacturer"]}
        else:
            available = {k: v for k, v in PLANS.items() if v["audience"] == "supplier"}
    return render(request, "billing.html", sub=sub, usage=usage, current_plan=current_plan, available_plans=available,
                  stripe_configured=stripe_enabled(), checkout=checkout,
                  founding_trial_days=FOUNDING_TRIAL_DAYS, standard_trial_days=STANDARD_TRIAL_DAYS,
                  founding_trial_enabled=FOUNDING_TRIAL_ENABLED)


@app.post("/billing/checkout")
async def billing_checkout(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = _billing_manager(request)
    plan_key = str(form.get("plan_key", ""))
    interval = str(form.get("interval", "monthly"))
    if plan_key not in PLANS or plan_key == "buyer_free":
        flash(request, "Choose a valid paid plan.", "error")
        return RedirectResponse("/billing", status_code=303)
    if user["account_kind"] == "manufacturer" and plan_key != "manufacturer":
        flash(request, "Manufacturer accounts use the Manufacturer plan.", "error")
        return RedirectResponse("/billing", status_code=303)
    if user["account_kind"] != "manufacturer" and PLANS[plan_key]["audience"] != "supplier":
        flash(request, "That plan is not available for this account.", "error")
        return RedirectResponse("/billing", status_code=303)
    try:
        with db() as conn:
            company = conn.execute("SELECT * FROM companies WHERE id=?", (user["company_id"],)).fetchone()
            sub = subscription_for(conn, user["company_id"])
            # Never create a second Stripe subscription for a company that already has
            # a non-terminal one. Existing subscribers manage payment/cancellation/plan
            # changes in Stripe's hosted Billing Portal, which handles SCA safely.
            if sub.get("stripe_subscription_id") and sub.get("status") in {"trialing", "active", "past_due", "paused", "unpaid"}:
                url = create_portal_session(conn, user["company_id"])
                audit(conn, request, user, "billing.portal_started", "company", user["company_id"], {"reason": "existing_subscription"})
                flash(request, "You already have a Stripe subscription. Manage it in the secure billing portal.")
                return RedirectResponse(url, status_code=303)
            url = create_checkout_session(conn, company, user["email"], plan_key, interval)
            audit(conn, request, user, "billing.checkout_started", "company", user["company_id"], {"plan_key": plan_key, "interval": interval})
        return RedirectResponse(url, status_code=303)
    except Exception as exc:
        logger.exception("billing_checkout_failed company_id=%s", user["company_id"])
        flash(request, f"Checkout could not be started: {exc}", "error")
        return RedirectResponse("/billing", status_code=303)


@app.post("/billing/portal")
async def billing_portal(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = _billing_manager(request)
    try:
        with db() as conn:
            url = create_portal_session(conn, user["company_id"])
        return RedirectResponse(url, status_code=303)
    except Exception as exc:
        flash(request, f"Billing portal is unavailable: {exc}", "error")
        return RedirectResponse("/billing", status_code=303)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(payload, signature):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    try:
        event = json.loads(payload.decode("utf-8"))
        process_stripe_event(event)
    except Exception:
        logger.exception("stripe_webhook_processing_failed")
        raise HTTPException(status_code=500, detail="Webhook processing failed")
    return JSONResponse({"received": True})


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, days: int = 90):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/analytics", status_code=303)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    with db() as conn:
        plan = effective_plan(conn, user["company_id"])
        max_days = int(plan.get("analytics_days", 0) or 0)
        allowed = [d for d in (30, 90, 365) if d <= max_days]
        if not allowed:
            analytics = None
            days = 0
        else:
            days = days if days in allowed else max(allowed)
            analytics = supplier_analytics(conn, user["company_id"], days)
        sub = subscription_for(conn, user["company_id"])
    return render(request, "analytics.html", analytics=analytics, days=days, allowed_days=allowed, plan=plan, sub=sub)


@app.get("/promotions", response_class=HTMLResponse)
def promotions_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/promotions", status_code=303)
    if user["role"] != "supplier":
        raise HTTPException(status_code=403)
    with db() as conn:
        plan = effective_plan(conn, user["company_id"])
        rows = conn.execute("""SELECT i.*, CASE WHEN ip.id IS NULL THEN 0 ELSE 1 END AS promoted
            FROM inventory i LEFT JOIN inventory_promotions ip ON ip.inventory_id=i.id AND ip.active=1 AND (ip.ends_at IS NULL OR ip.ends_at>?)
            WHERE i.company_id=? AND i.active=1 AND i.deleted_at IS NULL AND i.quantity>0
            ORDER BY promoted DESC,i.brand,i.part_number LIMIT 1000""", (now_iso(), user["company_id"])).fetchall()
        usage = plan_usage(conn, user["company_id"])
    return render(request, "promotions.html", rows=rows, plan=plan, usage=usage)


@app.post("/promotions/{inventory_id}/toggle")
async def promotion_toggle(request: Request, inventory_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_permission(request, "manage_inventory")
    if user["role"] != "supplier": raise HTTPException(status_code=403)
    with db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND company_id=? AND deleted_at IS NULL", (inventory_id, user["company_id"])).fetchone()
        if not item: raise HTTPException(status_code=404)
        existing = conn.execute("SELECT * FROM inventory_promotions WHERE company_id=? AND inventory_id=?", (user["company_id"], inventory_id)).fetchone()
        if existing and existing["active"]:
            conn.execute("UPDATE inventory_promotions SET active=0,ends_at=? WHERE id=?", (now_iso(), existing["id"]))
            audit(conn, request, user, "inventory.promotion_disabled", "inventory", inventory_id)
            flash(request, "Promotion removed.")
        else:
            if not commercial_access(conn, user["company_id"]):
                flash(request, "Choose a paid plan to promote inventory after your trial.", "error")
                return RedirectResponse("/billing", status_code=303)
            ok, current, limit = check_limit(conn, user["company_id"], "promotions", 1)
            if not ok:
                flash(request, f"Your plan allows {limit} promoted inventory line(s). Upgrade to add another.", "error")
                return RedirectResponse("/billing", status_code=303)
            if existing:
                conn.execute("UPDATE inventory_promotions SET active=1,starts_at=?,ends_at=NULL,created_by_user_id=? WHERE id=?", (now_iso(), user["id"], existing["id"]))
            else:
                insert_id(conn, "INSERT INTO inventory_promotions(company_id,inventory_id,created_by_user_id,active,starts_at,created_at) VALUES(?,?,?,?,?,?)", (user["company_id"], inventory_id, user["id"], 1, now_iso(), now_iso()))
            audit(conn, request, user, "inventory.promoted", "inventory", inventory_id)
            flash(request, "Inventory line promoted. Promotion only breaks ties between similarly relevant search results.")
    return RedirectResponse("/promotions", status_code=303)


@app.get("/manufacturer", response_class=HTMLResponse)
def manufacturer_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?next=/manufacturer", status_code=303)
    if user["role"] != "supplier" or user["account_kind"] != "manufacturer":
        raise HTTPException(status_code=403)
    with db() as conn:
        profile = conn.execute("SELECT * FROM manufacturer_profiles WHERE company_id=?", (user["company_id"],)).fetchone()
        campaigns = conn.execute("SELECT * FROM manufacturer_campaigns WHERE company_id=? ORDER BY active DESC,created_at DESC", (user["company_id"],)).fetchall()
        plan = effective_plan(conn, user["company_id"])
        usage = plan_usage(conn, user["company_id"])
    return render(request, "manufacturer.html", profile=profile, campaigns=campaigns, plan=plan, usage=usage)


@app.post("/manufacturer/profile")
async def manufacturer_profile_update(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = _commercial_company_user(request)
    if user["account_kind"] != "manufacturer": raise HTTPException(status_code=403)
    brand_name = str(form.get("brand_name", "")).strip()[:200]
    try:
        website = safe_external_url(str(form.get("website", "")))
    except ValueError as exc:
        flash(request, str(exc), "error"); return RedirectResponse("/manufacturer", status_code=303)
    description = str(form.get("description", "")).strip()[:4000]
    support_email = str(form.get("support_email", "")).strip()[:320]
    with db() as conn:
        existing = conn.execute("SELECT id FROM manufacturer_profiles WHERE company_id=?", (user["company_id"],)).fetchone()
        if existing:
            conn.execute("UPDATE manufacturer_profiles SET brand_name=?,website=?,description=?,support_email=?,updated_at=? WHERE company_id=?", (brand_name, website, description, support_email, now_iso(), user["company_id"]))
        else:
            insert_id(conn, "INSERT INTO manufacturer_profiles(company_id,brand_name,website,description,support_email,updated_at) VALUES(?,?,?,?,?,?)", (user["company_id"], brand_name, website, description, support_email, now_iso()))
        audit(conn, request, user, "manufacturer.profile_updated", "company", user["company_id"])
    flash(request, "Manufacturer profile updated.")
    return RedirectResponse("/manufacturer", status_code=303)


@app.post("/manufacturer/campaigns")
async def manufacturer_campaign_create(request: Request):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = _commercial_company_user(request)
    if user["account_kind"] != "manufacturer": raise HTTPException(status_code=403)
    with db() as conn:
        if not commercial_access(conn, user["company_id"]):
            flash(request, "Choose a plan before creating campaigns.", "error"); return RedirectResponse("/billing", status_code=303)
        ok, current, limit = check_limit(conn, user["company_id"], "campaigns", 1)
        if not ok:
            flash(request, f"Your plan allows {limit} active campaign(s).", "error"); return RedirectResponse("/manufacturer", status_code=303)
        title = str(form.get("title", "")).strip()[:180]
        body = str(form.get("body", "")).strip()[:1200]
        if not title or not body:
            flash(request, "Campaign title and message are required.", "error"); return RedirectResponse("/manufacturer", status_code=303)
        try:
            cta_url = safe_external_url(str(form.get("cta_url", "")))
        except ValueError as exc:
            flash(request, str(exc), "error"); return RedirectResponse("/manufacturer", status_code=303)
        campaign_id = insert_id(conn, """INSERT INTO manufacturer_campaigns(company_id,title,body,cta_label,cta_url,active,starts_at,created_by_user_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (user["company_id"], title, body, str(form.get("cta_label", "Learn more")).strip()[:60], cta_url, 1, now_iso(), user["id"], now_iso(), now_iso()))
        audit(conn, request, user, "manufacturer.campaign_created", "manufacturer_campaign", campaign_id, {"title": title})
    flash(request, "Campaign published to the catalogue sponsorship area.")
    return RedirectResponse("/manufacturer", status_code=303)


@app.post("/manufacturer/campaigns/{campaign_id}/toggle")
async def manufacturer_campaign_toggle(request: Request, campaign_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = _commercial_company_user(request)
    if user["account_kind"] != "manufacturer": raise HTTPException(status_code=403)
    with db() as conn:
        row = conn.execute("SELECT * FROM manufacturer_campaigns WHERE id=? AND company_id=?", (campaign_id, user["company_id"])).fetchone()
        if not row: raise HTTPException(status_code=404)
        active = 0 if row["active"] else 1
        if active:
            ok, current, limit = check_limit(conn, user["company_id"], "campaigns", 1)
            if not ok:
                flash(request, f"Your plan allows {limit} active campaign(s).", "error"); return RedirectResponse("/manufacturer", status_code=303)
        conn.execute("UPDATE manufacturer_campaigns SET active=?,updated_at=? WHERE id=?", (active, now_iso(), campaign_id))
        audit(conn, request, user, "manufacturer.campaign_toggled", "manufacturer_campaign", campaign_id, {"active": bool(active)})
    flash(request, "Campaign status updated.")
    return RedirectResponse("/manufacturer", status_code=303)


@app.get("/admin/billing", response_class=HTMLResponse)
def admin_billing_page(request: Request):
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    with db() as conn:
        companies = conn.execute("""SELECT c.id,c.name,c.location,c.verified,c.account_kind,bs.plan_key,bs.status,bs.founding_trial,bs.trial_started_at,bs.trial_ends_at,bs.current_period_end,bs.stripe_customer_id
            FROM companies c JOIN billing_subscriptions bs ON bs.company_id=c.id WHERE c.company_type='supplier' AND c.active=1 ORDER BY c.name""").fetchall()
    return render(request, "admin_billing.html", companies=companies)


@app.post("/admin/billing/{company_id}/trial")
async def admin_billing_trial(request: Request, company_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    try: days = max(1, min(730, int(str(form.get("days", "90")))))
    except Exception: days = 90
    plan_key = str(form.get("plan_key", "supplier_pro"))
    start = datetime.now(timezone.utc); end = start + timedelta(days=days)
    with db() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id=? AND company_type='supplier'", (company_id,)).fetchone()
        if not company: raise HTTPException(status_code=404)
        if company["account_kind"] == "manufacturer":
            plan_key = "manufacturer"
        elif plan_key not in {"supplier_starter", "supplier_pro", "supplier_premium"}:
            plan_key = "supplier_pro"
        conn.execute("""UPDATE billing_subscriptions SET plan_key=?,status='trialing',founding_trial=?,trial_started_at=?,trial_ends_at=?,updated_at=? WHERE company_id=?""",
                     (plan_key, 1 if days == FOUNDING_TRIAL_DAYS else 0, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), now_iso(), company_id))
        conn.execute("DELETE FROM billing_notifications WHERE company_id=?", (company_id,))
        audit(conn, request, user, "billing.trial_granted", "company", company_id, {"days": days, "plan_key": plan_key}, company_id=company_id)
    flash(request, f"{days}-day trial granted.")
    return RedirectResponse("/admin/billing", status_code=303)


@app.post("/admin/billing/{company_id}/set-status")
async def admin_billing_status(request: Request, company_id: int):
    form = await request.form(); check_csrf(request, str(form.get("csrf_token", "")))
    user = require_user(request)
    if user["role"] != "admin": raise HTTPException(status_code=403)
    plan_key = str(form.get("plan_key", "supplier_pro")); status = str(form.get("status", "active"))
    if status not in {"active","paused","canceled","past_due"}:
        flash(request, "Invalid billing override.", "error"); return RedirectResponse("/admin/billing", status_code=303)
    with db() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id=? AND company_type='supplier'", (company_id,)).fetchone()
        if not company:
            raise HTTPException(status_code=404)
        if company["account_kind"] == "manufacturer":
            plan_key = "manufacturer"
        elif plan_key not in {"supplier_starter", "supplier_pro", "supplier_premium"}:
            flash(request, "Invalid supplier plan.", "error"); return RedirectResponse("/admin/billing", status_code=303)
        conn.execute("UPDATE billing_subscriptions SET plan_key=?,status=?,trial_ends_at=NULL,current_period_end=NULL,updated_at=? WHERE company_id=?", (plan_key, status, now_iso(), company_id))
        audit(conn, request, user, "billing.status_overridden", "company", company_id, {"plan_key": plan_key, "status": status}, company_id=company_id)
    flash(request, "Billing access updated.")
    return RedirectResponse("/admin/billing", status_code=303)

# ---------- health ----------
@app.get("/health")
@app.get("/health/live")
def health_live():
    return JSONResponse({"ok":True,"service":"controls-exchange"})


@app.get("/health/ready")
def health_ready():
    try:
        with db() as conn:
            scalar(conn,"SELECT 1")
        return JSONResponse({"ok":True,"database":"postgresql" if IS_POSTGRES else "sqlite"})
    except Exception as exc:
        logger.exception("readiness_check_failed")
        return JSONResponse({"ok":False,"database":"unavailable","error":type(exc).__name__},status_code=503)
