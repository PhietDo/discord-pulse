import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from pulse import stats
from pulse.models import to_iso
from pulse.web import jobs
from pulse.web.deps import get_conn, get_filters
from pulse.web.filters import Filters
from pulse.web.views.reports import require_agents

router = APIRouter()
QUESTION_MAX = 500


@router.post("/investigations")
def start_investigation(
    request: Request,
    background: BackgroundTasks,
    question: str = Form(""),
    theme_id: str = Form(""),
    launch: str = Form(""),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    require_agents(request)
    question = question.strip()
    if not question or len(question) > QUESTION_MAX:
        raise HTTPException(status_code=400, detail=f"Ask a question of 1 to {QUESTION_MAX} characters.")
    context: dict = {}
    if theme_id:
        try:
            tid = int(theme_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="theme_id must be a number") from e
        if tid not in stats.theme_resolution(conn):
            raise HTTPException(status_code=404, detail=f"No theme {tid}")
        context["theme_id"] = tid
    if launch:
        if conn.execute("SELECT 1 FROM launches WHERE name = ?", (launch,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"No launch {launch!r}")
        context["launch"] = launch
    if f.channel:
        context["channel_id"] = f.channel
    context["days"] = f.days
    with conn:
        inv_id = int(conn.execute(
            "INSERT INTO investigations (question, context, created_at) VALUES (?, ?, ?)",
            (question, json.dumps(context), to_iso(f.now)),
        ).lastrowid)
    background.add_task(jobs.run_investigation_job, request.app.state.settings, inv_id, question, context)
    return RedirectResponse(f"/reports/investigation/{inv_id}{f.qs()}", status_code=303)
