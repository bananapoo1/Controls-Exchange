#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sys
import time
from email import policy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import process_inventory_match_alerts  # noqa: E402
from inventory_ingestion import (  # noqa: E402
    due_feed_ids,
    feed_secrets,
    guess_column_mapping,
    inbound_feed_by_token,
    mark_feed_failed,
    read_tabular_file,
    run_feed,
    run_import,
)
from platform_core import db, init_db, logger, now_iso, send_email  # noqa: E402
from billing import init_billing_schema, process_trial_reminders  # noqa: E402

SUPPORTED = {".csv", ".xlsx", ".xlsm"}


def notify_feed_failure(feed_id: int, message: str) -> None:
    with db() as conn:
        feed = conn.execute("SELECT * FROM inventory_feeds WHERE id=?", (feed_id,)).fetchone()
        if not feed:
            return
        users = conn.execute(
            "SELECT email FROM users WHERE company_id=? AND active=1 AND email_verified=1 AND company_role IN ('owner','admin')",
            (feed["company_id"],),
        ).fetchall()
    subject = f"Controls Exchange inventory feed failed: {feed['name']}"
    body = f"The inventory feed '{feed['name']}' failed at {now_iso()}.\n\n{message}\n\nOpen Inventory feeds to review the connection and recent import reports."
    for user in users:
        send_email(user["email"], subject, body)


def run_due_feeds_once() -> int:
    processed = 0
    for feed_id in due_feed_ids(limit=20):
        try:
            _job_id, changed_ids, _counts = run_feed(feed_id)
            process_inventory_match_alerts(changed_ids)
        except Exception as exc:
            logger.exception("scheduled_feed_failed feed_id=%s", feed_id)
            mark_feed_failed(feed_id, str(exc))
            notify_feed_failure(feed_id, str(exc))
        processed += 1
    return processed


def _recipient_tokens(message) -> list[str]:
    domain = os.getenv("INVENTORY_IMPORT_EMAIL_DOMAIN", "").strip().lower()
    if not domain:
        return []
    values = []
    for header in ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To"):
        values.extend(message.get_all(header, []))
    text = " ".join(values)
    pattern = re.compile(rf"imports\+([A-Za-z0-9_-]+)@{re.escape(domain)}", re.I)
    return list(dict.fromkeys(pattern.findall(text)))


def _first_inventory_attachment(message):
    for part in message.iter_attachments():
        filename = part.get_filename() or ""
        if Path(filename).suffix.lower() not in SUPPORTED:
            continue
        payload = part.get_payload(decode=True) or b""
        return filename, payload
    return None, None


def _process_email_message(message) -> bool:
    tokens = _recipient_tokens(message)
    if not tokens:
        return False
    filename, raw = _first_inventory_attachment(message)
    if not filename or raw is None:
        logger.warning("inventory_email_no_supported_attachment subject=%s", message.get("Subject", ""))
        return True
    for raw_token in tokens:
        feed = inbound_feed_by_token(raw_token, ("email",))
        if not feed:
            continue
        try:
            parsed = read_tabular_file(filename, raw)
            import json
            mapping = json.loads(feed["mapping_json"] or "{}") or guess_column_mapping(parsed.headers)
            if not mapping.get("part_number"):
                raise ValueError("Could not identify part-number column; configure the feed mapping.")
            job_id, changed_ids, _counts = run_import(
                company_id=feed["company_id"],
                user_id=None,
                feed_id=feed["id"],
                source_type="email_imap",
                source_name=filename,
                raw_rows=parsed.rows,
                mapping=mapping,
                import_mode=feed["import_mode"],
                duplicate_policy=feed["duplicate_policy"],
                default_location=feed["company_location"] or "",
            )
            with db() as conn:
                conn.execute("UPDATE inventory_feeds SET last_run_at=?,last_status='success',updated_at=? WHERE id=?", (now_iso(), now_iso(), feed["id"]))
            process_inventory_match_alerts(changed_ids)
            logger.info("inventory_email_imported feed_id=%s job_id=%s", feed["id"], job_id)
        except Exception as exc:
            logger.exception("inventory_email_processing_failed feed_id=%s", feed["id"])
            mark_feed_failed(feed["id"], str(exc))
            notify_feed_failure(feed["id"], str(exc))
        return True
    return False


def poll_imap_once() -> int:
    host = os.getenv("INVENTORY_IMPORT_IMAP_HOST", "").strip()
    username = os.getenv("INVENTORY_IMPORT_IMAP_USER", "").strip()
    password = os.getenv("INVENTORY_IMPORT_IMAP_PASSWORD", "")
    if not host or not username or not password or not os.getenv("INVENTORY_IMPORT_EMAIL_DOMAIN", "").strip():
        return 0
    port = int(os.getenv("INVENTORY_IMPORT_IMAP_PORT", "993"))
    use_ssl = os.getenv("INVENTORY_IMPORT_IMAP_SSL", "true").lower() == "true"
    folder = os.getenv("INVENTORY_IMPORT_IMAP_FOLDER", "INBOX")
    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    processed = 0
    try:
        client.login(username, password)
        status, _ = client.select(folder)
        if status != "OK":
            raise RuntimeError(f"Could not select IMAP folder {folder}")
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            return 0
        for msg_id in data[0].split()[:50]:
            status, payload = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1], policy=policy.default)
            attempted = _process_email_message(message)
            if attempted:
                client.store(msg_id, "+FLAGS", "\\Seen")
                processed += 1
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Controls Exchange automated inventory feed worker")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=int(os.getenv("FEED_WORKER_INTERVAL_SECONDS", "60")))
    args = parser.parse_args()
    init_db()
    init_billing_schema()
    while True:
        run_due_feeds_once()
        try:
            process_trial_reminders()
        except Exception:
            logger.exception("billing_reminder_processing_failed")
        try:
            poll_imap_once()
        except Exception:
            logger.exception("imap_poll_failed")
        if not args.loop:
            break
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
