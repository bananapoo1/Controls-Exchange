from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from platform_core import (
    BASE_DIR,
    ENVIRONMENT,
    IS_POSTGRES,
    _ensure_column,
    db,
    decrypt_secret_dict,
    encrypt_secret_dict,
    insert_id,
    now_iso,
    parse_iso,
    scalar,
)

ORDER_DOCUMENT_DIR = Path(os.getenv("ORDER_DOCUMENT_DIR", str(BASE_DIR / "data" / "order_documents")))
if not ORDER_DOCUMENT_DIR.is_absolute():
    ORDER_DOCUMENT_DIR = BASE_DIR / ORDER_DOCUMENT_DIR
ORDER_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
ORDER_DOCUMENT_MAX_BYTES = max(1024 * 1024, int(os.getenv("ORDER_DOCUMENT_MAX_BYTES", str(10 * 1024 * 1024))))
WEBHOOK_MAX_ATTEMPTS = max(1, int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "8")))
WEBHOOK_TIMEOUT_SECONDS = max(2, int(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "15")))
WEBHOOK_WORKER_INTERVAL_SECONDS = max(5, int(os.getenv("WEBHOOK_WORKER_INTERVAL_SECONDS", "20")))

ORDER_STATUSES = (
    "awaiting_buyer_po",
    "submitted",
    "acknowledged",
    "processing",
    "dispatched",
    "delivered",
    "cancelled",
)
WEBHOOK_EVENTS = {
    "order.created",
    "order.submitted",
    "order.acknowledged",
    "order.processing",
    "order.dispatched",
    "order.delivered",
    "order.cancelled",
    "order.message_added",
    "order.document_added",
}
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xlsm", ".docx", ".txt", ".jpg", ".jpeg", ".png"}


