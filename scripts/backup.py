#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import shutil
import sqlite3
import subprocess
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE / "data" / "controls_exchange.db")))
if not DB_PATH.is_absolute(): DB_PATH = BASE / DB_PATH
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(BASE / "backups")))
if not BACKUP_DIR.is_absolute(): BACKUP_DIR = BASE / BACKUP_DIR
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))
INTERVAL = int(os.getenv("BACKUP_INTERVAL_SECONDS", "21600"))
ORDER_DOCUMENT_DIR = Path(os.getenv("ORDER_DOCUMENT_DIR", str(BASE / "data" / "order_documents")))
if not ORDER_DOCUMENT_DIR.is_absolute(): ORDER_DOCUMENT_DIR = BASE / ORDER_DOCUMENT_DIR


def timestamp(): return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def backup_once() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_URL.startswith(("postgresql://", "postgres://")):
        target = BACKUP_DIR / f"controls_exchange_{timestamp()}.dump"
        pg_dump = shutil.which("pg_dump")
        if not pg_dump: raise RuntimeError("pg_dump is required for PostgreSQL backups")
        subprocess.run([pg_dump, "--format=custom", "--no-owner", "--no-acl", "--file", str(target), DATABASE_URL], check=True)
    else:
        if not DB_PATH.exists(): raise RuntimeError(f"SQLite database not found: {DB_PATH}")
        target = BACKUP_DIR / f"controls_exchange_{timestamp()}.sqlite3"
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(target)
        try: source.backup(dest)
        finally: dest.close(); source.close()
    documents_target = None
    if ORDER_DOCUMENT_DIR.exists():
        documents_target = BACKUP_DIR / f"controls_exchange_documents_{target.stem.replace('controls_exchange_', '')}.tar.gz"
        with tarfile.open(documents_target, "w:gz") as archive:
            archive.add(ORDER_DOCUMENT_DIR, arcname="order_documents")
    prune()
    print(target, flush=True)
    if documents_target: print(documents_target, flush=True)
    return target


def prune() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for path in BACKUP_DIR.glob("controls_exchange_*"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff: path.unlink()
        except FileNotFoundError: pass


def main():
    parser = argparse.ArgumentParser(description="Create Controls Exchange database backups")
    parser.add_argument("--loop", action="store_true", help="Run forever at BACKUP_INTERVAL_SECONDS")
    args = parser.parse_args()
    if not args.loop: backup_once(); return
    while True:
        try: backup_once()
        except Exception as exc: print(f"backup failed: {exc}", flush=True)
        time.sleep(max(300, INTERVAL))

if __name__ == "__main__": main()
