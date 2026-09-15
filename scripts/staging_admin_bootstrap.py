#!/usr/bin/env python3
"""Secure the staging bootstrap admin created by init_db().

Render Blueprints do not always prompt for `sync: false` environment variables.
If staging starts without explicit admin credentials, init_db() historically
creates the local-development admin. On an internet-facing staging URL that
account must never remain usable with known defaults.

Behaviour:
- if explicit SEED_ADMIN_EMAIL + SEED_ADMIN_PASSWORD are present, replace the
  untouched default bootstrap admin with those credentials;
- otherwise disable the untouched default bootstrap admin and randomise its
  password so the public staging service remains safe;
- never overwrite an already configured non-default platform admin.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core import db, hash_password, init_db, now_iso

DEFAULT_ADMIN_EMAIL = "admin@controlsexchange.local"


def main() -> int:
    requested_email = os.getenv("SEED_ADMIN_EMAIL", "").strip().lower()
    requested_password = os.getenv("SEED_ADMIN_PASSWORD", "")

    init_db()
    with db() as conn:
        admin = conn.execute(
            "SELECT id,email,active FROM users WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if not admin:
            print("staging_admin_bootstrap: no admin row found")
            return 0

        # A real/non-default admin has already been configured. Do not rotate it
        # implicitly on subsequent deploys.
        if admin["email"] != DEFAULT_ADMIN_EMAIL:
            print(f"staging_admin_bootstrap: configured admin present ({admin['email']})")
            return 0

        if requested_email and requested_password:
            if "@" not in requested_email:
                raise SystemExit("SEED_ADMIN_EMAIL must be a valid email address")
            if len(requested_password) < 14:
                raise SystemExit("SEED_ADMIN_PASSWORD must be at least 14 characters")
            conn.execute(
                """UPDATE users
                   SET email=?, password_hash=?, email_verified=1,
                       email_verified_at=?, active=1,
                       session_version=session_version+1
                   WHERE id=?""",
                (requested_email, hash_password(requested_password), now_iso(), admin["id"]),
            )
            print(f"staging_admin_bootstrap: configured staging admin {requested_email}")
            return 0

        # Never leave the documented development password usable on a public
        # staging hostname. The random value is intentionally not printed.
        conn.execute(
            """UPDATE users
               SET password_hash=?, active=0, session_version=session_version+1
               WHERE id=?""",
            (hash_password(secrets.token_urlsafe(32)), admin["id"]),
        )
        print(
            "staging_admin_bootstrap: disabled default development admin; "
            "set SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD in Render, then redeploy"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
