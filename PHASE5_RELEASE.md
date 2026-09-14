# Phase 5 release — monetisation and commercial launch layer

Phase 5 adds the commercial layer on top of the Phase 1–4 marketplace.

## Included

- 90-day founding supplier/manufacturer trial beginning at verification
- configurable 30-day standard trial for post-founding sign-ups
- buyer-free / Supplier Starter / Pro / Premium / Manufacturer plans
- hosted Stripe Checkout and Billing Portal integration
- no-card preserved founding trials, including the Stripe <48-hour `trial_end` edge case
- signed Stripe webhook processing and event deduplication
- server-side Stripe Price → plan mapping
- commercial-access gating across search/catalogue/wanted flows
- plan limits for inventory, team users, feeds, promotions and campaigns
- promoted inventory with relevance-safe ranking
- manufacturer profiles and catalogue campaigns
- supplier/manufacturer analytics
- trial expiry reminders
- admin trial/status overrides for launch operations
- sandbox Test Clock support for real Stripe trial testing
- updated production Compose/env wiring and setup/testing guide

## Launch trial decision

Use **90 days free for the founding cohort**. It starts at verification and gives an early B2B marketplace enough time to demonstrate intermittent sourcing value. Once the marketplace has meaningful buyer activity, set `FOUNDING_TRIAL_ENABLED=false`; new suppliers then use the default 30-day trial.

## Automated validation

At packaging, run:

```bash
python scripts/phase5_smoke_test.py
python scripts/phase4_smoke_test.py
python scripts/phase3_smoke_test.py
python scripts/phase2_smoke_test.py
```

The Phase 5 suite uses temporary SQLite and simulated Stripe HTTP/webhook payloads; it does not contact Stripe or charge money.

## Still requires your environment

See `SETUP_AND_TESTING.md` for the exact staging checklist. Most importantly: real Stripe sandbox Checkout/Portal/webhook delivery + Test Clock expiry, PostgreSQL/Docker, HTTPS/Caddy, transactional email, backup restore, SFTP/IMAP and any enabled OpenAI/Sentry integrations.

## Final release validation

The packaged release was validated with:

- `PHASE5_SMOKE_TEST_OK`
- `PHASE4_SMOKE_TEST_OK`
- `PHASE3_SMOKE_TEST_OK`
- `PHASE2_SMOKE_TEST_OK`
- Python bytecode compilation across the application and scripts
- Jinja compilation across all templates
- YAML parsing of development and production Compose files
- shell syntax validation of the macOS/Linux launcher
- a positive production configuration validation with Stripe enabled
- a negative production validation proving `STRIPE_TEST_CLOCK_ID` is rejected
- archive integrity and SHA-256 verification

The Phase 5 smoke suite specifically covers the Stripe `<48h` trial edge case by verifying that Checkout receives a two-day trial instead of charging before the local founding trial ends.
