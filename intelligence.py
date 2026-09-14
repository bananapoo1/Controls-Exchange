from __future__ import annotations

import hashlib
import json
import os
import secrets
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from billing import effective_plan, subscription_for
from platform_core import IS_POSTGRES, _ensure_column, db, insert_id, normalize, now_iso, parse_iso, scalar

MIN_DEMAND_EVENTS = max(1, int(os.getenv("INTELLIGENCE_MIN_DEMAND_EVENTS", "5")))
MIN_BUYER_COMPANIES = max(1, int(os.getenv("INTELLIGENCE_MIN_BUYER_COMPANIES", "3")))
MIN_QUOTE_SAMPLES = max(1, int(os.getenv("INTELLIGENCE_MIN_QUOTE_SAMPLES", "5")))
MIN_QUOTE_SUPPLIERS = max(1, int(os.getenv("INTELLIGENCE_MIN_QUOTE_SUPPLIERS", "3")))
MIN_QUOTE_BUYER_COMPANIES = max(1, int(os.getenv("INTELLIGENCE_MIN_QUOTE_BUYER_COMPANIES", "3")))
DEFAULT_API_RATE_PER_HOUR = max(10, int(os.getenv("API_DEFAULT_RATE_PER_HOUR", "1000")))
MAX_API_RATE_PER_HOUR = max(DEFAULT_API_RATE_PER_HOUR, int(os.getenv("API_MAX_RATE_PER_HOUR", "10000")))
SEARCH_DEMAND_DEDUP_MINUTES = max(0, int(os.getenv("SEARCH_DEMAND_DEDUP_MINUTES", "30")))
MAX_CONTRIBUTOR_SHARE = min(1.0, max(0.2, float(os.getenv("INTELLIGENCE_MAX_CONTRIBUTOR_SHARE", "0.5"))))

API_SCOPES = {"inventory:read", "catalog:read", "intelligence:read", "orders:read", "orders:write"}


