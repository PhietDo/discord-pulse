from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse.modqueue import close_item
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()
REASONS = ("frustrated", "unanswered")


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, reason: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    reason = reason if reason in REASONS else None
    summary = queries.open_queue_summary(conn, f.channels)
    return render(
        request, "queue.html", conn, f, "queue", items=queries.queue_cards(conn, f, reason=reason), reason=reason,
        open_total=summary[reason] if reason else summary["total"],
    )


@router.post("/queue/{queue_id}/close")
def close(
    request: Request,
    queue_id: int,
    action: str = Form(...),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    if action not in ("handled", "dismissed"):
        raise HTTPException(status_code=400, detail="action must be handled or dismissed")
    row = conn.execute("SELECT message_id FROM mod_queue WHERE id = ?", (queue_id,)).fetchone()
    if row is None or not close_item(conn, queue_id, action, f.now):
        raise HTTPException(status_code=404, detail="This item is already closed or does not exist.")
    if request.headers.get("HX-Request"):
        card = cards_by_ids(conn, [row["message_id"]])[0]
        return request.app.state.templates.TemplateResponse(
            request, "_queue_closed.html", {"m": card, "action": action}
        )
    return RedirectResponse(f"/queue{f.qs()}", status_code=303)
