# Controls Exchange — Setup, Testing & Live Validation Guide

This is the living handoff document for Controls Exchange. Keep it with every future build.

It covers:
1. how to run the application locally;
2. how to deploy the production stack;
3. the automated tests that should pass before every release;
4. which integrations were fully tested in the build environment;
5. which items still require validation with your own infrastructure, domains or API credentials.

---

## Phase 7 quick validation — orders, procurement API and webhooks

### Automated test

```bash
python scripts/phase7_smoke_test.py
```

Expected: `PHASE7_SMOKE_TEST_OK`. The suite uses a temporary SQLite database and a simulated outbound webhook target; it does not call a real ERP or public webhook receiver.

It validates:

- accepted quote → one idempotent order record;
- buyer PO submission and delivery details;
- private order messages and file access;
- SHA-256 document checksums;
- supplier acknowledgement / processing / dispatch / delivery transitions;
- buyer order-only API-key scope stripping;
- supplier order API updates;
- HMAC-signed webhook delivery and retry queue;
- one-time webhook-secret display / encrypted-at-rest storage;
- migration/backfill of pre-Phase-7 accepted RFQs.

### View the workflow locally

1. Start the app and sign in as `buyer@example.com` / `Buyer123!`.
2. Create an RFQ from a live demo stock result.
3. Sign in as `supplier@example.com` / `Supplier123!` in another browser/private window and submit a quote.
4. Return as the buyer and select the quote.
5. You should be redirected to `/orders/<id>`.
6. Add a PO number and delivery details.
7. As the supplier, acknowledge → process → dispatch → deliver the order.
8. Upload a test PDF as either company and verify only the buyer/supplier on that order can download it.

### Buyer / supplier order API

Owners/admins can create an API key at `/integrations/api`. During private beta a buyer company gets one order-only key. If a buyer attempts to request inventory/catalog/intelligence scopes, the server removes them and retains only `orders:read` / `orders:write`.

```bash
curl -H "Authorization: Bearer cxk_REPLACE_ME" \
  "http://127.0.0.1:8000/v1/orders"

curl -H "Authorization: Bearer cxk_REPLACE_ME" \
  "http://127.0.0.1:8000/v1/orders/1"
```

Buyer PO submission:

```bash
curl -X POST \
  -H "Authorization: Bearer cxk_REPLACE_ME" \
  -H "Content-Type: application/json" \
  -d '{"buyer_po_number":"PO-1001","delivery_address":"1 Example Street, London"}' \
  "http://127.0.0.1:8000/v1/orders/1/buyer-details"
```

Supplier fulfilment update:

```bash
curl -X POST \
  -H "Authorization: Bearer cxk_REPLACE_ME" \
  -H "Content-Type: application/json" \
  -d '{"status":"dispatched","carrier":"DHL","tracking_number":"TRACK123"}' \
  "http://127.0.0.1:8000/v1/orders/1/status"
```

### Outbound webhook test

1. Sign in as a company owner/admin and open `/integrations/webhooks`.
2. Add an HTTPS endpoint you control.
3. Copy the `cxwhsec_...` signing secret; it is shown once.
4. Trigger an order event.
5. The receiver should get `X-CX-Event`, `X-CX-Event-ID`, `X-CX-Timestamp` and `X-CX-Signature`.
6. Verify the signature over `timestamp + "." + raw_body` with HMAC-SHA256.
7. Return a non-2xx response and confirm the delivery stays pending and retries later.
8. Rotate the secret and verify the old secret no longer validates new deliveries.

Production webhook URLs must be HTTPS and cannot resolve to private/loopback/link-local addresses unless `ALLOW_PRIVATE_WEBHOOK_HOSTS=true` is deliberately enabled. Redirects are not followed.

### Order document storage and backup

Order documents are stored under `ORDER_DOCUMENT_DIR` (default `/app/data/order_documents` in Compose). The Phase 7 Compose stack persists `/app/data` and the backup worker now creates a matching `controls_exchange_documents_<timestamp>.tar.gz` next to each database backup.

Restore both together after stopping the app:

```bash
python scripts/restore_backup.py /path/to/database-backup \
  --documents-backup /path/to/controls_exchange_documents_<timestamp>.tar.gz \
  --yes
```

For a multi-host/horizontally scaled deployment, move order documents to managed object storage before adding more web hosts; a local Docker volume is intentionally the simple first-production implementation.

### Phase 7 checks that require your real environment

These were not possible to prove end-to-end here and should be checked in staging:

- real outbound HTTPS webhook delivery across the public internet;
- DNS rebinding / egress-network controls at your hosting provider in addition to application SSRF checks;
- webhook retries across worker/container restarts using PostgreSQL;
- two concurrent webhook workers using PostgreSQL `SKIP LOCKED`;
- ERP-specific API mapping against the first real customer's system;
- order-document persistence across a real Docker redeploy;
- off-host database + document backup replication and destructive restore drill;
- malware scanning / content-disarm policy for buyer/supplier uploads (not implemented in this MVP);
- final legal review of when quote acceptance/PO submission forms a contract and what cancellation language should say.

---

## Phase 6 quick validation — market intelligence and API / ERP

### Automated test

Run this before any release:

```bash
python scripts/phase6_smoke_test.py
```

Expected: `PHASE6_SMOKE_TEST_OK`. It uses a temporary SQLite database and no external service.

### How to actually see populated intelligence locally

A clean demo database will often be below the privacy thresholds, which is expected. In **development/staging only**, run:

```bash
python scripts/seed_phase6_demo_intelligence.py
```

Then sign in as:

```text
supplier@example.com
Supplier123!
```

and open:

```text
http://127.0.0.1:8000/market-intelligence
```

You should see synthetic examples including demand gaps and GBP quote medians. The seeder refuses to run when `ENVIRONMENT=production`. Do not screenshot or present seeded figures as real market evidence.

### Test the V1 API locally

1. Sign in as the supplier.
2. Open `/integrations/api`.
3. Create a key with `inventory:read`, `catalog:read`, and optionally `intelligence:read`.
4. Copy the `cxk_...` secret immediately; it is never shown again.
5. Test catalogue resolution:

```bash
curl -H "Authorization: Bearer cxk_REPLACE_ME" \
  "http://127.0.0.1:8000/v1/catalog/resolve?q=IQ233"
```

6. Test live inventory:

```bash
curl -H "Authorization: Bearer cxk_REPLACE_ME" \
  "http://127.0.0.1:8000/v1/inventory/search?q=IQ233"
```

7. If you seeded Phase 6 demo intelligence, test an aggregate benchmark:

```bash
curl -H "Authorization: Bearer cxk_REPLACE_ME" \
  "http://127.0.0.1:8000/v1/intelligence/part?q=Satchwell%20BAS2800&days=90"
```

8. Revoke the key in the UI and repeat the request. It must return HTTP 401.

### Phase 6 production configuration

Keep the production defaults unless you have a documented privacy review:

```env
INTELLIGENCE_MIN_DEMAND_EVENTS=5
INTELLIGENCE_MIN_BUYER_COMPANIES=3
INTELLIGENCE_MIN_QUOTE_SAMPLES=5
INTELLIGENCE_MIN_QUOTE_SUPPLIERS=3
INTELLIGENCE_MAX_CONTRIBUTOR_SHARE=0.5
SEARCH_DEMAND_DEDUP_MINUTES=30
API_DEFAULT_RATE_PER_HOUR=1000
API_MAX_RATE_PER_HOUR=10000
```

The application refuses to start in production if the four minimum cohorts are below 5 / 3 / 5 / 3 or if `INTELLIGENCE_MAX_CONTRIBUTOR_SHARE` is above 0.5.

### Phase 6 live checks I could not fully perform in this environment

The application logic and local HTTP contract are covered by automated tests, but validate the following on your staging/production infrastructure:

- **PostgreSQL query performance with realistic scale:** load at least hundreds of thousands of inventory lines and a representative volume of `market_demand_events` / quotes, then time `/market-intelligence` and the three `/v1/...` endpoints. The current implementation is suitable for launch; at large scale you may later materialize/warehouse aggregates.
- **Multi-worker API rate limiting:** rate limits are database-backed and logically safe, but run concurrent requests against the real PostgreSQL deployment to confirm expected throughput/contention.
- **Public HTTPS API path through Caddy:** test Bearer and `X-API-Key` authentication over your real domain, verify secrets never appear in proxy/application logs, and confirm 401/403/429 responses.
- **Real ERP/CRM consumer:** call the API from the system you actually plan to integrate (SAP, Dynamics, bespoke quoting tool, etc.) and verify field mapping/timeouts/retry behaviour. No third-party ERP credentials are included here.
- **Load/abuse testing:** verify the configured request limits and reverse-proxy limits are sufficient for legitimate batch use while protecting the application from scraping.
- **Commercial privacy review:** before selling quote intelligence, have your privacy/legal adviser review the aggregation thresholds, retention policy and customer terms. The software prevents sparse cohorts, but commercial policy still needs human approval.
- **Data-quality review:** quote intelligence measures submitted supplier quotes, not necessarily final invoiced transaction prices. Confirm your customer-facing wording and analytics methodology are acceptable before marketing it as a price benchmark.