def init_intelligence_schema() -> None:
    """Create the Phase 6 data layer and backfill RFQ/wanted demand safely."""
    with db() as conn:
        if IS_POSTGRES:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_demand_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor_company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
                    normalized_query TEXT NOT NULL,
                    display_query TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    result_count INTEGER,
                    source_id BIGINT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_demand_source_unique
                    ON market_demand_events(event_type, source_id) WHERE source_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_demand_normalized_created ON market_demand_events(normalized_query, created_at);
                CREATE INDEX IF NOT EXISTS idx_demand_actor_created ON market_demand_events(actor_company_id, created_at);

                CREATE TABLE IF NOT EXISTS api_keys (
                    id BIGSERIAL PRIMARY KEY,
                    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL DEFAULT 'inventory:read,catalog:read',
                    rate_limit_per_hour INTEGER NOT NULL DEFAULT 1000,
                    active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    last_used_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_company_active ON api_keys(company_id, active);

                CREATE TABLE IF NOT EXISTS api_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    api_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
                    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
                    endpoint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_key_created ON api_usage_events(api_key_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_api_usage_company_created ON api_usage_events(company_id, created_at);
                """
            )
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_demand_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                    normalized_query TEXT NOT NULL,
                    display_query TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    result_count INTEGER,
                    source_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_demand_source_unique
                    ON market_demand_events(event_type, source_id) WHERE source_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_demand_normalized_created ON market_demand_events(normalized_query, created_at);
                CREATE INDEX IF NOT EXISTS idx_demand_actor_created ON market_demand_events(actor_company_id, created_at);

                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL DEFAULT 'inventory:read,catalog:read',
                    rate_limit_per_hour INTEGER NOT NULL DEFAULT 1000,
                    active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    last_used_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_company_active ON api_keys(company_id, active);

                CREATE TABLE IF NOT EXISTS api_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
                    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                    endpoint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_key_created ON api_usage_events(api_key_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_api_usage_company_created ON api_usage_events(company_id, created_at);
                """
            )
        _ensure_column(conn, "rfqs", "normalized_part TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "wanted_requests", "normalized_query TEXT NOT NULL DEFAULT ''")

        # Safe idempotent backfill. Existing RFQs/wanted requests become demand signals,
        # while searches are recorded only from Phase 6 onward (older search_match events
        # were one row per supplier match and cannot be reliably de-duplicated).
        rfqs = conn.execute(
            """SELECT r.id,r.part_number,r.quantity,r.created_at,u.company_id
               FROM rfqs r JOIN users u ON u.id=r.buyer_user_id"""
        ).fetchall()
        for row in rfqs:
            nq = normalize(row["part_number"])
            if not nq:
                continue
            if not scalar(conn, "SELECT COUNT(*) FROM market_demand_events WHERE event_type='rfq' AND source_id=?", (row["id"],)):
                conn.execute(
                    "INSERT INTO market_demand_events(event_type,actor_company_id,normalized_query,display_query,quantity,result_count,source_id,created_at) VALUES('rfq',?,?,?,?,?,?,?)",
                    (row["company_id"], nq, row["part_number"], row["quantity"] or 1, None, row["id"], row["created_at"]),
                )
            conn.execute("UPDATE rfqs SET normalized_part=? WHERE id=? AND (normalized_part='' OR normalized_part IS NULL)", (nq, row["id"]))

        wanted = conn.execute(
            """SELECT w.id,w.query,w.quantity,w.created_at,u.company_id
               FROM wanted_requests w JOIN users u ON u.id=w.buyer_user_id"""
        ).fetchall()
        for row in wanted:
            nq = normalize(row["query"])
            if not nq:
                continue
            if not scalar(conn, "SELECT COUNT(*) FROM market_demand_events WHERE event_type='wanted' AND source_id=?", (row["id"],)):
                conn.execute(
                    "INSERT INTO market_demand_events(event_type,actor_company_id,normalized_query,display_query,quantity,result_count,source_id,created_at) VALUES('wanted',?,?,?,?,?,?,?)",
                    (row["company_id"], nq, row["query"], row["quantity"] or 1, None, row["id"], row["created_at"]),
                )
            conn.execute("UPDATE wanted_requests SET normalized_query=? WHERE id=? AND (normalized_query='' OR normalized_query IS NULL)", (nq, row["id"]))


def record_demand_event(
    conn,
    event_type: str,
    *,
    company_id: int | None,
    query: str,
    quantity: int = 1,
    result_count: int | None = None,
    source_id: int | None = None,
    created_at: str | None = None,
) -> None:
    if event_type not in {"search", "rfq", "wanted"}:
        raise ValueError("Unsupported demand event type")
    nq = normalize(query)
    if len(nq) < 2:
        return
    if source_id is not None and scalar(conn, "SELECT COUNT(*) FROM market_demand_events WHERE event_type=? AND source_id=?", (event_type, source_id)):
        return
    # A user refreshing the same search repeatedly should not manufacture a demand trend.
    # RFQs/wanted requests remain separate because they represent deliberate procurement actions.
    if event_type == "search" and company_id is not None and SEARCH_DEMAND_DEDUP_MINUTES > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=SEARCH_DEMAND_DEDUP_MINUTES)).isoformat(timespec="seconds")
        duplicate = scalar(conn, "SELECT COUNT(*) FROM market_demand_events WHERE event_type='search' AND actor_company_id=? AND normalized_query=? AND created_at>=?", (company_id, nq, cutoff))
        if duplicate:
            return
    conn.execute(
        "INSERT INTO market_demand_events(event_type,actor_company_id,normalized_query,display_query,quantity,result_count,source_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (event_type, company_id, nq, (query or "").strip()[:240], max(1, int(quantity or 1)), result_count, source_id, created_at or now_iso()),
    )


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat(timespec="seconds")


