# Security notes

## General
- Never use development seed credentials in production.
- Production requires PostgreSQL, an HTTPS public URL, a real transactional-email provider, a strong `SECRET_KEY`, and a separate strong `INTEGRATION_ENCRYPTION_KEY`.
- Create the first production admin explicitly with `scripts/create_admin.py`.
- Keep `.env`, database credentials and integration encryption keys out of source control.
- Restrict PostgreSQL to the private container/network layer.
- Backups contain customer/supplier business data and should be encrypted and access-controlled when copied off-host.

## Authentication / company isolation
- State-changing browser forms use CSRF protection.
- Password-reset and verification tokens are one-use and expiring.
- Password resets invalidate existing sessions.
- Team permissions are scoped by company role, including last-owner protection.
- Inventory import jobs/feed settings are always queried with the authenticated company ID.

## Phase 2 commercial privacy
- RFQ messages are scoped to one RFQ recipient; competing suppliers do not receive each other's quotes/messages.
- Supplier trust metrics derive from marketplace events without exposing raw cross-supplier commercial data.
- Saved-search and wanted-match data contains confidential sourcing interests.

## Phase 3 integration security
- SFTP/API credentials are encrypted at rest using `INTEGRATION_ENCRYPTION_KEY`.
- Push/email ingest tokens are stored as a hash for lookup and encrypted only where the UI/worker must recover the endpoint/address.
- Token-bearing inbound URLs are redacted from application access/error logs.
- Rotating a feed token immediately revokes the previous endpoint/address.
- Inbound request bodies and attachments are capped by `MAX_UPLOAD_BYTES`.
- Production HTTP feeds must use HTTPS.
- Remote-feed redirects are rejected; configure the final URL directly.
- URLs containing embedded usernames/passwords are rejected.
- Private, loopback, link-local, reserved and multicast feed targets are rejected in production by default to reduce SSRF/internal-network risk.
- `ALLOW_PRIVATE_FEED_HOSTS=true` is an explicit escape hatch for deployments with intentional protected-network connectivity.
- Production SFTP feeds require a SHA256 host-key fingerprint.
- Sync feeds never retire stock belonging to another feed/manual source.
- Empty feed syncs are rejected.
- Feed runs containing row-validation errors suppress missing-line retirement.
- PostgreSQL scheduled workers claim feeds using row locks with `SKIP LOCKED` to avoid duplicate processing.

## Monitoring / external services
- Sentry is optional and configured with `send_default_pii=False`.
- Ensure any email provider, Sentry account and hosting provider are covered by appropriate data-processing terms for your customers.
- Terms/privacy templates remain drafts and should be reviewed before launch.

## Phase 4 technical intelligence / AI notes

- Raw Photo ID uploads are processed in memory and are not persisted by Controls Exchange.
- When `OPENAI_API_KEY` is configured, Photo ID sends the uploaded image to the configured OpenAI Responses API model; deployment privacy/terms should disclose this third-party processing.
- Photo uploads are restricted to JPEG/PNG/WebP and capped by `PHOTO_ID_MAX_BYTES`.
- The sourcing model, when enabled, is restricted to extracting retrieval terms (manufacturer/model/keywords). It is not trusted as a source of compatibility, replacement, firmware or installation guidance.
- Technical replacements/alternatives are read only from `catalog_relations`.
- A technical relationship cannot be marked Reviewed without a source.
- Catalogue changes and photo/sourcing actions are written to the platform audit/history tables.
- Treat technical sources and supplier inventory as confidential business data where appropriate.

## Phase 5 billing / Stripe security

- Controls Exchange never receives or stores card numbers; hosted Stripe Checkout/Billing Portal handle payment details.
- Stripe webhook requests require a valid `Stripe-Signature` HMAC and a five-minute timestamp tolerance.
- Webhook event IDs are retained in `billing_events` so ordinary redelivery is idempotent.
- Stripe Price IDs are mapped to application plans from server-side environment configuration rather than trusting client input or Stripe metadata alone.
- Supplier and manufacturer account kinds are kept on separate entitlement paths; an invalid cross-audience Stripe Price does not switch application entitlements.
- A company with an existing non-terminal Stripe subscription is routed to the Billing Portal rather than creating a second subscription.
- No-card trial Checkout explicitly uses `payment_method_collection=if_required`; `STRIPE_TRIAL_END_BEHAVIOR` defaults to `pause`.
- `STRIPE_TEST_CLOCK_ID` is test-only and production configuration validation rejects it.
- Production configuration rejects Stripe enablement when the webhook secret or required Price IDs are missing.
- Commercial expiry/revocation hides inventory from marketplace discovery but does not delete supplier data.
- Plan downgrades may disable excess feeds, promotions and campaigns; inventory and team users are never automatically deleted.
- Treat Stripe customer/subscription IDs as business metadata and do not expose secret keys or webhook secrets in logs/UI.
- Before live billing, review VAT/tax treatment, dunning/retry rules, refunds/cancellation terms and Billing Portal settings with appropriate finance/legal advice.


