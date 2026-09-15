# Controls Exchange staging deployment

This guide is for a **temporary live QA environment**, not production.

The repository now contains `render.yaml`, which provisions:

- one free Render web service using the existing Dockerfile;
- one free Render Postgres database;
- the feed and webhook polling workers inside the same staging web container.

That co-location is deliberate for staging because Render does not offer free standalone background-worker instances. Production should return to separate web/worker processes.

## Deploy from GitHub

1. Sign in to Render and choose **New > Blueprint**.
2. Connect the `bananapoo1/Controls-Exchange` GitHub repository.
3. Render should detect `render.yaml` automatically.
4. When prompted, enter:
   - `SEED_ADMIN_EMAIL` — your real admin email for staging.
   - `SEED_ADMIN_PASSWORD` — a long, unique staging password. Do not reuse another password.
5. Approve the Blueprint.
6. Wait for both `controls-exchange-staging` and `controls-exchange-staging-db` to become available.
7. Open the generated `https://...onrender.com` URL.
8. Check `/health/ready` returns a healthy response.

`PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, the database connection string and cryptographic application secrets are wired automatically by the Blueprint.

## First live checks

Run these before sharing the URL publicly:

1. Homepage loads on desktop and mobile.
2. Brand logo/favicons render.
3. `/health/ready` is healthy.
4. Sign in with the staging admin account.
5. Register a buyer account.
6. Register a supplier account.
7. Confirm unverified suppliers remain private.
8. Approve the supplier as admin and confirm the 90-day founding trial begins then.
9. Upload a small inventory CSV/XLSX file and search for one of its parts from a buyer account.
10. Create an RFQ and a wanted request.
11. Select a quote and exercise the order flow.
12. Check the mobile menu and authentication pages on a real phone.

## Email on the first deploy

The Blueprint deliberately starts with:

```text
EMAIL_PROVIDER=log
```

That prevents a half-configured email domain from blocking the first deployment. Verification/reset messages will appear in service logs rather than being delivered.

When you have a sending domain or want to test real delivery, change the service environment to either Resend or SMTP using the variables documented in `.env.example` and `SETUP_AND_TESTING.md`.

## Optional integrations

Leave these unconfigured for the first staging deploy unless you specifically want to test them:

- Stripe;
- OpenAI Photo ID / sourcing extraction;
- Sentry;
- SFTP feeds;
- IMAP inventory ingestion.

The local/manual fallbacks remain available where applicable.

## Important free-tier limitations

This environment is disposable:

- the free web service can spin down when idle, so the first request after inactivity may be slow;
- the local filesystem is ephemeral, so uploaded order documents can disappear after a restart/redeploy;
- the free Postgres database is temporary and should not contain irreplaceable data;
- feed/webhook workers stop whenever the free web service sleeps;
- do not use this layout for production traffic or real customer commercial documents.

The app is intentionally configured as `ENVIRONMENT=staging`, so it remains `noindex,nofollow` while we test it.

## When the URL is live

Send the staging URL in ChatGPT. The next QA pass should cover:

- public landing/search flow;
- real registration and cookies;
- desktop + iPhone responsive behaviour;
- cold-start behaviour;
- forms, redirects and error states;
- buyer/supplier/admin journeys;
- performance and obvious browser-console failures;
- public metadata/social previews;
- accessibility and keyboard navigation;
- security headers and HTTPS behaviour.

Do not treat the staging environment as production sign-off. The remaining production checklist in `SETUP_AND_TESTING.md` still applies.