These checks are additional to the external Stripe/PostgreSQL/DNS/email/SFTP/IMAP/OpenAI/Sentry/backup tests already listed later in this document.

---

## 1. Fastest way to view the product locally

### Requirements

- Python 3.11+ recommended
- Windows PowerShell, or macOS/Linux shell
- Internet access the first time so Python packages can install

### Windows

Unzip the project, open PowerShell in the project folder, then run:

```powershell
.\run_windows.ps1
```

Open:

```text
http://127.0.0.1:8000
```

### macOS / Linux

```bash
chmod +x run_mac_linux.sh
./run_mac_linux.sh
```

Open:

```text
http://127.0.0.1:8000
```

### Development demo accounts

These exist only when `SEED_DEMO_DATA=true` / `SEED_ADMIN=true`:

| Role | Email | Password |
|---|---|---|
| Admin | `admin@controlsexchange.local` | `ChangeMe123!` |
| Supplier | `supplier@example.com` | `Supplier123!` |
| Buyer | `buyer@example.com` | `Buyer123!` |

Never use those credentials in production.

---

## 2. What to look at first

After signing in as the **buyer**:

1. Open **Search** and search `IQ233`.
2. Open **Parts catalogue** and select the Trend IQ233 record.
3. Open **Source assistant** and enter something like `Need an IQ233 controller`.
4. Open **Photo ID**. Without an API key, type visible label text such as `Trend IQ233/UNB/230VAC` alongside any JPEG/PNG/WebP upload to exercise local catalogue matching.
5. Open **Wanted** and **Alerts** to review the Phase 2 workflows.

After signing in as the **supplier**:

1. Open **Billing** and confirm the 90-day founding trial / current plan.
2. Open **Inventory** and preview/map a CSV or XLSX import.
3. Open **Feeds** to view automated feed configuration.
4. Open **Promote** and feature a stock line (Pro/Premium trial entitlement).
5. Open **Analytics** and review search/RFQ demand metrics.
6. Open **RFQs** to review inbound and outbound sourcing.
7. Open **Team** for company roles.

After signing in as the **admin**:

1. Open the admin dashboard.
2. Open **Billing admin** and review/grant a trial override.
3. Open **Catalogue admin**.
4. Add a canonical part, alias and technical relationship.
5. Mark a relationship Reviewed only after adding an appropriate source.

---

## 3. Local configuration

Copy the example environment file if you want explicit settings:

```bash
cp .env.example .env
```

For plain Python local development, the application defaults to SQLite. The run scripts work without Docker.

Useful local values:

```env
ENVIRONMENT=development
DATABASE_PATH=data/controls_exchange.db
EMAIL_PROVIDER=log
SEED_ADMIN=true
SEED_DEMO_DATA=true
COOKIE_SECURE=false
```

With `EMAIL_PROVIDER=log`, outbound emails are written to:

```text
data/email_outbox.log
```

### Phase 5 launch trial and billing configuration

The recommended launch configuration is:

```env
FOUNDING_TRIAL_ENABLED=true
FOUNDING_TRIAL_DAYS=90
STANDARD_TRIAL_DAYS=30
STRIPE_TRIAL_END_BEHAVIOR=pause
```

Why 90 days now: early marketplace value is intermittent. A supplier needs enough time to upload stock, receive real searches/RFQs and judge whether the network produces business. Once the marketplace has meaningful buyer liquidity, switch `FOUNDING_TRIAL_ENABLED=false`; future verified suppliers then receive the standard 30-day trial.

The trial starts when an admin **verifies the supplier**, not when the company first registers. Buyers remain free. Expiry hides commercial inventory from marketplace discovery but does not delete the company, users, stock, RFQs or import history.