def _live_supply_by_normalized(conn) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """SELECT i.normalized_part,i.company_id,i.quantity,i.last_confirmed_at,bs.status,bs.trial_ends_at
           FROM inventory i
           JOIN companies c ON c.id=i.company_id
           JOIN billing_subscriptions bs ON bs.company_id=c.id
           WHERE i.active=1 AND i.deleted_at IS NULL AND i.quantity>0 AND c.active=1 AND c.verified=1"""
    ).fetchall()
    now = datetime.now(timezone.utc)
    expiry_days = int(os.getenv("INVENTORY_EXPIRY_DAYS", "90"))
    cutoff = now - timedelta(days=expiry_days)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        confirmed = parse_iso(row["last_confirmed_at"])
        if not confirmed or confirmed < cutoff:
            continue
        active = row["status"] in {"active", "past_due"}
        if row["status"] == "trialing":
            trial_end = parse_iso(row["trial_ends_at"])
            active = bool(trial_end and trial_end > now)
        if not active:
            continue
        nq = row["normalized_part"] or ""
        if not nq:
            continue
        bucket = grouped.setdefault(nq, {"suppliers": set(), "units": 0})
        bucket["suppliers"].add(int(row["company_id"]))
        bucket["units"] += max(0, int(row["quantity"] or 0))
    return {k: {"supplier_count": len(v["suppliers"]), "units": v["units"]} for k, v in grouped.items()}


