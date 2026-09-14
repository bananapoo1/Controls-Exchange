# Controls Exchange — Phase 7 MVP

Controls Exchange is a trade-only search and RFQ network for obsolete, surplus and hard-to-source BMS controls.

This build contains the complete marketplace through Phase 6 plus **Phase 7 procurement/order workflow, order documents, signed webhooks and buyer/supplier ERP order APIs**.



## Phase 7: procurement workflow and enterprise order integrations

Accepting a supplier quote now creates a first-class order record. Controls Exchange remains a neutral workflow platform: the buyer and supplier contract/pay each other directly; the platform records the agreed quote, PO handoff and fulfilment lifecycle.

Included in Phase 7:

- Accepted quote → order creation, including idempotent backfill for older awarded RFQs.
- Buyer PO number, internal reference, delivery address/contact and required-by data.
- Supplier acknowledgement, processing, dispatch and delivered statuses with supplier order reference, expected dispatch, carrier and tracking.
- Private buyer↔supplier order conversation.
- Private order documents (PO, confirmation, invoice, dispatch note, certificate, other), SHA-256 checksums and access control.
- Order timeline / event history and audit-log integration.
- Buyer and supplier V1 order API: `GET /v1/orders`, `GET /v1/orders/{id}`, buyer PO submission and supplier fulfilment updates.
- Buyer private-beta API keys restricted to `orders:read`/`orders:write`; supplier API keys can combine marketplace and order scopes.
- Signed outbound lifecycle webhooks with one-time secrets, HMAC-SHA256 signatures, retries/backoff, immediate secret rotation and SSRF protection.
- Dedicated webhook worker in both Compose stacks.
- Order-document storage included in the persistent app-data volume and paired with document backups.

Controls Exchange does **not** collect order payment, take title to goods, provide escrow or automatically determine whether a legal contract exists. Cancellation in the app records workflow state; it does not by itself cancel contractual/payment obligations between the parties.

### Phase 7 automated test

```bash
python scripts/phase7_smoke_test.py
```

The test exercises quote selection → order creation → webhook setup → buyer PO submission → document upload/download → buyer/supplier ERP keys → supplier acknowledgement/processing/dispatch/delivery → webhook signatures/delivery → order backfill idempotency.

## Phase 6: data moat, market intelligence and API / ERP infrastructure

Phase 6 turns marketplace activity into a privacy-safe proprietary data layer and makes the network consumable from distributors' own systems.

### Market intelligence

`/market-intelligence` provides Pro/Premium/Manufacturer suppliers with:
- normalized demand signals from authenticated searches, RFQs and wanted requests;
- stock-gap / supplier-opportunity recommendations;
- live supply counts by normalized part number;
- quote-price ranges and medians kept separate by currency;
- selected-quote medians when enough data exists;
- a network-level demand / quote / award summary.

The dashboard is deliberately aggregation-first. Commercial users cannot see a named buyer behind a demand trend or an individual competitor quote. Production refuses privacy thresholds lower than:
- 5 demand events;
- 3 independent demand companies;
- 5 quotes;
- 3 independent quoting suppliers.

Repeated identical searches from the same company are de-duplicated inside a configurable 30-minute window so refreshing a search cannot manufacture a market trend.

### Supplier opportunity engine

The opportunity score weights intent-bearing RFQs and wanted requests more heavily than searches, then discounts parts that already have many live suppliers. A supplier's own active stock is removed from its recommendations, so the list is aimed at **what to source next**, not what it already carries.

### V1 API / ERP access

Supplier Pro and above can create revocable, scoped API keys at `/integrations/api`. The full secret is shown once; only a SHA-256 hash is stored. Keys are company-scoped, plan-gated and rate-limited.

Endpoints:

```text
GET /v1/inventory/search?q=IQ233
GET /v1/catalog/resolve?q=IQ233
GET /v1/intelligence/part?q=IQ233&days=90
```

Scopes:
- `inventory:read`
- `catalog:read`
- `intelligence:read`

Supplier Pro includes 2 keys / 1,000 requests per key per rolling hour. Supplier Premium and Manufacturer include 5 keys / 5,000 per key per rolling hour. Supplier Starter has no API entitlement. A downgrade immediately disables excess keys and authentication also checks the current plan on every request.

### Development demo intelligence

Real privacy-safe intelligence will initially be sparse. To see the Phase 6 screens locally with clearly synthetic data:

```bash
python scripts/seed_phase6_demo_intelligence.py
```

The script refuses `ENVIRONMENT=production`. Then sign in as the seeded supplier and open `/market-intelligence`. Synthetic data must never be presented as live market evidence.