Stripe is optional while you are still running free founding trials/manual pilots. To enable paid checkout in test mode, configure:

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PORTAL_RETURN_URL=http://127.0.0.1:8000/billing
STRIPE_TRIAL_END_BEHAVIOR=pause
STRIPE_PRICE_SUPPLIER_STARTER_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_STARTER_ANNUAL=price_...
STRIPE_PRICE_SUPPLIER_PRO_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_PRO_ANNUAL=price_...
STRIPE_PRICE_SUPPLIER_PREMIUM_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_PREMIUM_ANNUAL=price_...
STRIPE_PRICE_MANUFACTURER_MONTHLY=price_...
STRIPE_PRICE_MANUFACTURER_ANNUAL=price_...
```

For accelerated **sandbox-only** trial testing you may also set:

```env
STRIPE_TEST_CLOCK_ID=clock_...
```

Never set `STRIPE_TEST_CLOCK_ID` in production; production startup explicitly rejects it.

For launch, configure the Stripe Billing Portal for payment-method updates, invoices and cancellation. **Do not enable self-service cross-plan switching in the portal yet.** The application prevents duplicate subscriptions and maps configured Stripe Price IDs back to plans, but supplier/manufacturer account types have different entitlement sets and usage-aware downgrades are better kept under admin control during the founding period.

---

## 4. Phase 4 AI / photo configuration

Phase 4 has two deliberately separate modes.

### No API key

The whole marketplace, technical catalogue, relationship graph, sourcing workflow and visible-text catalogue matching work without an AI provider.

Photo images themselves are **not analysed** in this mode. Users can type the text visible on a label and the local catalogue matcher will identify likely parts.

### OpenAI image identification + sourcing-intent extraction

Set:

```env
OPENAI_API_KEY=<your project API key>
PHOTO_ID_MODEL=gpt-5.6-luna
PHOTO_ID_MAX_BYTES=8388608
AI_SOURCING_ENABLED=true
AI_SOURCING_MODEL=gpt-5.6-luna
```

The application uses the OpenAI Responses API.

Important implementation behaviour:

- Photo bytes are sent to the model for the identification request.
- Controls Exchange does **not** persist the uploaded raw photo.
- The app stores identification metadata/result history.
- AI is allowed to extract manufacturer/model/keywords.
- AI is **not** allowed to create technical compatibility or replacement claims.
- Replacement/alternative recommendations come only from `catalog_relations` in the platform database.

If you do not want AI sourcing-intent extraction but do want photo ID:

```env
AI_SOURCING_ENABLED=false
```

---

## 5. Run all automated regression tests

From the project root with the virtual environment active:

```bash
python scripts/phase6_smoke_test.py
python scripts/phase5_smoke_test.py
python scripts/phase4_smoke_test.py
python scripts/phase3_smoke_test.py
python scripts/phase2_smoke_test.py
```

Expected terminal markers:

```text
PHASE5_SMOKE_TEST_OK
PHASE4_SMOKE_TEST_OK
PHASE3_SMOKE_TEST_OK
PHASE2_SMOKE_TEST_OK
```

### What Phase 5 tests

- 90-day founding trial starts at supplier verification
- trial expiry hides marketplace visibility without deleting company data
- plan entitlements / promotion limits
- supplier analytics
- manufacturer profile and campaigns
- simulated Stripe Checkout request construction
- no-card trial Checkout (`payment_method_collection=if_required`)
- signed webhook verification and duplicate-event handling
- Stripe Price ID → plan mapping
- downgrade reconciliation for promotions/feeds/campaigns
- trial reminder deduplication
- optional sandbox Test Clock propagation

### What Phase 4 tests

- canonical catalogue creation
- alias matching
- reviewed supersession relationship
- inventory → canonical-part linking
- natural-language sourcing retrieval
- replacement graph lookup
- local photo-ID visible-text fallback
- buyer/admin catalogue permissions
- catalogue enrichment in inventory search
- simulated OpenAI Responses API image request/response parsing

The simulated OpenAI test validates request construction and response parsing without spending API credits. It does **not** prove that your own API key/model access is configured correctly; see the live-validation checklist below.

### What Phase 3 tests

- mapped imports
- validation errors/reports
- duplicate handling
- legacy stock upserts
- push API feeds
- token rotation
- email webhook imports
- feed-scoped sync
- malformed/empty-feed retirement protection
- scheduled worker logic
- SFTP code path using a simulated Paramiko server/client
- cross-company isolation

### What Phase 2 tests

- improved part search
- stock freshness / expiry / reconfirmation
- saved-search alerts
- wanted-part matching
- private RFQ threads
- quote comparison / award flow
- supplier trust metrics
- multi-supplier commercial privacy

---

## 6. Docker development stack

If Docker is installed:

```bash
cp .env.example .env
docker compose up --build
```

Open:

```text
http://127.0.0.1:8000
```

The stack includes:

- PostgreSQL
- Controls Exchange web app
- inventory feed / email worker
- backup worker

Stop with:

```bash
docker compose down
```

To remove the local Docker database too:

```bash
docker compose down -v
```

Do not use `-v` against a production environment unless you intentionally want to delete persistent volumes.

---

## 7. Production deployment

### Minimum production environment

Create a `.env` alongside `docker-compose.production.yml` containing at least:

```env
DOMAIN=controlsexchange.example
POSTGRES_PASSWORD=<strong random password>
SECRET_KEY=<random 32+ character secret>
INTEGRATION_ENCRYPTION_KEY=<different random 32+ character secret>
EMAIL_PROVIDER=resend
EMAIL_FROM=Controls Exchange <hello@controlsexchange.example>
RESEND_API_KEY=<key>
SEED_DEMO_DATA=false
SEED_ADMIN=false
FOUNDING_TRIAL_ENABLED=true
FOUNDING_TRIAL_DAYS=90
STANDARD_TRIAL_DAYS=30
```

Optional Phase 4:

```env
OPENAI_API_KEY=<key>
PHOTO_ID_MODEL=gpt-5.6-luna
AI_SOURCING_ENABLED=true
AI_SOURCING_MODEL=gpt-5.6-luna
```

Required when enabling paid Stripe billing:

```env
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_TRIAL_END_BEHAVIOR=pause
STRIPE_PRICE_SUPPLIER_STARTER_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_STARTER_ANNUAL=price_...
STRIPE_PRICE_SUPPLIER_PRO_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_PRO_ANNUAL=price_...
STRIPE_PRICE_SUPPLIER_PREMIUM_MONTHLY=price_...
STRIPE_PRICE_SUPPLIER_PREMIUM_ANNUAL=price_...
STRIPE_PRICE_MANUFACTURER_MONTHLY=price_...
STRIPE_PRICE_MANUFACTURER_ANNUAL=price_...
```

`STRIPE_PORTAL_RETURN_URL` is set by the production Compose file to `https://$DOMAIN/billing`. Do not set a test clock in production.