## Phase 6 market-intelligence / API security

- Commercial market intelligence is aggregate-only. Buyer identities and raw competitor quotes are never returned to supplier dashboards or the external API.
- Production enforces minimum cohorts: 5 demand events / 3 independent demand companies and 5 quotes / 3 independent suppliers. Configuration below those thresholds aborts startup.
- Quote statistics are calculated separately by currency; GBP/EUR/USD values are never pooled into one benchmark.
- Repeated identical searches from one company are de-duplicated inside `SEARCH_DEMAND_DEDUP_MINUTES` (30 minutes by default) to reduce trivial demand manipulation.
- API secrets use high-entropy `cxk_...` values, are displayed once and are stored only as SHA-256 hashes. The key prefix is retained for identification.
- API keys are scoped (`inventory:read`, `catalog:read`, `intelligence:read`), company-bound, revocable, plan-gated and database-rate-limited.
- Every successful external API request is metered in `api_usage_events`; raw API secrets are not logged by the application.
- API authentication re-checks commercial access and the current plan on every request. A billing downgrade therefore removes access even before key cleanup runs.
- Plan reconciliation revokes excess active API keys on downgrade without deleting usage/audit history.
- The API is intended primarily for server-to-server integration. CORS is not opened broadly by default.
- The synthetic Phase 6 intelligence seeder refuses production and its output must never be represented as genuine market data.
- Quote intelligence is a benchmark of marketplace quotes, not proof of a completed sale or final invoiced price.

## Phase 7 procurement / order security

- Accepting an RFQ quote creates an order record; order access is limited to the buyer company, supplier company and read-only platform administrators.
- Platform administrators are intentionally read-only on commercial order actions. They cannot submit a buyer PO, change supplier fulfilment state, add order messages/documents or cancel an order for either party.
- Buyer API keys created during private beta are restricted server-side to `orders:read` / `orders:write`; requesting inventory/catalogue/intelligence scopes does not grant them.
- Supplier order writes are limited to the supplier company on the order; buyer order writes are limited to the buyer company.
- Order state transitions are validated server-side. A supplier cannot mark an order delivered before dispatch, and a buyer cannot modify PO details after dispatch/cancellation.
- Order documents use random stored filenames, `Path.name` sanitisation, an extension allow-list, a size cap and SHA-256 checksums. Download routes re-check order-company access and return `Cache-Control: private, no-store`.
- The current MVP does **not** include malware scanning or content disarm/reconstruction for uploaded documents. Add a scanning service before opening document uploads broadly to unknown counterparties.
- Order-document files are persisted separately from PostgreSQL and are included in the Phase 7 document backup archive. Protect document backups to the same standard as the database.
- Webhook signing secrets are high entropy, displayed once, encrypted at rest with `INTEGRATION_ENCRYPTION_KEY`, rotatable and never placed in webhook URLs.
- Outbound webhooks sign `timestamp.raw_body` with HMAC-SHA256 and send the signature in `X-CX-Signature: v1=<hex>`. Receivers should enforce their own timestamp replay window and de-duplicate `X-CX-Event-ID`.
- Webhook delivery is asynchronous. A receiver outage cannot roll back an order action; failed deliveries retry with exponential backoff up to `WEBHOOK_MAX_ATTEMPTS`.
- Production webhook destinations must use HTTPS, redirects are not followed, embedded URL credentials are rejected, and private/loopback/link-local/reserved destinations are blocked by default to reduce SSRF risk.
- `ALLOW_PRIVATE_WEBHOOK_HOSTS=true` is an explicit escape hatch for intentionally private enterprise integrations and should be combined with network-layer egress controls.
- Multiple PostgreSQL webhook workers use `FOR UPDATE SKIP LOCKED` when claiming due deliveries.
- Controls Exchange records workflow only: it does not collect order payment, provide escrow, take title to goods or itself determine contractual formation/cancellation. Final marketplace terms should reflect the intended legal position.
