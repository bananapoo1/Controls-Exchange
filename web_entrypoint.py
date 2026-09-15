from __future__ import annotations

import os

import app as app_module
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import app, render
from platform_core import ENVIRONMENT, logger


_BROWSER_ERROR_CODES = {403, 404}
_JSON_PREFIXES = ("/api/", "/health/", "/static/")


# Render staging intentionally uses EMAIL_PROVIDER=log. The core log provider
# persists messages to an ephemeral outbox file for local development, which is
# inconvenient to inspect on Render. Mirror those staging-only messages to the
# service log so verification/reset links are available to the site owner.
# Production configuration rejects the log provider, so this never exposes
# email contents in a production deployment.
_original_send_email = app_module.send_email


def _staging_visible_send_email(to_email: str, subject: str, body: str, html: str | None = None) -> bool:
    sent = _original_send_email(to_email, subject, body, html)
    if ENVIRONMENT == "staging" and os.getenv("EMAIL_PROVIDER", "").strip().lower() == "log":
        logger.info("staging_email_outbox to=%s subject=%s\n%s", to_email, subject, body)
    return sent


if ENVIRONMENT == "staging" and os.getenv("EMAIL_PROVIDER", "").strip().lower() == "log":
    app_module.send_email = _staging_visible_send_email


def _wants_branded_error(request: Request, status_code: int) -> bool:
    if status_code not in _BROWSER_ERROR_CODES:
        return False
    if request.method not in {"GET", "HEAD"}:
        return False
    if request.url.path.startswith(_JSON_PREFIXES):
        return False
    return True


@app.exception_handler(StarletteHTTPException)
async def branded_http_exception(request: Request, exc: StarletteHTTPException):
    """Render friendly browser errors while preserving JSON for APIs and health checks."""
    if _wants_branded_error(request, exc.status_code):
        if exc.status_code == 404:
            heading = "Page not found"
            message = "That page does not exist or may have moved."
        else:
            heading = "Access denied"
            message = "You do not have permission to view this page."
        return render(
            request,
            "error.html",
            status_code=exc.status_code,
            error_status=exc.status_code,
            error_heading=heading,
            error_message=message,
        )

    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )
