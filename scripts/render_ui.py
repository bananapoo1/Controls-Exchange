#!/usr/bin/env python3
"""Render Controls Exchange UI snapshots without a running database/server.

The script renders real Jinja templates with synthetic fixture data, inlines the
repository CSS/JS, and uses Chromium via Playwright. This makes visual review
repeatable in development environments (including ChatGPT's working container).

Examples:
    python scripts/render_ui.py --quick
    python scripts/render_ui.py --all
    python scripts/render_ui.py --view supplier-dashboard --output /tmp/cx-ui

First-time setup:
    pip install -r requirements-dev.txt
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "ui_fixtures"
DEFAULT_OUTPUT = ROOT / "ui_snapshots"
CSS_FILES = ("styles.css", "mvp.css", "modern.css", "visual-polish.css")
BASE_URL = "http://controls-exchange.test/"


def as_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: as_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [as_namespace(item) for item in value]
    return value


def read_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def find_chromium(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
    ):
        if candidate:
            return candidate
    return None


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.globals["can"] = lambda _user, _permission: True
    return env


def common_context() -> dict[str, Any]:
    anon = read_json("anonymous.json")
    return {
        "request": as_namespace({"url": {"path": "/"}}),
        "public_base_url": "https://controls-exchange.test",
        "user": None,
        "billing": None,
        "flash": None,
        "csrf_token": "visual-fixture-token",
        "founding_trial_days": anon["founding_trial_days"],
        "environment": "production",
        "stats": as_namespace(anon["stats"]),
        "prefill_query": anon.get("prefill_query", ""),
        "next": "",
        "account_type": "buyer",
    }


def context_for(role: str | None = None, *, dashboard: bool = False, fixture_name: str | None = None) -> dict[str, Any]:
    ctx = common_context()
    if not role and not fixture_name:
        return ctx
    fixture = read_json(f"{fixture_name or role}.json")
    ctx["user"] = as_namespace(fixture["user"])
    if fixture.get("billing"):
        ctx["billing"] = as_namespace(fixture["billing"])
    if dashboard:
        ctx["data"] = as_namespace(fixture["dashboard"])
    return ctx


def inline_assets(html: str) -> str:
    css = "\n".join((ROOT / "static" / name).read_text(encoding="utf-8") for name in CSS_FILES)
    js = (ROOT / "static" / "mvp.js").read_text(encoding="utf-8")

    # Avoid any external dependency during visual tests. System fonts are close
    # enough for layout QA and make snapshots deterministic/offline.
    html = re.sub(r"\s*<link[^>]+fonts\.googleapis\.com[^>]*>", "", html)
    html = re.sub(r"\s*<link[^>]+fonts\.gstatic\.com[^>]*>", "", html)
    html = re.sub(r"\s*<link[^>]+rel=\"preconnect\"[^>]*>", "", html)
    html = re.sub(r"\s*<link[^>]+href=\"/static/(?:styles|mvp|modern|visual-polish)\.css\"[^>]*>", "", html)
    html = html.replace("</head>", f"<base href=\"{BASE_URL}\"><style>{css}</style></head>")
    html = html.replace('<script src="/static/mvp.js" defer></script>', f"<script>{js}</script>")
    return html


def render_template(template_name: str, context: dict[str, Any]) -> str:
    env = build_environment()
    return inline_assets(env.get_template(template_name).render(**context))


@dataclass(frozen=True)
class View:
    name: str
    template: str
    width: int
    height: int
    role: str | None = None
    dashboard: bool = False
    full_page: bool = True
    action: str | None = None
    fixture: str | None = None


VIEWS = {
    view.name: view
    for view in (
        View("home-desktop", "index.html", 1440, 1000),
        View("home-mobile", "index.html", 390, 844),
        View("home-mobile-menu", "index.html", 390, 844, full_page=False, action="mobile-menu"),
        View("search-results", "index.html", 1440, 1000, role="buyer", full_page=False, action="search"),
        View("rfq-modal", "index.html", 1440, 1000, role="buyer", full_page=False, action="rfq"),
        View("suppliers-desktop", "suppliers.html", 1440, 1000),
        View("register-mobile", "register.html", 390, 844, action="supplier-register"),
        View("buyer-dashboard", "dashboard.html", 1440, 1000, role="buyer", dashboard=True),
        View("supplier-dashboard", "dashboard.html", 1440, 1000, role="supplier", dashboard=True),
        View("new-buyer-dashboard", "dashboard.html", 1440, 1000, role="buyer", dashboard=True, fixture="new_buyer"),
        View("new-supplier-dashboard", "dashboard.html", 1440, 1100, role="supplier", dashboard=True, fixture="new_supplier"),
        View("buyer-rfqs-empty", "rfqs.html", 1440, 850, role="buyer"),
        View("supplier-rfqs-empty", "rfqs.html", 1440, 850, role="supplier"),
        View("buyer-wanted-empty", "wanted.html", 1440, 950, role="buyer"),
        View("buyer-orders-empty", "orders.html", 1440, 850, role="buyer"),
        View("admin-dashboard", "dashboard.html", 1440, 1000, role="admin", dashboard=True),
    )
}


def attach_routes(page: Page) -> None:
    search_payload = read_json("search_results.json")

    def search_handler(route) -> None:  # Playwright's Route type is optional at runtime
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(search_payload),
        )

    page.route("**/api/search**", search_handler)
    # Visual fixtures should not make accidental network calls.
    page.route("https://**", lambda route: route.abort())


def apply_action(page: Page, action: str | None) -> None:
    if action == "mobile-menu":
        page.locator("#menuButton").click()
        page.locator("#mobileMenu").wait_for(state="visible")
    elif action in {"search", "rfq"}:
        page.locator("#heroSearch").fill("IQ233")
        page.locator("#heroSearchButton").click()
        page.locator(".result-card").first.wait_for(state="visible")
        if action == "rfq":
            page.locator(".result-card .single-rfq").first.click()
            page.locator("#rfqModal").wait_for(state="visible")
    elif action == "supplier-register":
        page.locator('input[name="account_type"][value="supplier"]').check()


def render_view(browser, view: View, output: Path) -> Path:
    ctx = context_for(view.role, dashboard=view.dashboard, fixture_name=view.fixture)
    if view.name.endswith("rfqs-empty") or view.name == "buyer-wanted-empty":
        ctx["rows"] = []
    if view.name == "buyer-orders-empty":
        ctx.update({"rows": [], "order_statuses": ["awaiting_po", "submitted", "acknowledged", "processing", "dispatched", "delivered", "cancelled"], "selected_status": ""})
    if view.name == "register-mobile":
        ctx["account_type"] = "supplier"
    html = render_template(view.template, ctx)

    page = browser.new_page(viewport={"width": view.width, "height": view.height})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    attach_routes(page)
    page.set_content(html, wait_until="domcontentloaded", timeout=15_000)
    apply_action(page, view.action)
    page.emulate_media(reduced_motion="reduce")

    target = output / f"{view.name}.png"
    page.screenshot(path=str(target), full_page=view.full_page, timeout=60_000)
    page.close()
    if errors:
        raise RuntimeError(f"{view.name} generated browser errors: {errors}")
    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Render all visual fixtures (default).")
    group.add_argument("--quick", action="store_true", help="Render desktop + mobile homepage only.")
    group.add_argument("--view", choices=sorted(VIEWS), action="append", help="Render one named view; repeat as needed.")
    parser.add_argument("--list", action="store_true", help="List available view names and exit.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Snapshot output directory.")
    parser.add_argument("--chromium", help="Explicit Chromium/Chrome executable path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list:
        print("\n".join(sorted(VIEWS)))
        return 0

    if args.quick:
        names = ["home-desktop", "home-mobile"]
    elif args.view:
        names = args.view
    else:
        names = list(VIEWS)

    chromium = find_chromium(args.chromium)
    args.output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        launch_args: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
        if chromium:
            launch_args["executable_path"] = chromium
        browser = playwright.chromium.launch(**launch_args)
        try:
            for name in names:
                path = render_view(browser, VIEWS[name], args.output)
                print(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