### Phase 6 automated test

```bash
python scripts/phase6_smoke_test.py
```

Expected:

```text
PHASE6_SMOKE_TEST_OK
```

The test covers aggregation privacy, pricing medians, stock-gap ranking, one-time API-key display, key hashing/scopes, all three V1 endpoints, API usage metering, immediate revocation and downgrade entitlement cleanup.


## Phase 5: monetisation, founding trials and commercial analytics

Phase 5 turns the marketplace into a commercially operable launch product while keeping buyer access free.

### Launch trial strategy

- Buyers remain free.
- Verified founding suppliers/manufacturers receive **90 days free**.
- The clock starts at **company verification**, not registration.
- No card is required to use the local founding trial.
- Set `FOUNDING_TRIAL_ENABLED=false` when the founding programme closes; new verified suppliers then receive the standard `STANDARD_TRIAL_DAYS` trial (default **30 days**).
- When access expires, accounts/data remain intact but commercial inventory disappears from marketplace search until access is restored.

Ninety days is intentionally longer than a mature SaaS trial: an early B2B sourcing network needs enough time for suppliers to upload stock, receive intermittent RFQs and see actual marketplace value. Once buyer liquidity is established, 30 days is the recommended default.

### Plans included

| Plan | Monthly | Annual | Active inventory | Team | Feeds | Promotions | Analytics | API | Network intel |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Supplier Starter | £59 | £590 | 5,000 | 3 | 1 | 0 | 30 days | — | — |
| Supplier Pro | £119 | £1,190 | 50,000 | 10 | 10 | 5 | 90 days | 2 keys / 1k hr | Yes |
| Supplier Premium | £229 | £2,290 | 250,000 | 25 | 50 | 20 | 365 days | 5 keys / 5k hr | Yes |
| Manufacturer | £199 | £1,990 | 100,000 | 20 | 10 | 20 | 365 days | 5 keys / 5k hr | Yes |

These are launch hypotheses, not hard-coded commercial commitments; edit `PLANS` in `billing.py` and create matching Stripe Prices before launch if you want different pricing.

### Stripe billing

- Hosted Stripe Checkout for the first paid subscription.
- Remaining founding-trial time is preserved when a supplier subscribes early. If fewer than 48 hours remain, Checkout uses a two-day Stripe trial rather than charging the supplier before the promised local trial has ended.
- During a preserved free trial Checkout uses `payment_method_collection=if_required`, so no card is forced while £0 is due.
- `STRIPE_TRIAL_END_BEHAVIOR` defaults to `pause`; `cancel` and `create_invoice` are also supported.
- Signed webhook verification and idempotent event storage are included.
- Stripe Price IDs are mapped back to app plans server-side so plan state does not depend only on mutable metadata.
- Existing non-terminal Stripe subscriptions are sent to the Stripe Billing Portal rather than accidentally creating a second subscription.
- `past_due` keeps temporary grace access; `paused`, `unpaid`, `canceled` and expired local trials do not.

### Plan limits and downgrade behaviour

Limits are enforced when adding inventory, users, feeds, promotions and campaigns. On a Stripe plan downgrade, entitlement-only resources are reconciled automatically: excess promoted lines are disabled, excess manufacturer campaigns are paused and excess automated feeds are disabled. Existing inventory and team members are never deleted automatically; while above a lower plan's limit the company must reduce usage before adding more.

### Promoted inventory

Eligible plans can promote a limited number of stock lines. Promotion only breaks ties between similarly relevant search matches; it does not buy relevance over a stronger part-number match. Search impressions are recorded for analytics.

### Manufacturer accounts and campaigns

Manufacturer accounts have a separate plan and tooling for an official brand profile, catalogue sponsorship messages, CTA links and campaign analytics. Cross-audience billing plans are rejected so a manufacturer cannot accidentally receive a supplier entitlement set.

### Supplier analytics

Paid/trial suppliers can view search appearances, profile views, RFQs, quote rate, selected quotes, top matching searches and most-requested parts. Manufacturer accounts additionally receive campaign impressions/clicks and promoted-stock impressions.

### Phase 5 automated test

Run:

```bash
python scripts/phase5_smoke_test.py
```

Expected:

```text
PHASE5_SMOKE_TEST_OK
```

The test uses a temporary SQLite database and simulated Stripe calls/webhooks; it never contacts Stripe or charges anything. Live Stripe/PostgreSQL validation is documented in `SETUP_AND_TESTING.md`.

## Phase 4: technical catalogue and sourcing intelligence

