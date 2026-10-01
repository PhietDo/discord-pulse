"""Per-request database connection, filters, and page rendering."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from pulse.db import connect
from pulse.stats import top_channels
from pulse.web.context import base_context
from pulse.web.filters import Filters, parse_filters


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_filters(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    days: str | None = None,
    channel: str | None = None,
) -> Filters:
    known = {c["id"] for c in top_channels(conn)}
    return parse_filters(days, channel, request.app.state.settings.clock(), known)


def render(request: Request, template: str, conn, f: Filters, active: str, *, status_code: int = 200, **ctx) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, conn, f, active), **ctx}, status_code=status_code
    )
