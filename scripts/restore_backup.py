#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE / "data" / "controls_exchange.db")))
if not DB_PATH.is_absolute(): DB_PATH = BASE / DB_PATH
ORDER_DOCUMENT_DIR = Path(os.getenv("ORDER_DOCUMENT_DIR", str(BASE / "data" / "order_documents")))
if not ORDER_DOCUMENT_DIR.is_absolute(): ORDER_DOCUMENT_DIR = BASE / ORDER_DOCUMENT_DIR


def main():
    p=argparse.ArgumentParser(description="Restore a Controls Exchange database backup")
    p.add_argument("backup", type=Path); p.add_argument("--documents-backup", type=Path, help="Optional matching order-document tar.gz backup")
    p.add_argument("--yes", action="store_true", help="Confirm destructive restore")
    args=p.parse_args(); backup=args.backup.resolve()
    if not backup.exists(): raise SystemExit(f"Backup not found: {backup}")
    if not args.yes: raise SystemExit("Restore is destructive. Re-run with --yes after stopping the app.")
    if DATABASE_URL.startswith(("postgresql://","postgres://")):
        pg_restore=shutil.which("pg_restore")
        if not pg_restore: raise SystemExit("pg_restore is required")
        subprocess.run([pg_restore,"--clean","--if-exists","--no-owner","--no-acl","--dbname",DATABASE_URL,str(backup)],check=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        check=sqlite3.connect(backup); check.execute("PRAGMA integrity_check").fetchone(); check.close()
        temp=DB_PATH.with_suffix(DB_PATH.suffix+".restore")
        shutil.copy2(backup,temp); temp.replace(DB_PATH)
    if args.documents_backup:
        docs = args.documents_backup.resolve()
        if not docs.exists(): raise SystemExit(f"Documents backup not found: {docs}")
        if ORDER_DOCUMENT_DIR.exists(): shutil.rmtree(ORDER_DOCUMENT_DIR)
        ORDER_DOCUMENT_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(docs, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                resolved = (ORDER_DOCUMENT_DIR.parent / member.name).resolve()
                if ORDER_DOCUMENT_DIR.parent.resolve() not in resolved.parents and resolved != ORDER_DOCUMENT_DIR.parent.resolve():
                    raise SystemExit("Unsafe path in documents backup")
            archive.extractall(ORDER_DOCUMENT_DIR.parent, filter="data")
    print("restore complete")
if __name__=="__main__": main()
