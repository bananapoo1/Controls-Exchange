# Visual UI testing

Controls Exchange includes a lightweight screenshot harness for reviewing the real Jinja templates without a live database.

## First-time setup

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
```

If Chromium is already installed system-wide, the script will use it automatically. You can override the executable with either `--chromium /path/to/chromium` or `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`.

## Render the main launch views

```bash
python scripts/render_ui.py --quick
```

This writes desktop and mobile homepage screenshots to `ui_snapshots/`.

To render the complete review set:

```bash
python scripts/render_ui.py --all
```

The complete set currently covers:

- desktop and mobile homepages;
- the open mobile menu;
- live-search results using synthetic stock;
- the RFQ modal;
- the supplier acquisition page;
- mobile registration;
- buyer, supplier and admin dashboards.

List individual view names with:

```bash
python scripts/render_ui.py --list
```

Render one or more targeted views with:

```bash
python scripts/render_ui.py --view supplier-dashboard --view rfq-modal
```

## Fixtures

Synthetic render data lives under `ui_fixtures/`. It is deliberately separate from the application database so screenshots are repeatable and safe to generate anywhere.

Do not put real customer data, API keys, passwords, quote history or commercially sensitive production information into visual fixtures.

## What this does and does not prove

The harness is useful for layout, responsive behaviour, navigation, modal states and product polish. It does **not** replace testing the deployed application in a real browser against PostgreSQL, Stripe, email, external feeds and live API endpoints. Those checks remain in `SETUP_AND_TESTING.md`.

Before a major UI merge, review at minimum:

```bash
python scripts/render_ui.py --all
node --check static/mvp.js
python -m compileall -q .
```

Then verify the live Docker app at desktop and iPhone widths before production deployment.