### Canonical parts catalogue
Phase 4 adds a platform-owned technical layer above supplier inventory:
- canonical manufacturer + part-number records
- product family, lifecycle, description, source and review status
- aliases / alternative historic codes
- automatic inventory-to-canonical-part linking
- catalogue links directly from inventory search results

The development seed creates identity-only records for the six demo parts. It deliberately does **not** seed technical compatibility or supersession claims.

### Supersession and compatibility graph
Admins can record directed technical relationships between parts:
- `superseded_by`
- `supersedes`
- `compatible_with`
- `alternative_to`
- `requires_adapter`

Each relationship stores confidence, source, notes and a reviewed/unreviewed flag. The sourcing assistant never invents replacement relationships; it only surfaces relationships stored in this graph.

### Sourcing assistant
`/source` accepts natural-language sourcing requests.

Without an AI key it extracts part-like model codes locally. With `OPENAI_API_KEY` configured, the model can extract manufacturer/model/keywords from messy prose, but it is intentionally restricted to retrieval assistance. Replacement and compatibility recommendations remain database-grounded.

### Photo identification
`/identify` accepts JPEG, PNG and WebP images.
- With `OPENAI_API_KEY`, the image is sent transiently to the configured OpenAI Responses API vision model. The raw image is not stored by Controls Exchange.
- Without a key, users can type visible label/model text and get local catalogue matching.
- Identification metadata/history is stored for audit and product improvement; the uploaded image itself is not retained by the app.

Configuration:

```env
OPENAI_API_KEY=
PHOTO_ID_MODEL=gpt-5.6-luna
PHOTO_ID_MAX_BYTES=8388608
AI_SOURCING_ENABLED=true
AI_SOURCING_MODEL=gpt-5.6-luna
```

### Catalogue administration
Platform admins can use `/admin/catalog` to:
- add canonical parts
- edit lifecycle / descriptions / sources / review state
- add aliases
- add replacement / compatibility relationships
- mark relationships reviewed

For live use, technical replacement claims should be backed by a manufacturer bulletin, datasheet, verified integrator documentation, or another source you are comfortable relying on.

### Phase 4 automated test

```bash
python scripts/phase4_smoke_test.py
```

It covers catalogue creation, aliases, reviewed supersession relationships, canonical inventory linkage, source-assistant retrieval, local photo-ID fallback, access control, search enrichment, and a simulated OpenAI image-response contract.

See **`SETUP_AND_TESTING.md`** for the complete local/production setup and the live-validation checklist for services that cannot be fully exercised in this build environment.

---
## Phase 3: supplier ingestion automation

### 1. Mapped manual imports
Suppliers can upload CSV/XLSX stock and preview it before committing:
- automatic header detection for common ERP/export columns
- manual dropdown mapping for manufacturer, part number, description, condition, quantity and location
- first-10-row raw preview
- required part-number mapping
- validation before import
- staged uploads expire automatically after two hours

Use **Inventory → Preview & map columns**.

### 2. Duplicate detection
Imports calculate a stock identity from:

`brand + part number + condition + location`

Three policies are available:
- **Merge** — duplicate source rows are combined and quantities summed
- **Skip** — first duplicate wins
- **Keep separate** — duplicate rows retain separate stable source keys

Imports also detect matches against the supplier's existing catalogue so an upsert refreshes existing stock rather than creating unnecessary duplicates. Legacy Phase 1/2 stock is matched once and stamped with the new source key automatically.

### 3. Import modes
- **Upsert** — update matching stock and add new stock
- **Add** — append new stock only
- **Replace** — manual imports archive the current company catalogue, then load the new file
- **Sync** — automated feeds update/add feed-owned stock and retire stock that disappears from that same feed

Automated sync is deliberately feed-scoped. A feed can never retire inventory managed manually or by another feed.

Safety guardrails:
- an empty sync payload is rejected
- if any source rows fail validation, the run may update valid rows but **missing stock is not retired**
- this prevents a malformed supplier export from accidentally emptying a live catalogue

### 4. Import history and row-level quality reports
Every manual and automated run creates an immutable import job with:
- source/feed
- row count
- inserted rows
- updated rows
- retired rows
- duplicate count
- skipped count
- validation errors/warnings
- source column mapping
- timestamps/status

Row-level errors include the source row number, error code and explanation. Suppliers can inspect the history at `/inventory-imports`.

### 5. Scheduled HTTPS file feeds
A supplier can configure a hosted CSV/XLSX URL and Controls Exchange will poll it automatically.

Supported authentication:
- Bearer token
- custom HTTP authentication header