Optional monitoring:

```env
SENTRY_DSN=<dsn>
SENTRY_TRACES_SAMPLE_RATE=0.1
```

### DNS

Point the domain's `A`/`AAAA` record at the production server before starting Caddy if you want automatic TLS certificate issuance.

### Start

```bash
docker compose -f docker-compose.production.yml up -d --build
```

Check containers:

```bash
docker compose -f docker-compose.production.yml ps
```

Check app logs:

```bash
docker compose -f docker-compose.production.yml logs -f app
```

Check feed worker logs:

```bash
docker compose -f docker-compose.production.yml logs -f feed_worker
```

### Create the first admin

Do this explicitly; production does not seed a predictable admin password:

```bash
docker compose -f docker-compose.production.yml run --rm app \
  python scripts/create_admin.py --email you@yourdomain.com --name "Platform Admin"
```

Follow the command prompts/output and sign in with the credentials you create.

### Health checks

Public liveness:

```text
https://yourdomain.com/health/live
```

Database readiness:

```text
https://yourdomain.com/health/ready
```

Expected readiness response includes:

```json
{"ok":true,"database":"postgresql"}
```

---

## 7A. Phase 5 Stripe sandbox/live validation — REQUIRED before charging anyone

The automated suite simulates Stripe request/response payloads and signatures but does **not** contact your Stripe account. Complete this in Stripe sandbox first.

### Create Stripe products/prices

Create recurring GBP Prices matching the plan catalogue:

- Supplier Starter — £59 monthly / £590 annual
- Supplier Pro — £119 monthly / £1,190 annual
- Supplier Premium — £229 monthly / £2,290 annual
- Manufacturer — £199 monthly / £1,990 annual

Put the eight `price_...` IDs in `.env`. If you change launch pricing, change both Stripe and `PLANS` in `billing.py` so displayed pricing and entitlements remain aligned.

### Configure the webhook

Create a webhook endpoint:

```text
https://your-staging-domain.example/webhooks/stripe
```

Subscribe at minimum to:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `customer.subscription.paused`
- `customer.subscription.resumed`
- `customer.subscription.trial_will_end`
- `invoice.payment_failed`

Copy its signing secret into `STRIPE_WEBHOOK_SECRET`. An invalid/missing Stripe signature must return HTTP 400.

### Test a founding supplier checkout

1. Use a **verified supplier** whose local trial has more than 48 hours remaining (Stripe Checkout requires an explicit `trial_end` to be at least 48 hours in the future).
2. Open `/billing` and choose Supplier Pro monthly in Stripe **sandbox** mode.
3. Confirm Checkout does not require a payment method while £0 is due.
4. Complete Checkout and return to `/billing`.
5. Confirm `billing_subscriptions.stripe_customer_id` and `stripe_subscription_id` populate after webhooks arrive.
6. Confirm inventory remains searchable during the trial.
7. Click **Manage Stripe billing** and confirm the Billing Portal opens.
8. Repeat a webhook delivery from Stripe and confirm the `billing_events` table still contains one row for that event ID.

Stripe currently documents `payment_method_collection=if_required` for no-card Checkout trials and requires Checkout `trial_end` to be at least 48 hours in the future; the integration is coded to those rules. If a supplier starts Checkout with less than 48 hours left locally, Controls Exchange deliberately uses a two-day Stripe trial instead of charging them before their promised local trial ends. The next Stripe subscription webhook then becomes the source of truth for the slightly extended trial end.

**Also test the <48-hour edge case:** set a sandbox supplier's local `trial_ends_at` to roughly 24 hours ahead, start Checkout, and confirm Stripe creates a two-day trial rather than collecting payment immediately.

### Accelerate trial expiry with a Test Clock

This is the cleanest way to validate an actual Stripe transition without waiting 90 days:

1. In Stripe sandbox, create a Test Clock.
2. Put its `clock_...` ID into `STRIPE_TEST_CLOCK_ID` **before the application creates the supplier's Stripe Customer**.
3. Start Checkout for that supplier.
4. In Stripe, confirm the Customer is attached to the clock.
5. Advance the clock to just before trial end; verify your local reminder behaviour and Stripe `trial_will_end` event.
6. Advance through trial end with no payment method. With the default `STRIPE_TRIAL_END_BEHAVIOR=pause`, confirm the Stripe subscription becomes paused and Controls Exchange hides the supplier's commercial inventory.
7. Add a payment method / resume through your configured Stripe workflow and confirm a subscription webhook restores commercial access.

### Payment failure / cancellation checks

In sandbox, also prove:

- `past_due` keeps temporary grace access;
- `unpaid`, `paused` and `canceled` revoke commercial visibility;
- a canceled/expired company can still log in and retains inventory/history;
- starting Checkout for a company with a non-terminal Stripe subscription sends the user to the Billing Portal instead of creating a second subscription;
- a Stripe Price change maps to the expected application plan;
- downgrading disables excess promotion/feed/campaign slots without deleting stock or team members.

### Stripe settings to review before live mode

- business identity / statement descriptor;
- invoice branding and support contact;
- payment methods you want to accept;
- failed-payment retry / dunning rules;
- cancellation behaviour;
- UK VAT/tax treatment and whether your Stripe Prices are inclusive/exclusive of VAT;
- Billing Portal features (for founding launch, leave self-service cross-plan switching disabled);
- customer emails and trial reminders, bearing in mind Controls Exchange also sends its own 14/7/3-day/expired reminders.

Do not switch `sk_test_`/test Prices to live values until the complete sandbox checklist passes.

---

## 8. Transactional email live test

This environment could test email generation and local logging, but it could not prove external deliverability through your real domain/provider.

After deploying:

1. Register a fresh test buyer using an email address you control.
2. Confirm the verification email arrives.
3. Click the link and verify the account unlocks trading actions.
4. Use **Forgot password** and confirm the reset email arrives.
5. Send an RFQ and confirm the supplier notification arrives.
6. Reply/message in the RFQ and verify the corresponding notification.
7. Trigger a saved-search alert with a matching supplier inventory update.

Check SPF/DKIM/DMARC with your chosen email provider before using the platform commercially.

---

## 9. OpenAI photo identification live test — REQUIRED if enabling Photo ID

The code path is covered by a simulated API response, but a real call could not be made here with your credentials.

After adding `OPENAI_API_KEY`:

1. Restart the app.
2. Sign in with a verified buyer account.
3. Open `/identify`.
4. Confirm the page says **Vision service configured**.
5. Upload a clear photograph of a BMS controller label whose manufacturer/model you know.
6. Confirm the returned manufacturer and model are plausible.
7. Confirm a catalogue candidate is shown when that part exists in your canonical catalogue.
8. Try a deliberately unclear photo and confirm the result expresses uncertainty rather than inventing a model number.
9. Check the application logs for errors/timeouts.
10. Review your OpenAI project usage/billing after the call.

If you get an API error, verify:

- `OPENAI_API_KEY` is present inside the `app` container;
- `PHOTO_ID_MODEL` is available to the project;
- the server can make outbound HTTPS requests;
- the uploaded file is JPEG/PNG/WebP and below `PHOTO_ID_MAX_BYTES`.

---

## 10. Sourcing assistant live AI test

Without an API key, local model-code extraction is fully functional and tested.

With an API key:

1. Open `/source`.
2. Enter a messy request such as:

   `Need an old Trend controller - label says IQ 233 / UNB 230V, customer wants the same or the recorded replacement.`

3. Confirm the assistant resolves the correct canonical part.
4. Confirm replacements shown are only relationships that exist in Catalogue Admin.
5. Delete/disable the relationship and repeat the query; the replacement should disappear.

That last test is important: it proves the model is assisting retrieval rather than generating unsupported compatibility advice.

---

## 11. Real PostgreSQL / Docker validation — REQUIRED before launch

