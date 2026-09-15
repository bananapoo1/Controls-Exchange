from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import app, render


_BROWSER_ERROR_CODES = {403, 404}
_JSON_PREFIXES = ("/api/", "/health/", "/static/")


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
