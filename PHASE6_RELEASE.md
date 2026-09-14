# Phase 6 release — data moat & infrastructure

Phase 6 turns Controls Exchange activity into a privacy-safe proprietary intelligence layer and exposes selected network capabilities through an authenticated supplier API.

## Delivered

- Normalized demand-event capture from authenticated searches, RFQs and wanted requests.
- Repeated-search de-duplication to reduce trivial demand-metric gaming.
- Privacy-safe demand intelligence with minimum event/company cohorts and contributor-share controls.
- Quote-price low / median / high benchmarks, always separated by currency.
- Selected-quote median where the accepted-quote cohort is large enough.
- Supplier stock-gap / opportunity recommendations based on demand versus live supply.
- Network summary metrics for searches, RFQs, wanted requests, quotes and awards.
- Pro+ market-intelligence dashboard.
- Supplier Pro+ V1 API / ERP access.
- One-time-display API secrets; only SHA-256 hashes are stored.
- Company-scoped API keys with explicit scopes, revocation and rolling-hour rate limits.
- `GET /v1/inventory/search`
- `GET /v1/catalog/resolve`
- `GET /v1/intelligence/part`
- API usage reporting and audit events.
- Plan entitlements: Starter has no API/network intelligence; Pro has 2 keys / 1,000 requests per key per rolling hour; Premium/Manufacturer have 5 keys / 5,000 per key per rolling hour.
- Downgrade reconciliation disables API access immediately when entitlement is lost.
- Development-only synthetic intelligence seeder for visual QA.
- Updated billing feature display, navigation, `README.md`, `SECURITY.md`, and the living `SETUP_AND_TESTING.md` handoff guide.

## Commercial privacy safeguards

Supplier-facing intelligence never exposes a named buyer behind a demand trend or an individual competitor quote. Production configuration enforces at least:

- 5 demand events;
- 3 independent demand companies;
- 5 quote samples;
- 3 independent quoting suppliers;
- maximum single-contributor share of 50%.

The application refuses production startup if these floors are weakened through environment configuration.

## Automated validation

The release was regression-tested with:

```bash
python scripts/phase6_smoke_test.py
python scripts/phase5_smoke_test.py
python scripts/phase4_smoke_test.py
python scripts/phase3_smoke_test.py
python scripts/phase2_smoke_test.py
```

Expected and observed markers:

```text
PHASE6_SMOKE_TEST_OK
PHASE5_SMOKE_TEST_OK
PHASE4_SMOKE_TEST_OK
PHASE3_SMOKE_TEST_OK
PHASE2_SMOKE_TEST_OK
```

Additional release checks completed in the build environment:

- Python compile / AST validation.
- All Jinja templates compiled successfully.
- Both Docker Compose YAML files parsed successfully.
- Production-safe Phase 6 threshold configuration accepted.
- Deliberately unsafe Phase 6 privacy threshold rejected at startup.
- Obvious production-secret / private-key scan.
- Runtime email/test artefacts removed before packaging.

## Methodology note

Pricing intelligence is based on supplier quotes submitted through Controls Exchange. It is **not** yet a completed-transaction price index. If order confirmation or transaction settlement is added later, completed transaction benchmarks should be a separate metric rather than silently changing the meaning of historical quote benchmarks.

## Live validation still required

The exact checklist is maintained in `SETUP_AND_TESTING.md`. Phase 6 items that require your infrastructure include:

- PostgreSQL query performance at realistic inventory/demand scale;
- concurrent API/rate-limit behaviour against PostgreSQL with multiple app workers;
- API authentication through the real HTTPS/Caddy deployment;
- integration from at least one real ERP/CRM/quoting system;
- external load/abuse testing;
- commercial privacy/legal review of the analytics methodology and terms;
- data-quality review before marketing quote metrics as market benchmarks.

These are in addition to the Stripe, DNS/TLS, email, SFTP, IMAP, OpenAI, Sentry and backup/restore checks already documented from earlier phases.