The application code supports PostgreSQL and the Compose files are supplied, but Docker/PostgreSQL were not available in the build environment used to create this package.

On a machine with Docker:

```bash
docker compose up --build
```

Then run the three smoke tests from inside the app container or against a staging database where appropriate.

Minimum checks:

- app starts with PostgreSQL rather than SQLite;
- `/health/ready` reports PostgreSQL;
- registration/login work;
- supplier inventory import works;
- catalogue records and relations persist after restart;
- feed worker can claim scheduled jobs;
- two workers do not process one feed twice;
- app/container restart preserves all data;
- database volume survives `docker compose down` (without `-v`).

---

## 12. Real HTTPS / Caddy validation — REQUIRED before launch

Caddy configuration is included but real certificate issuance requires a public domain/server.

Validate:

- `https://yourdomain.com` loads with no certificate warning;
- HTTP redirects to HTTPS;
- secure session cookies work;
- login persists correctly over HTTPS;
- `/health/ready` is reachable;
- PostgreSQL is **not** exposed publicly;
- ports 80/443 are the only application-facing ports required from the internet.

---

## 13. Real SFTP feed validation — REQUIRED if selling SFTP integration

The SFTP implementation is covered by an automated simulated Paramiko test. No external SFTP server was available in the build environment.

Before offering this to a supplier:

1. Create a staging SFTP account/server.
2. Upload a small `stock.csv`.
3. Add an SFTP feed through the supplier UI.
4. Configure the server SHA256 host-key fingerprint.
5. Run the feed manually.
6. Confirm inventory appears.
7. Modify quantities and rerun; confirm it updates rather than duplicates.
8. Remove one row from a clean feed and confirm feed-owned stock retires.
9. Put an invalid row into the feed and confirm missing stock is **not** retired.
10. Test both password auth and private-key auth if you intend to support both commercially.

---

## 14. Real IMAP / inbound email validation — REQUIRED if enabling emailed stock lists

The webhook/attachment processing is covered by automated tests. A real mailbox was not available here.

If using the IMAP plus-address workflow:

1. Set up a mailbox/catch-all for the import domain.
2. Configure all `INVENTORY_IMPORT_IMAP_*` variables.
3. Create an Email feed in the supplier account.
4. Send a CSV attachment to the generated `imports+<token>@...` address.
5. Confirm the worker sees the unread email.
6. Confirm the file imports and an import report is created.
7. Confirm the email is not repeatedly imported on subsequent worker loops.
8. Rotate the feed token and confirm the old address/token no longer maps to the feed.

---

## 15. Backup and restore live validation — REQUIRED before launch

SQLite backup creation was tested. The production PostgreSQL backup/restore path should be proven against your staging deployment.

1. Create recognizable staging data.
2. Run the backup job or wait for the scheduled backup.
3. Confirm a backup appears in the backup volume.
4. Copy the backup off the server.
5. Restore into a disposable/staging database using:

```bash
python scripts/restore_backup.py /path/to/backup --yes
```

6. Confirm accounts, inventory, RFQs, catalogue records and technical relationships are present.
7. Only after this test should you rely on the backup process for production.

For production, also copy backups to off-host/object storage. A backup stored only on the application server does not protect against server loss.

---

## 16. Sentry / monitoring live validation

Sentry integration is optional and was not connected to a real project here.

After setting `SENTRY_DSN`:

1. deploy to staging;
2. intentionally trigger a safe test exception or temporarily add a staging-only test route;
3. confirm it appears in Sentry with the environment and request ID;
4. remove/disable the test route afterwards.

Application logs already include request IDs and request duration.

---

## 17. Supplier feed security validation

Before production launch, verify:

- feed tokens are not present in application logs;
- rotating an API/email feed token revokes the old endpoint immediately;
- a supplier cannot view another company's feeds/import reports;
- production HTTP feed URLs must be HTTPS;
- internal/private IP targets are blocked unless `ALLOW_PRIVATE_FEED_HOSTS=true` is deliberately enabled;
- remote redirects are rejected;
- large inbound files are rejected at the configured upload limit;
- SFTP host-key verification is enforced in production.

These behaviours are covered in code/tests, but repeat them against staging infrastructure before launch.

---

## 18. Catalogue governance checklist

The technical catalogue can become one of the platform's most valuable assets, but only if trust is maintained.

Recommended operating rule:

- supplier inventory can map automatically to canonical identities;
- aliases can be proposed/imported but should be reviewed for ambiguous cases;
- AI output can help identify a part but should never auto-create a Reviewed relationship;
- replacement/compatibility relations should include a source;
- only a platform admin should mark relationships Reviewed;
- display unreviewed relationships as unreviewed;
- keep an audit trail of catalogue edits.

