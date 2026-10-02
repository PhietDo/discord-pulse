from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse import stats
from pulse.agents.digest import build_launch_input
from pulse.models import from_iso
from pulse.web import jobs
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sentiment_chart
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters
from pulse.web.views.reports import require_agents

router = APIRouter()


def _pick(rows, name: str | None, today: str):
    for r in rows:
        if r["name"] == name:
            return r
    for r in rows:
        if r["date"] <= today:
            return r
    return rows[0]


@router.get("/launch", response_class=HTMLResponse)
def launch(request: Request, name: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    rows = conn.execute("SELECT * FROM launches ORDER BY date DESC, name").fetchall()
    if not rows:
        return render(request, "launch.html", conn, f, "launch", launches=[], sel=None)
    sel = _pick(rows, name, f.now.date().isoformat())
    try:
        data = build_launch_input(conn, sel, f.now)
    except LookupError:
        data = None
    chart, cards = None, []
    if data:
        start, end = from_iso(data["before"]["start"]), from_iso(data["after"]["end"])
        chart = sentiment_chart(stats.sentiment_series(conn, start, end), [(sel["date"], sel["name"])])
        cards = cards_by_ids(conn, [m["message_id"] for m in data["messages"][:12]])
    latest = conn.execute(
        "SELECT id, created_at FROM digests WHERE launch_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (sel["id"],)
    ).fetchone()
    return render(
        request, "launch.html", conn, f, "launch",
        launches=rows, sel=sel, data=data, chart=chart, cards=cards, latest=latest,
    )


@router.post("/launch/{launch_id}/digest")
def write_launch_digest(
    request: Request, launch_id: int, background: BackgroundTasks, conn=Depends(get_conn), f: Filters = Depends(get_filters)
):
    require_agents(request)
    row = conn.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such launch")
    if row["date"] > f.now.date().isoformat():
        raise HTTPException(status_code=400, detail="This launch hasn't happened yet")
    slots = request.app.state.job_slots
    slots.take_digest()
    background.add_task(slots.wrap("digests", jobs.run_digest_job), request.app.state.settings, row["name"])
    return RedirectResponse(f"/reports{f.qs(pending=int(f.now.timestamp()))}", status_code=303)
