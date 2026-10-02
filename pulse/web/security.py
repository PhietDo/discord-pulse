"""Cross-site and DNS-rebinding protection, and limits on agent jobs started from the dashboard."""
from __future__ import annotations

import sqlite3
import threading
from http import HTTPStatus
from typing import Callable
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_DIGESTS = 1
MAX_INVESTIGATIONS = 2


def _cross_site(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()


async def refuse_cross_site(request: Request, call_next) -> Response:
    """Writes from another site (or a page whose Origin is not this host) get a 403.
    Requests without Sec-Fetch-Site or Origin (curl, scripts) pass."""
    if request.method in UNSAFE_METHODS and _cross_site(request):
        return PlainTextResponse("Cross-site request refused", status_code=403)
    return await call_next(request)


LOCKED_TEXT = "The pipeline is writing right now; try again in a few seconds."


def error_response(request: Request, status_code: int, detail: str, headers: dict | None = None) -> Response:
    """A small HTML error page, or just a fragment for htmx requests (swapped in place)."""
    template = "_error.html" if request.headers.get("HX-Request") else "error.html"
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    return request.app.state.templates.TemplateResponse(
        request, template, {"title": title, "detail": detail}, status_code=status_code, headers=headers
    )


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    return error_response(request, exc.status_code, str(exc.detail), getattr(exc, "headers", None))


async def database_busy(request: Request, exc: sqlite3.OperationalError) -> Response:
    """A write that lost the race with the pipeline's write lock gets a 503, not a 500."""
    if request.method in UNSAFE_METHODS and "locked" in str(exc):
        return error_response(request, 503, LOCKED_TEXT)
    raise exc


class JobSlots:
    """Agent jobs running in this process, so the dashboard can't start a pile of them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.digests = 0
        self.investigations = 0

    def take_digest(self) -> None:
        with self.lock:
            if self.digests >= MAX_DIGESTS:
                raise HTTPException(status_code=409, detail="A digest is already being written")
            self.digests += 1

    def take_investigation(self) -> None:
        with self.lock:
            if self.investigations >= MAX_INVESTIGATIONS:
                raise HTTPException(
                    status_code=429, detail="Two investigations are already running; try again in a minute"
                )
            self.investigations += 1

    def release(self, kind: str) -> None:
        with self.lock:
            setattr(self, kind, max(0, getattr(self, kind) - 1))

    def wrap(self, kind: str, job: Callable[..., None]) -> Callable[..., None]:
        """The background task: runs the job, then frees its slot whatever happened."""

        def run(*args, **kwargs) -> None:
            try:
                job(*args, **kwargs)
            finally:
                self.release(kind)

        return run