Before a public launch, consider adding a formal internal source hierarchy, e.g. manufacturer technical bulletin > manufacturer datasheet > authorised distributor documentation > experienced-integrator evidence > unverified community report.

---

## 19. Legal / policy work still required before a public launch

The included Terms and Privacy pages remain product drafts, not legal advice.

Before launching commercially, have appropriate UK legal/privacy review covering at least:

- marketplace intermediary terms;
- supplier inventory ownership/licensing;
- liability for technical catalogue information;
- disclaimers around replacements/compatibility;
- buyer/supplier disputes;
- privacy/data processing;
- cookies/session technology;
- email communications;
- third-party AI processing if Photo ID/AI sourcing is enabled;
- retention/deletion of sourcing and identification history.

---

## 20. Release checklist

Before each release:

```bash
python -m py_compile app.py platform_core.py inventory_ingestion.py catalog_intelligence.py billing.py intelligence.py procurement.py
python scripts/phase7_smoke_test.py
python scripts/phase6_smoke_test.py
python scripts/phase5_smoke_test.py
python scripts/phase4_smoke_test.py
python scripts/phase3_smoke_test.py
python scripts/phase2_smoke_test.py
```

Then for staging/production:

- [ ] PostgreSQL migration/startup succeeds
- [ ] `/health/ready` passes
- [ ] backup taken before upgrade
- [ ] login/email verification works
- [ ] 90-day founding trial starts on supplier verification
- [ ] Stripe sandbox checkout/webhook/portal tests pass before charging anyone
- [ ] expired/paused billing hides inventory without deleting account data
- [ ] supplier import works
- [ ] search/RFQ works
- [ ] accepting a quote creates an order and buyer PO submission works
- [ ] supplier acknowledgement/dispatch/delivery workflow works
- [ ] order document upload/download is private to the two companies
- [ ] webhook worker runs and a real signed HTTPS event is received
- [ ] buyer/supplier order API tested with a staging key
- [ ] catalogue/source assistant works
- [ ] Photo ID live test passes if enabled
- [ ] scheduled feed worker runs
- [ ] backup created and off-host copy exists
- [ ] no demo accounts/data in production
- [ ] terms/privacy versions approved for launch

---

## 21. Validation status for this Phase 7 package

### Tested here and passing

- Phase 7 order lifecycle: accepted quote → PO → acknowledge/process/dispatch/deliver
- Phase 7 buyer/supplier order API and scope isolation
- Phase 7 order-document upload/download/access controls and checksums
- Phase 7 HMAC webhook signing/delivery using a simulated receiver
- Phase 7 webhook secret one-time display/encrypted-at-rest storage
- Phase 7 accepted-RFQ order backfill idempotency
- database + order-document backup/restore using temporary local storage
- Phase 6 smoke suite / intelligence API
- Phase 5 founding-trial lifecycle using temporary SQLite
- Phase 5 plan entitlements / promotions / analytics / manufacturer campaigns
- simulated Stripe Checkout request construction (including no-card trial settings)
- signed Stripe webhook verification, duplicate delivery handling and Price-to-plan mapping
- downgrade reconciliation of entitlement-only resources
- trial reminder deduplication
- Python syntax for application/modules/scripts
- SQLite schema/migrations
- canonical catalogue CRUD path used by the smoke test
- aliases and inventory linking
- technical relationship graph
- local sourcing assistant
- local visible-text Photo ID fallback
- simulated OpenAI image request/response contract
- Phase 4 smoke suite
- Phase 3 regression suite
- Phase 2 regression suite
- cross-company permissions exercised by existing suites

### Not possible to fully validate here; test in your environment

- real outbound Phase 7 HTTPS webhook delivery / retry persistence on PostgreSQL
- real ERP/procurement API integration with a customer system
- real order-document persistent volume across redeploy and off-host restore
- malware scanning/content-disarm service for uploaded documents (not implemented)
- real Stripe sandbox/live API calls, Checkout, Billing Portal and webhook delivery
- real Stripe Test Clock trial-expiry transition
- live VAT/tax/payment-method configuration in your Stripe account
- real OpenAI API call with your key/project/model access
- real Docker runtime
- live PostgreSQL container/database
- real public DNS + Caddy certificate issuance
- real Resend/SMTP deliverability from your domain
- real external SFTP server
- real IMAP mailbox / plus-address delivery
- real Sentry project ingestion
- production PostgreSQL backup + destructive restore drill
- production load/performance at large inventory scale

That list should be updated after every future phase so nothing silently falls between development and deployment.
