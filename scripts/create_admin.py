#!/usr/bin/env python3
from __future__ import annotations
import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core import db, hash_password, init_db, insert_id, now_iso, scalar


def main():
    parser = argparse.ArgumentParser(description="Create the first Controls Exchange platform admin")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Platform Admin")
    parser.add_argument("--password")
    args = parser.parse_args()
    email = args.email.strip().lower()
    password = args.password or getpass.getpass("Admin password: ")
    if "@" not in email or len(password) < 14:
        raise SystemExit("Use a valid email and a password of at least 14 characters.")
    init_db()
    with db() as conn:
        if scalar(conn, "SELECT COUNT(*) FROM users WHERE role='admin'"):
            raise SystemExit("A platform admin already exists. Refusing to create another through bootstrap.")
        company = conn.execute("SELECT id FROM companies WHERE company_type='admin' ORDER BY id LIMIT 1").fetchone()
        if company:
            company_id = company["id"]
        else:
            company_id = insert_id(conn, "INSERT INTO companies(name,company_type,location,verified,active,created_at) VALUES(?,?,?,?,?,?)", ("Controls Exchange", "admin", "United Kingdom", 1, 1, now_iso()))
        conn.execute("INSERT INTO users(company_id,name,email,password_hash,role,company_role,email_verified,email_verified_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (company_id, args.name.strip() or "Platform Admin", email, hash_password(password), "admin", "owner", 1, now_iso(), 1, now_iso()))
    print(f"Created platform admin: {email}")

if __name__ == "__main__":
    main()