def init_procurement_schema() -> None:
    with db() as conn:
        if IS_POSTGRES:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    rfq_id BIGINT NOT NULL REFERENCES rfqs(id) ON DELETE RESTRICT,
                    rfq_recipient_id BIGINT NOT NULL UNIQUE REFERENCES rfq_recipients(id) ON DELETE RESTRICT,
                    buyer_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
                    supplier_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    part_number TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    quoted_price DOUBLE PRECISION NOT NULL,
                    quoted_currency TEXT NOT NULL DEFAULT 'GBP',
                    status TEXT NOT NULL DEFAULT 'awaiting_buyer_po',
                    buyer_po_number TEXT NOT NULL DEFAULT '',
                    buyer_reference TEXT NOT NULL DEFAULT '',
                    supplier_order_reference TEXT NOT NULL DEFAULT '',
                    delivery_address TEXT NOT NULL DEFAULT '',
                    delivery_contact TEXT NOT NULL DEFAULT '',
                    required_by TEXT NOT NULL DEFAULT '',
                    expected_dispatch_date TEXT NOT NULL DEFAULT '',
                    carrier TEXT NOT NULL DEFAULT '',
                    tracking_number TEXT NOT NULL DEFAULT '',
                    tracking_url TEXT NOT NULL DEFAULT '',
                    cancellation_reason TEXT NOT NULL DEFAULT '',
                    acknowledged_at TEXT,
                    dispatched_at TEXT,
                    delivered_at TEXT,
                    cancelled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders(buyer_company_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_supplier_created ON orders(supplier_company_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

                CREATE TABLE IF NOT EXISTS order_messages (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    sender_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_messages_order ON order_messages(order_id, created_at);

                CREATE TABLE IF NOT EXISTS order_documents (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    uploaded_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    document_type TEXT NOT NULL DEFAULT 'other',
                    original_filename TEXT NOT NULL,
                    stored_filename TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size_bytes BIGINT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_documents_order ON order_documents(order_id, created_at);

                CREATE TABLE IF NOT EXISTS order_events (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, created_at);

                CREATE TABLE IF NOT EXISTS webhook_endpoints (
                    id BIGSERIAL PRIMARY KEY,
                    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    event_types TEXT NOT NULL,
                    secret_blob TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_company ON webhook_endpoints(company_id, active);

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id BIGSERIAL PRIMARY KEY,
                    endpoint_id BIGINT REFERENCES webhook_endpoints(id) ON DELETE SET NULL,
                    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    response_status INTEGER,
                    response_excerpt TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(endpoint_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_due ON webhook_deliveries(status, next_attempt_at);
                """
            )
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rfq_id INTEGER NOT NULL REFERENCES rfqs(id) ON DELETE RESTRICT,
                    rfq_recipient_id INTEGER NOT NULL UNIQUE REFERENCES rfq_recipients(id) ON DELETE RESTRICT,
                    buyer_company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
                    supplier_company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    part_number TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    quoted_price REAL NOT NULL,
                    quoted_currency TEXT NOT NULL DEFAULT 'GBP',
                    status TEXT NOT NULL DEFAULT 'awaiting_buyer_po',
                    buyer_po_number TEXT NOT NULL DEFAULT '',
                    buyer_reference TEXT NOT NULL DEFAULT '',
                    supplier_order_reference TEXT NOT NULL DEFAULT '',
                    delivery_address TEXT NOT NULL DEFAULT '',
                    delivery_contact TEXT NOT NULL DEFAULT '',
                    required_by TEXT NOT NULL DEFAULT '',
                    expected_dispatch_date TEXT NOT NULL DEFAULT '',
                    carrier TEXT NOT NULL DEFAULT '',
                    tracking_number TEXT NOT NULL DEFAULT '',
                    tracking_url TEXT NOT NULL DEFAULT '',
                    cancellation_reason TEXT NOT NULL DEFAULT '',
                    acknowledged_at TEXT,
                    dispatched_at TEXT,
                    delivered_at TEXT,
                    cancelled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders(buyer_company_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_supplier_created ON orders(supplier_company_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

                CREATE TABLE IF NOT EXISTS order_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    sender_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_messages_order ON order_messages(order_id, created_at);

                CREATE TABLE IF NOT EXISTS order_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    uploaded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    document_type TEXT NOT NULL DEFAULT 'other',
                    original_filename TEXT NOT NULL,
                    stored_filename TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_documents_order ON order_documents(order_id, created_at);

                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, created_at);

                CREATE TABLE IF NOT EXISTS webhook_endpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    event_types TEXT NOT NULL,
                    secret_blob TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_company ON webhook_endpoints(company_id, active);

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint_id INTEGER REFERENCES webhook_endpoints(id) ON DELETE SET NULL,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    response_status INTEGER,
                    response_excerpt TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(endpoint_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_due ON webhook_deliveries(status, next_attempt_at);
                """
            )

        accepted = conn.execute(
            """SELECT r.id AS rfq_id,r.buyer_user_id,rr.id AS recipient_id
               FROM rfqs r JOIN rfq_recipients rr ON rr.rfq_id=r.id
               WHERE rr.status='accepted' AND rr.quoted_price IS NOT NULL"""
        ).fetchall()
        for row in accepted:
            if not scalar(conn, "SELECT COUNT(*) FROM orders WHERE rfq_recipient_id=?", (row["recipient_id"],)):
                create_order_from_selected_quote(conn, int(row["rfq_id"]), int(row["recipient_id"]), int(row["buyer_user_id"]))


def order_snapshot(conn, order_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT o.*,bc.name AS buyer_company,sc.name AS supplier_company
           FROM orders o
           JOIN companies bc ON bc.id=o.buyer_company_id
           JOIN companies sc ON sc.id=o.supplier_company_id
           WHERE o.id=?""",
        (order_id,),
    ).fetchone()
    return dict(row) if row else None


def order_for_company(conn, order_id: int, company_id: int, *, admin: bool = False):
    row = conn.execute(
        """SELECT o.*,bc.name AS buyer_company,sc.name AS supplier_company
           FROM orders o JOIN companies bc ON bc.id=o.buyer_company_id JOIN companies sc ON sc.id=o.supplier_company_id
           WHERE o.id=?""",
        (order_id,),
    ).fetchone()
    if not row:
        return None
    if not admin and company_id not in {row["buyer_company_id"], row["supplier_company_id"]}:
        return None
    return row


def create_order_from_selected_quote(conn, rfq_id: int, recipient_id: int, buyer_user_id: int) -> tuple[int, bool]:
    existing = conn.execute("SELECT id FROM orders WHERE rfq_recipient_id=?", (recipient_id,)).fetchone()
    if existing:
        return int(existing["id"]), False
    row = conn.execute(
        """SELECT r.id AS rfq_id,r.part_number,r.quantity,r.required_by,u.company_id AS buyer_company_id,
                  rr.id AS recipient_id,rr.supplier_company_id,rr.quoted_price,rr.quoted_currency
           FROM rfqs r JOIN users u ON u.id=r.buyer_user_id
           JOIN rfq_recipients rr ON rr.rfq_id=r.id
           WHERE r.id=? AND rr.id=? AND rr.status='accepted' AND rr.quoted_price IS NOT NULL""",
        (rfq_id, recipient_id),
    ).fetchone()
    if not row:
        raise ValueError("Selected quote is not eligible for order creation")
    now = now_iso()
    order_id = insert_id(
        conn,
        """INSERT INTO orders(rfq_id,rfq_recipient_id,buyer_company_id,supplier_company_id,created_by_user_id,
               part_number,quantity,quoted_price,quoted_currency,status,required_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["rfq_id"], row["recipient_id"], row["buyer_company_id"], row["supplier_company_id"], buyer_user_id,
            row["part_number"], row["quantity"], row["quoted_price"], row["quoted_currency"] or "GBP",
            "awaiting_buyer_po", row["required_by"] or "", now, now,
        ),
    )
    conn.execute(
        "INSERT INTO order_events(order_id,actor_user_id,event_type,metadata,created_at) VALUES(?,?,?,?,?)",
        (order_id, buyer_user_id, "order.created", json.dumps({"rfq_id": rfq_id, "rfq_recipient_id": recipient_id}), now),
    )
    return order_id, True


def order_payload(conn, order_id: int) -> dict[str, Any] | None:
    order = order_snapshot(conn, order_id)
    if not order:
        return None
    return {
        "id": order["id"],
        "rfq_id": order["rfq_id"],
        "buyer": {"id": order["buyer_company_id"], "name": order["buyer_company"]},
        "supplier": {"id": order["supplier_company_id"], "name": order["supplier_company"]},
        "part_number": order["part_number"],
        "quantity": order["quantity"],
        "quote": {"price": order["quoted_price"], "currency": order["quoted_currency"]},
        "status": order["status"],
        "buyer_po_number": order["buyer_po_number"],
        "buyer_reference": order["buyer_reference"],
        "supplier_order_reference": order["supplier_order_reference"],
        "delivery_address": order["delivery_address"],
        "delivery_contact": order["delivery_contact"],
        "required_by": order["required_by"],
        "expected_dispatch_date": order["expected_dispatch_date"],
        "carrier": order["carrier"],
        "tracking_number": order["tracking_number"],
        "tracking_url": order["tracking_url"],
        "created_at": order["created_at"],
        "updated_at": order["updated_at"],
    }


def record_order_event(conn, order_id: int, actor_user_id: int | None, event_type: str, metadata: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO order_events(order_id,actor_user_id,event_type,metadata,created_at) VALUES(?,?,?,?,?)",
        (order_id, actor_user_id, event_type, json.dumps(metadata or {}, separators=(",", ":"), default=str), now_iso()),
    )


def _safe_webhook_host(hostname: str) -> None:
    if ENVIRONMENT != "production" or os.getenv("ALLOW_PRIVATE_WEBHOOK_HOSTS", "false").lower() == "true":
        return
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError("Webhook host did not resolve")
    for info in infos:
        address = info[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Webhook URL resolves to a private/non-public address")


def validate_webhook_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Webhook URL must be http(s)")
    if parsed.username or parsed.password:
        raise ValueError("Do not embed credentials in webhook URLs")
    if ENVIRONMENT == "production" and parsed.scheme != "https":
        raise ValueError("Production webhooks must use HTTPS")
    _safe_webhook_host(parsed.hostname)
    return url[:1000]


def new_webhook_secret() -> str:
    return "cxwhsec_" + secrets.token_urlsafe(32)


def endpoint_secret(row: Any) -> str:
    data = decrypt_secret_dict(row["secret_blob"])
    return str(data.get("signing_secret", ""))


def create_webhook_endpoint(conn, company_id: int, user_id: int, name: str, url: str, event_types: set[str]) -> tuple[int, str]:
    url = validate_webhook_url(url)
    selected = sorted(WEBHOOK_EVENTS.intersection(event_types))
    if not selected:
        raise ValueError("Select at least one webhook event")
    secret = new_webhook_secret()
    now = now_iso()
    endpoint_id = insert_id(
        conn,
        "INSERT INTO webhook_endpoints(company_id,created_by_user_id,name,url,event_types,secret_blob,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (company_id, user_id, (name or "Order integration")[:120], url, ",".join(selected), encrypt_secret_dict({"signing_secret": secret}), 1, now, now),
    )
    return endpoint_id, secret


def rotate_webhook_secret(conn, company_id: int, endpoint_id: int) -> str | None:
    row = conn.execute("SELECT id FROM webhook_endpoints WHERE id=? AND company_id=?", (endpoint_id, company_id)).fetchone()
    if not row:
        return None
    secret = new_webhook_secret()
    conn.execute("UPDATE webhook_endpoints SET secret_blob=?,updated_at=? WHERE id=?", (encrypt_secret_dict({"signing_secret": secret}), now_iso(), endpoint_id))
    return secret


def queue_order_webhooks(conn, order_id: int, event_type: str) -> int:
    if event_type not in WEBHOOK_EVENTS:
        return 0
    payload_order = order_payload(conn, order_id)
    if not payload_order:
        return 0
    event_id = "evt_" + secrets.token_urlsafe(18)
    envelope = {
        "id": event_id,
        "type": event_type,
        "created_at": now_iso(),
        "data": {"order": payload_order},
    }
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    count = 0
    for company_id in {payload_order["buyer"]["id"], payload_order["supplier"]["id"]}:
        endpoints = conn.execute("SELECT * FROM webhook_endpoints WHERE company_id=? AND active=1", (company_id,)).fetchall()
        for endpoint in endpoints:
            events = {e for e in (endpoint["event_types"] or "").split(",") if e}
            if event_type not in events:
                continue
            delivery_event_id = f"{event_id}:{endpoint['id']}"
            conn.execute(
                """INSERT INTO webhook_deliveries(endpoint_id,company_id,event_type,event_id,payload,status,attempts,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',0,?,?,?)""",
                (endpoint["id"], company_id, event_type, delivery_event_id, payload, now_iso(), now_iso(), now_iso()),
            )
            count += 1
    return count


def _retry_at(attempts: int) -> str:
    minutes = min(360, 2 ** min(max(attempts, 1), 8))
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def deliver_webhook(conn, delivery: Any) -> bool:
    endpoint = conn.execute("SELECT * FROM webhook_endpoints WHERE id=?", (delivery["endpoint_id"],)).fetchone()
    if not endpoint or not endpoint["active"]:
        conn.execute("UPDATE webhook_deliveries SET status='cancelled',updated_at=? WHERE id=?", (now_iso(), delivery["id"]))
        return False
    try:
        url = validate_webhook_url(endpoint["url"])
        secret = endpoint_secret(endpoint)
        if not secret:
            raise ValueError("Webhook signing secret unavailable")
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        signed = f"{timestamp}.{delivery['payload']}".encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        response = httpx.post(
            url,
            content=delivery["payload"].encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ControlsExchange-Webhooks/1.0",
                "X-CX-Event": delivery["event_type"],
                "X-CX-Event-ID": delivery["event_id"],
                "X-CX-Timestamp": timestamp,
                "X-CX-Signature": f"v1={signature}",
            },
            timeout=WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if 200 <= response.status_code < 300:
            conn.execute(
                "UPDATE webhook_deliveries SET status='delivered',attempts=attempts+1,response_status=?,response_excerpt=?,delivered_at=?,last_error='',updated_at=? WHERE id=?",
                (response.status_code, response.text[:500], now_iso(), now_iso(), delivery["id"]),
            )
            conn.execute("UPDATE webhook_endpoints SET last_success_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), endpoint["id"]))
            return True
        error = f"HTTP {response.status_code}"
        excerpt = response.text[:500]
    except Exception as exc:
        error = str(exc)[:500]
        excerpt = ""
        response = None
    attempts = int(delivery["attempts"] or 0) + 1
    status = "failed" if attempts >= WEBHOOK_MAX_ATTEMPTS else "pending"
    conn.execute(
        "UPDATE webhook_deliveries SET status=?,attempts=?,next_attempt_at=?,response_status=?,response_excerpt=?,last_error=?,updated_at=? WHERE id=?",
        (status, attempts, _retry_at(attempts), getattr(response, "status_code", None), excerpt, error, now_iso(), delivery["id"]),
    )
    conn.execute("UPDATE webhook_endpoints SET last_failure_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), endpoint["id"]))
    return False


def process_due_webhooks(limit: int = 50) -> dict[str, int]:
    stats = {"claimed": 0, "delivered": 0, "failed": 0}
    with db() as conn:
        if IS_POSTGRES:
            rows = conn.execute(
                """SELECT * FROM webhook_deliveries WHERE status='pending' AND next_attempt_at<=?
                   ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT ?""",
                (now_iso(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT ?",
                (now_iso(), limit),
            ).fetchall()
        for row in rows:
            stats["claimed"] += 1
            if deliver_webhook(conn, row):
                stats["delivered"] += 1
            else:
                stats["failed"] += 1
    return stats


def save_order_document(order_id: int, user_id: int, filename: str, raw: bytes, document_type: str) -> dict[str, Any]:
    if len(raw) > ORDER_DOCUMENT_MAX_BYTES:
        raise ValueError(f"Document exceeds the {ORDER_DOCUMENT_MAX_BYTES // (1024*1024)} MB limit")
    original = Path(filename or "document").name[:240]
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError("Unsupported file type. Use PDF, CSV, XLSX, DOCX, TXT, JPG or PNG.")
    digest = hashlib.sha256(raw).hexdigest()
    stored = f"{secrets.token_hex(16)}{ext}"
    path = ORDER_DOCUMENT_DIR / stored
    path.write_bytes(raw)
    content_type = mimetypes.guess_type(original)[0] or "application/octet-stream"
    with db() as conn:
        doc_id = insert_id(
            conn,
            """INSERT INTO order_documents(order_id,uploaded_by_user_id,document_type,original_filename,stored_filename,content_type,size_bytes,sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (order_id, user_id, (document_type or "other")[:40], original, stored, content_type, len(raw), digest, now_iso()),
        )
    return {"id": doc_id, "stored_filename": stored, "original_filename": original, "content_type": content_type, "size_bytes": len(raw), "sha256": digest}


def document_path(stored_filename: str) -> Path:
    return ORDER_DOCUMENT_DIR / Path(stored_filename).name
