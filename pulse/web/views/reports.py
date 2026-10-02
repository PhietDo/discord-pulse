import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup

from pulse.citations import render_html
from pulse.models import from_iso
from pulse.web import jobs, queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def require_agents(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.agents_on:
        raise HTTPException(status_code=409, detail=settings.agents_off_text)


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, pending: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    job = None
    if pending:
        try:
            since = datetime.fromtimestamp(int(pending), timezone.utc)
        except (ValueError, OverflowError, OSError):
            since = None
        if since is not None:
            state, reason = queries.digest_job_state(conn, since, f.now)
            job = {"state": state, "reason": reason, "pending": pending}
    return render(request, "reports.html", conn, f, "reports", rows=queries.report_rows(conn), job=job)


@router.post("/reports/digest")
def write_weekly(request: Request, background: BackgroundTasks, f: Filters = Depends(get_filters)):
    require_agents(request)
    settings = request.app.state.settings
    slots = request.app.state.job_slots
    slots.take_digest()
    background.add_task(slots.wrap("digests", jobs.run_digest_job), settings, None)
    return RedirectResponse(f"/reports{f.qs(pending=int(f.now.timestamp()))}", status_code=303)


def _cards(conn, raw: str) -> list[dict]:
    return cards_by_ids(conn, json.loads(raw or "[]"))


@router.get("/reports/digest/{digest_id}", response_class=HTMLResponse)
def digest_detail(request: Request, digest_id: int, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    row = conn.execute(
        "SELECT d.*, l.name AS launch FROM digests d LEFT JOIN launches l ON l.id = d.launch_id WHERE d.id = ?",
        (digest_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such digest")
    return render(
        request, "report_digest.html", conn, f, "reports",
        d=row, html=Markup(render_html(row["markdown"], conn)), cited=_cards(conn, row["cited_message_ids"]),
        removed=json.loads(row["removed_citations"] or "[]"),
    )


@router.get("/reports/investigation/{inv_id}", response_class=HTMLResponse)
def investigation_detail(request: Request, inv_id: int, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    row = conn.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such investigation")
    md = row["markdown"]
    failure = None
    if md is None:
        state = "running"
        if (f.now - from_iso(row["created_at"])).total_seconds() > queries.JOB_TIMEOUT_MINUTES * 60:
            state, failure = "failed", f"No result after {queries.JOB_TIMEOUT_MINUTES} minutes. Try asking again."
    elif md.startswith("Investigation failed"):
        state, failure = "failed", md
    else:
        state = "done"
    return render(
        request, "report_investigation.html", conn, f, "reports",
        inv=row, state=state, failure=failure,
        html=Markup(render_html(md, conn)) if state == "done" else None,
        cited=_cards(conn, row["cited_message_ids"]) if state == "done" else [],
        removed=json.loads(row["removed_citations"] or "[]"),
    )