Production safeguards:
- HTTPS required
- URL credentials are rejected; secrets must use encrypted fields
- redirects are disabled to reduce SSRF risk
- private/loopback/link-local/reserved network targets are blocked by default
- response size is capped by `MAX_UPLOAD_BYTES`

Set `ALLOW_PRIVATE_FEED_HOSTS=true` only if the deployment intentionally has a protected private-network feed source.

### 6. JSON API pull feeds
Controls Exchange can poll an HTTPS JSON endpoint on the same schedule.

Accepted response shapes:

```json
[
  {"manufacturer":"Trend","part_number":"IQ233","quantity":2}
]
```

or:

```json
{"items":[...]}
```

For nested payloads, configure a dotted `data_path`, for example `data.inventory`.

Column mapping fields correspond to JSON object keys.

### 7. SFTP feeds
Suppliers can sync a CSV/XLSX export over SFTP using:
- password authentication, or
- encrypted private-key authentication

Production requires the server's SHA256 host-key fingerprint unless the deployment explicitly relaxes that requirement. The worker uses Paramiko, installed from `requirements.txt`.

### 8. Push API feeds
Each push feed gets a unique high-entropy revocable endpoint. The token is hashed for lookup and encrypted for display; it is never stored as plaintext in a normal config field.

The endpoint accepts either JSON inventory objects or a multipart CSV/XLSX file. Example JSON:

```bash
curl -X POST 'https://yourdomain.com/api/inventory-feed/<feed-token>' \
  -H 'Content-Type: application/json' \
  --data '[{"brand":"Trend","part":"IQ233","qty":2}]'
```

The supplier configures the key mapping once in the feed settings.

Rotating the token immediately revokes the old endpoint.

### 9. Email attachment imports
There are two supported operational models.

**IMAP / plus-address mailbox**

Configure a mailbox/catch-all and:

```env
INVENTORY_IMPORT_EMAIL_DOMAIN=stock.yourdomain.com
INVENTORY_IMPORT_IMAP_HOST=imap.provider.com
INVENTORY_IMPORT_IMAP_PORT=993
INVENTORY_IMPORT_IMAP_USER=imports@stock.yourdomain.com
INVENTORY_IMPORT_IMAP_PASSWORD=<secret>
INVENTORY_IMPORT_IMAP_SSL=true
INVENTORY_IMPORT_IMAP_FOLDER=INBOX
```

Each feed receives an address such as:

`imports+<unique-token>@stock.yourdomain.com`

The worker scans unread messages, identifies the feed from the recipient token and imports the first supported CSV/XLSX attachment.

**Inbound-email provider webhook**

Each email feed also exposes `/api/inventory-email/<token>`. A mail provider can POST a multipart attachment or JSON containing:

```json
{
  "attachments": [
    {"filename":"stock.csv", "content_base64":"..."}
  ]
}
```

### 10. Background feed worker
`scripts/feed_worker.py`:
- claims due URL/API/SFTP feeds
- executes syncs
- triggers saved-search/wanted-part alerts
- records feed status
- emails supplier owners/admins when scheduled imports fail
- optionally polls the configured IMAP mailbox

PostgreSQL uses `FOR UPDATE SKIP LOCKED` so multiple workers cannot claim the same due feed simultaneously.

The Docker stacks include this worker automatically.

### 11. Credential protection
Remote API/SFTP credentials are encrypted at rest with a separate key:

```env
INTEGRATION_ENCRYPTION_KEY=<random secret at least 32 chars>
```

Production startup refuses to run without a proper integration encryption key.

Inbound push/email tokens are redacted from application request logs.

---

## Phase 2 features retained
- fuzzy/normalised part-number search
- inventory freshness and automatic search expiry
- saved-search alerts
- automatic wanted-part matching
- private buyer↔supplier RFQ threads
- quote comparison and buyer selection
- objective supplier trust metrics
- company-level sourcing visibility

## Phase 1 features retained
- PostgreSQL production database / SQLite local development
- hardened Docker deployment and Caddy HTTPS
- email verification and password reset
- Resend/SMTP transactional email
- company/team roles and permissions
- audit logging
- inventory edit/archive/restore/delete
- request IDs, health checks and optional Sentry
- automatic database backups and restore tooling

---

## Upgrade from earlier phases

Phase 3 and Phase 4 migrations are additive. Before upgrading a real deployment, take a backup, then start the Phase 4 app normally.

Phase 3 adds:
- `inventory_import_profiles`
- `inventory_feeds`
- `inventory_import_jobs`
- `inventory_import_errors`
- `inventory_import_staging`
- `source_feed_id`, `source_key` and `last_import_job_id` on inventory

