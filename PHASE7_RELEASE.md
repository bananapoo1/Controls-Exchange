# Controls Exchange — Phase 7 release

Phase 7 adds the enterprise procurement/order workflow on top of the Phase 1–6 marketplace.

## What changed

### Quote → order workflow
- Selecting an accepted supplier quote creates exactly one order record tied to the RFQ recipient.
- Existing accepted RFQs are backfilled idempotently on upgrade.
- Buyer and supplier company IDs, quoted price/currency, part, quantity and RFQ lineage are retained.
- Buyer can submit PO number, project/reference, delivery address and delivery contact.
- Supplier can acknowledge, process, dispatch and mark delivered, with constrained state transitions.
- Supplier order reference, expected dispatch, carrier, tracking number and tracking URL are retained.
- Either commercial party can record a cancellation subject to workflow guards; the UI explicitly states this does not itself determine contractual rights.

### Order collaboration
- Private buyer↔supplier order message thread.
- Private order documents for PO, order confirmation, invoice, dispatch note, certificate/test record and other supported documents.
- Documents use random stored filenames, extension allow-list, configured size cap and SHA-256 checksum.
- Download access is re-authorised on every request and responses use `Cache-Control: private, no-store`.
- Event timeline and platform audit entries are retained.
- Platform admins are read-only for commercial order actions.

### ERP/API
New scopes:
- `orders:read`
- `orders:write`

New endpoints:
- `GET /v1/orders`
- `GET /v1/orders/{order_id}`
- `POST /v1/orders/{order_id}/buyer-details`
- `POST /v1/orders/{order_id}/status`

Buyer private-beta companies receive one API-key slot at 500 requests/hour, but their scopes are restricted server-side to order read/write only. Supplier Pro/Premium/Manufacturer keys can combine the existing marketplace scopes with order scopes.

### Signed outbound webhooks
- Company owners/admins can configure lifecycle endpoints at `/integrations/webhooks`.
- One-time high-entropy `cxwhsec_...` signing secret.
- Secret encrypted at rest with `INTEGRATION_ENCRYPTION_KEY` because the worker must sign outbound events.
- HMAC-SHA256 over `timestamp.raw_body`.
- Headers: `X-CX-Event`, `X-CX-Event-ID`, `X-CX-Timestamp`, `X-CX-Signature`.
- Async delivery queue with exponential retry/backoff.
- Endpoint pause/enable/delete and immediate secret rotation.
- HTTPS-only in production, no redirects, no embedded URL credentials and private-network SSRF blocking by default.
- Dedicated `scripts/webhook_worker.py` included in both Compose stacks.
- PostgreSQL workers claim due rows using `FOR UPDATE SKIP LOCKED`.

Events include:
- `order.created`
- `order.submitted`
- `order.acknowledged`
- `order.processing`
- `order.dispatched`
- `order.delivered`
- `order.cancelled`
- `order.message_added`
- `order.document_added`

### Persistence and backups
- Production application now mounts `app_data:/app/data` so order documents survive container replacement.
- `ORDER_DOCUMENT_DIR` is required in production configuration.
- Database backup worker also writes a matching order-document `tar.gz` archive.
- Restore tooling accepts `--documents-backup` and validates archive paths before extraction.

## Automated validation completed here

```text
PHASE7_SMOKE_TEST_OK
PHASE6_SMOKE_TEST_OK
PHASE5_SMOKE_TEST_OK
PHASE4_SMOKE_TEST_OK
PHASE3_SMOKE_TEST_OK
PHASE2_SMOKE_TEST_OK
```

The Phase 7 suite covers quote selection, idempotent order creation, PO handoff, document access/checksum, buyer and supplier API scopes, API fulfilment updates, webhook secret handling, HMAC headers, queued delivery and migration idempotency.

Additional checks completed:
- Python syntax compilation for application/modules/scripts.
- All Jinja templates compile.
- Development and production Compose YAML parse.
- Local database + order-document backup/restore tested with a temporary PDF.
- Production configuration accepts intended safe values and rejects missing persistent `ORDER_DOCUMENT_DIR`.

## Requires live/staging validation

The following are implemented but cannot be fully proven in this build environment:
- public HTTPS webhook delivery to a real external receiver;
- retry persistence and multiple concurrent webhook workers against a live PostgreSQL database;
- integration with a real ERP/procurement product;
- order-document persistence across a real Docker host redeploy;
- off-host backup replication and full destructive PostgreSQL + document restore;
- network-layer egress controls / DNS-rebinding defence at the hosting provider;
- malware scanning/content disarm for user-uploaded documents (not implemented);
- final legal review of quote acceptance, PO submission, order cancellation and platform intermediary wording.

See `SETUP_AND_TESTING.md` for exact validation steps.