def network_demand(conn, days: int, *, admin: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    since = _since(days)
    rows = conn.execute(
        """SELECT normalized_query,MAX(display_query) AS display_query,
                  COUNT(*) AS events,
                  COUNT(DISTINCT actor_company_id) AS buyer_companies,
                  SUM(CASE WHEN event_type='search' THEN 1 ELSE 0 END) AS searches,
                  SUM(CASE WHEN event_type='rfq' THEN 1 ELSE 0 END) AS rfqs,
                  SUM(CASE WHEN event_type='wanted' THEN 1 ELSE 0 END) AS wanted,
                  SUM(CASE WHEN event_type IN ('rfq','wanted') THEN quantity ELSE 0 END) AS requested_units,
                  SUM(CASE WHEN event_type='search' AND result_count=0 THEN 1 ELSE 0 END) AS zero_result_searches
           FROM market_demand_events
           WHERE created_at>=? AND actor_company_id IS NOT NULL
           GROUP BY normalized_query""",
        (since,),
    ).fetchall()
    actor_rows = conn.execute(
        """SELECT normalized_query,actor_company_id,COUNT(*) AS n FROM market_demand_events
           WHERE created_at>=? AND actor_company_id IS NOT NULL GROUP BY normalized_query,actor_company_id""",
        (since,),
    ).fetchall()
    max_actor_counts: dict[str, int] = {}
    for actor in actor_rows:
        max_actor_counts[actor["normalized_query"]] = max(max_actor_counts.get(actor["normalized_query"], 0), int(actor["n"] or 0))
    supply = _live_supply_by_normalized(conn)
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        events = int(item["events"] or 0)
        item["max_contributor_share"] = round(max_actor_counts.get(item["normalized_query"], 0) / events, 3) if events else 0.0
        if not admin and (events < MIN_DEMAND_EVENTS or int(item["buyer_companies"] or 0) < MIN_BUYER_COMPANIES or item["max_contributor_share"] > MAX_CONTRIBUTOR_SHARE):
            continue
        stock = supply.get(item["normalized_query"], {"supplier_count": 0, "units": 0})
        item.update(stock)
        item["demand_score"] = int(item["searches"] or 0) + 4 * int(item["rfqs"] or 0) + 5 * int(item["wanted"] or 0)
        item["opportunity_score"] = round(item["demand_score"] / max(1, int(stock["supplier_count"]) + 1), 1)
        result.append(item)
    result.sort(key=lambda x: (-x["opportunity_score"], -x["demand_score"], x["display_query"]))
    return result[: max(1, limit)]


def supplier_opportunities(conn, company_id: int, days: int, *, admin: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    owned = {r["normalized_part"] for r in conn.execute("SELECT DISTINCT normalized_part FROM inventory WHERE company_id=? AND active=1 AND deleted_at IS NULL", (company_id,)).fetchall() if r["normalized_part"]}
    candidates = network_demand(conn, days, admin=admin, limit=200)
    result = []
    for item in candidates:
        if item["normalized_query"] in owned:
            continue
        if item["supplier_count"] > 5:
            continue
        reason = "No live supply" if item["supplier_count"] == 0 else f"Only {item['supplier_count']} live supplier{'s' if item['supplier_count'] != 1 else ''}"
        result.append({**item, "reason": reason})
    return result[:limit]


def pricing_intelligence(conn, query: str, days: int, *, admin: bool = False) -> dict[str, Any]:
    nq = normalize(query)
    result: dict[str, Any] = {
        "query": query,
        "normalized_query": nq,
        "available": False,
        "reason": "Not enough independent quote data yet.",
        "currencies": [],
        "quote_samples": 0,
        "supplier_samples": 0,
        "privacy": {
            "min_quotes": MIN_QUOTE_SAMPLES,
            "min_suppliers": MIN_QUOTE_SUPPLIERS,
            "min_buyer_companies": MIN_QUOTE_BUYER_COMPANIES,
        },
    }
    if len(nq) < 2:
        result["reason"] = "Enter a part number or identifier."
        return result
    since = _since(days)
    rows = conn.execute(
        """SELECT rr.quoted_price,rr.quoted_currency,rr.supplier_company_id,rr.status,r.normalized_part,r.part_number,u.company_id AS buyer_company_id
           FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id JOIN users u ON u.id=r.buyer_user_id
           WHERE rr.quoted_price IS NOT NULL AND rr.quoted_price>0 AND r.created_at>=?""",
        (since,),
    ).fetchall()
    matching = [r for r in rows if (r["normalized_part"] or normalize(r["part_number"])) == nq]
    result["quote_samples"] = len(matching)
    result["supplier_samples"] = len({int(r["supplier_company_id"]) for r in matching})
    result["buyer_company_samples"] = len({int(r["buyer_company_id"]) for r in matching})
    supplier_counts: dict[int, int] = {}
    buyer_counts: dict[int, int] = {}
    for row in matching:
        sid = int(row["supplier_company_id"]); supplier_counts[sid] = supplier_counts.get(sid, 0) + 1
        bid = int(row["buyer_company_id"]); buyer_counts[bid] = buyer_counts.get(bid, 0) + 1
    supplier_share = max(supplier_counts.values(), default=0) / len(matching) if matching else 0.0
    buyer_share = max(buyer_counts.values(), default=0) / len(matching) if matching else 0.0
    result["max_contributor_share"] = round(max(supplier_share, buyer_share), 3)
    if not admin and (result["quote_samples"] < MIN_QUOTE_SAMPLES or result["supplier_samples"] < MIN_QUOTE_SUPPLIERS or result["buyer_company_samples"] < MIN_QUOTE_BUYER_COMPANIES or result["max_contributor_share"] > MAX_CONTRIBUTOR_SHARE):
        if result["max_contributor_share"] > MAX_CONTRIBUTOR_SHARE:
            result["reason"] = "Quote pool is too concentrated in one company to publish safely."
        return result
    by_currency: dict[str, list[Any]] = {}
    for row in matching:
        by_currency.setdefault((row["quoted_currency"] or "GBP").upper(), []).append(row)
    stats = []
    for currency, bucket in sorted(by_currency.items()):
        prices = sorted(float(r["quoted_price"]) for r in bucket)
        supplier_count = len({int(r["supplier_company_id"]) for r in bucket})
        bucket_counts: dict[int, int] = {}
        bucket_buyer_counts: dict[int, int] = {}
        for row in bucket:
            sid = int(row["supplier_company_id"]); bucket_counts[sid] = bucket_counts.get(sid, 0) + 1
            bid = int(row["buyer_company_id"]); bucket_buyer_counts[bid] = bucket_buyer_counts.get(bid, 0) + 1
        buyer_count = len(bucket_buyer_counts)
        bucket_share = max(max(bucket_counts.values(), default=0), max(bucket_buyer_counts.values(), default=0)) / len(bucket) if bucket else 0.0
        if not admin and (len(prices) < MIN_QUOTE_SAMPLES or supplier_count < MIN_QUOTE_SUPPLIERS or buyer_count < MIN_QUOTE_BUYER_COMPANIES or bucket_share > MAX_CONTRIBUTOR_SHARE):
            continue
        accepted = [float(r["quoted_price"]) for r in bucket if r["status"] == "accepted"]
        stats.append({
            "currency": currency,
            "quote_count": len(prices),
            "supplier_count": supplier_count,
            "low": round(min(prices), 2),
            "median": round(float(statistics.median(prices)), 2),
            "high": round(max(prices), 2),
            "accepted_count": len(accepted),
            "accepted_median": round(float(statistics.median(accepted)), 2) if accepted else None,
        })
    result["currencies"] = stats
    result["available"] = bool(stats)
    if result["available"]:
        result["reason"] = "Aggregated from independent supplier quotes; individual quotes are never exposed."
    return result


def network_summary(conn, days: int, *, admin: bool = False) -> dict[str, Any]:
    since = _since(days)
    events = conn.execute(
        """SELECT event_type,COUNT(*) AS n,COUNT(DISTINCT actor_company_id) AS companies
           FROM market_demand_events WHERE created_at>=? AND actor_company_id IS NOT NULL GROUP BY event_type""",
        (since,),
    ).fetchall()
    counts = {r["event_type"]: int(r["n"] or 0) for r in events}
    buyers = int(scalar(conn, "SELECT COUNT(DISTINCT actor_company_id) FROM market_demand_events WHERE created_at>=? AND actor_company_id IS NOT NULL", (since,)) or 0)
    quotes = int(scalar(conn, "SELECT COUNT(*) FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.quoted_price IS NOT NULL AND r.created_at>=?", (since,)) or 0)
    awards = int(scalar(conn, "SELECT COUNT(*) FROM rfq_recipients rr JOIN rfqs r ON r.id=rr.rfq_id WHERE rr.status='accepted' AND r.created_at>=?", (since,)) or 0)
    live_supply = _live_supply_by_normalized(conn)
    return {
        "days": days,
        "searches": counts.get("search", 0),
        "rfqs": counts.get("rfq", 0),
        "wanted": counts.get("wanted", 0),
        "buyer_companies": buyers,
        "quotes": quotes,
        "awards": awards,
        "quote_to_award_rate": round(awards / quotes * 100, 1) if quotes else None,
        "live_part_numbers": len(live_supply),
    }


def part_intelligence(conn, query: str, days: int, *, admin: bool = False) -> dict[str, Any]:
    nq = normalize(query)
    demand_rows = network_demand(conn, days, admin=admin, limit=500)
    demand = next((x for x in demand_rows if x["normalized_query"] == nq), None)
    pricing = pricing_intelligence(conn, query, days, admin=admin)
    if demand is None:
        demand = {
            "normalized_query": nq,
            "display_query": query,
            "events": 0,
            "buyer_companies": 0,
            "searches": 0,
            "rfqs": 0,
            "wanted": 0,
            "requested_units": 0,
            "zero_result_searches": 0,
            "supplier_count": _live_supply_by_normalized(conn).get(nq, {}).get("supplier_count", 0),
            "units": _live_supply_by_normalized(conn).get(nq, {}).get("units", 0),
            "private": not admin,
        }
    return {"demand": demand, "pricing": pricing, "days": days}


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(conn, company_id: int, user_id: int, name: str, scopes: set[str] | None = None, *, rate_limit: int | None = None, expires_at: str | None = None) -> tuple[str, int]:
    scopes = set(scopes or {"inventory:read", "catalog:read"}) & API_SCOPES
    if not scopes:
        scopes = {"inventory:read"}
    raw = "cxk_" + secrets.token_urlsafe(36)
    prefix = raw[:12]
    rate = max(10, min(MAX_API_RATE_PER_HOUR, int(rate_limit or DEFAULT_API_RATE_PER_HOUR)))
    key_id = insert_id(
        conn,
        "INSERT INTO api_keys(company_id,created_by_user_id,name,key_prefix,key_hash,scopes,rate_limit_per_hour,active,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (company_id, user_id, (name or "ERP integration").strip()[:100], prefix, _hash_key(raw), ",".join(sorted(scopes)), rate, 1, expires_at, now_iso()),
    )
    return raw, key_id


def api_key_rows(conn, company_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM api_keys WHERE company_id=? ORDER BY active DESC,created_at DESC", (company_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(conn, company_id: int, key_id: int) -> bool:
    row = conn.execute("SELECT id FROM api_keys WHERE id=? AND company_id=?", (key_id, company_id)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE api_keys SET active=0,revoked_at=? WHERE id=?", (now_iso(), key_id))
    return True


def authenticate_api_key(conn, raw_key: str, required_scope: str, endpoint: str) -> tuple[dict[str, Any] | None, str | None]:
    if not raw_key or not raw_key.startswith("cxk_"):
        return None, "invalid_key"
    row = conn.execute(
        """SELECT k.*,c.active AS company_active,c.verified,c.company_type,c.account_kind
           FROM api_keys k JOIN companies c ON c.id=k.company_id WHERE k.key_hash=?""",
        (_hash_key(raw_key),),
    ).fetchone()
    if not row or not row["active"] or not row["company_active"]:
        return None, "invalid_key"
    expiry = parse_iso(row["expires_at"])
    if expiry and expiry <= datetime.now(timezone.utc):
        return None, "expired_key"
    scopes = {s for s in (row["scopes"] or "").split(",") if s}
    if required_scope not in scopes:
        return None, "missing_scope"
    sub = subscription_for(conn, int(row["company_id"]))
    if not sub["access_active"]:
        return None, "commercial_access_paused"
    plan = effective_plan(conn, int(row["company_id"]))
    if int(plan.get("api_key_limit", 0) or 0) <= 0:
        return None, "plan_no_api_access"
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    used = int(scalar(conn, "SELECT COUNT(*) FROM api_usage_events WHERE api_key_id=? AND created_at>=?", (row["id"], since)) or 0)
    limit = min(int(row["rate_limit_per_hour"] or DEFAULT_API_RATE_PER_HOUR), int(plan.get("api_rate_per_hour", DEFAULT_API_RATE_PER_HOUR) or DEFAULT_API_RATE_PER_HOUR))
    if used >= limit:
        return None, "rate_limited"
    conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    data = dict(row)
    data["scopes_set"] = scopes
    data["rate_limit"] = limit
    data["rate_used"] = used
    data["endpoint"] = endpoint
    return data, None


def record_api_usage(conn, key: dict[str, Any] | None, endpoint: str, status_code: int) -> None:
    if not key:
        return
    conn.execute(
        "INSERT INTO api_usage_events(api_key_id,company_id,endpoint,status_code,created_at) VALUES(?,?,?,?,?)",
        (key["id"], key["company_id"], endpoint[:160], int(status_code), now_iso()),
    )


def api_usage_summary(conn, company_id: int, days: int = 30) -> dict[str, Any]:
    since = _since(days)
    total = int(scalar(conn, "SELECT COUNT(*) FROM api_usage_events WHERE company_id=? AND created_at>=?", (company_id, since)) or 0)
    by_endpoint = conn.execute(
        "SELECT endpoint,COUNT(*) AS hits FROM api_usage_events WHERE company_id=? AND created_at>=? GROUP BY endpoint ORDER BY hits DESC LIMIT 12",
        (company_id, since),
    ).fetchall()
    return {"days": days, "requests": total, "by_endpoint": [dict(r) for r in by_endpoint]}