Existing companies, users, inventory, RFQs, saved searches and Phase 2 messages remain intact.

Existing inventory does not need a destructive migration. Its source identity is populated lazily the first time a Phase 3 upsert matches it.

Phase 4 adds:
- `catalog_parts`
- `catalog_aliases`
- `catalog_relations`
- `photo_identifications`
- `sourcing_queries`
- `canonical_part_id`, `catalog_match_method` and `catalog_match_confidence` on inventory

Existing inventory is linked automatically where a confident exact/alias match exists. Unmatched inventory remains fully usable.

## Fast local start — SQLite

### Windows PowerShell

```powershell
.\run_windows.ps1
```

### macOS / Linux

```bash
./run_mac_linux.sh
```

Then open `http://127.0.0.1:8000`.

Local demo accounts:
- Admin: `admin@controlsexchange.local` / `ChangeMe123!`
- Supplier: `supplier@example.com` / `Supplier123!`
- Buyer: `buyer@example.com` / `Buyer123!`

Development-only; disable demo data outside local testing.

## Local Docker — PostgreSQL + workers

```bash
cp .env.example .env
docker compose up --build
```

The local stack contains:
- PostgreSQL
- web application
- scheduled feed/email worker
- backup worker

Open `http://127.0.0.1:8000`.

## Production deployment

Required minimum values:

```env
ENVIRONMENT=production
DOMAIN=yourdomain.com
SECRET_KEY=<long random secret>
INTEGRATION_ENCRYPTION_KEY=<different long random secret>
POSTGRES_PASSWORD=<strong random database password>
PUBLIC_BASE_URL=https://yourdomain.com
ALLOWED_HOSTS=yourdomain.com
COOKIE_SECURE=true
EMAIL_PROVIDER=resend
EMAIL_FROM=Controls Exchange <hello@yourdomain.com>
RESEND_API_KEY=<your key>
SEED_DEMO_DATA=false
SEED_ADMIN=false
```

Optional inbound-email values are listed above.

Start:

```bash
docker compose -f docker-compose.production.yml up -d --build
```

Create the first admin explicitly:

```bash
docker compose -f docker-compose.production.yml run --rm app \
  python scripts/create_admin.py --email you@yourdomain.com --name "Platform Admin"
```

## Monitoring and backups

Optional Sentry:

```env
SENTRY_DSN=<dsn>
SENTRY_TRACES_SAMPLE_RATE=0.1
```

Backup defaults:

```env
BACKUP_INTERVAL_SECONDS=21600
BACKUP_RETENTION_DAYS=14
```

Restore database only:

```bash
python scripts/restore_backup.py /path/to/backup --yes
```

Restore a matching Phase 7 order-document archive as well:

```bash
python scripts/restore_backup.py /path/to/backup --documents-backup /path/to/controls_exchange_documents_<timestamp>.tar.gz --yes
```

For serious production use, replicate the backup volume to off-host object storage.

## Automated tests

Phase 7 procurement/integration suite:

```bash
python scripts/phase7_smoke_test.py
```

Phase 6 intelligence/API suite:

```bash
python scripts/phase6_smoke_test.py
```

Phase 5 monetisation/billing suite:

```bash
python scripts/phase5_smoke_test.py
```

Phase 4 technical-intelligence suite:

```bash
python scripts/phase4_smoke_test.py
```

Phase 3 ingestion/regression suite:

```bash
python scripts/phase3_smoke_test.py
```

Phase 2 marketplace regression suite:

```bash
python scripts/phase2_smoke_test.py
```

The Phase 7 suite covers end-to-end orders, PO handoff, documents, order APIs, webhook signing/delivery, scope isolation and migration idempotency. The Phase 5 suite covers founding trials, commercial access expiry, plan entitlements, promoted stock, supplier analytics, manufacturer campaigns, simulated Stripe Checkout/webhooks, Price-to-plan mapping, downgrade reconciliation and trial reminders. The Phase 3 suite covers mapped uploads, validation reports, duplicate policies, legacy-stock upserts, push API, token rotation, email/webhook ingestion, feed-scoped sync, retirement guardrails, scheduled worker execution, SFTP code path and cross-company isolation.

## Important deployment notes
- `terms.html` and `privacy.html` are still product drafts and should receive proper legal review before public launch.
- Do not expose the PostgreSQL service publicly.
- Treat supplier inventory files, sourcing requests and integration credentials as confidential business data.
- Do not set `ALLOW_PRIVATE_FEED_HOSTS=true` unless the worker is intentionally allowed to access a protected private network.
