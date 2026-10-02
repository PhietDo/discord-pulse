from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse import stats
from pulse.issues import TrackerError, send_issue, tracker_status
from pulse.theme_status import LABELS, STATUSES, set_status, shipped_comparison, status_for
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def _selected(conn, theme: str | None, rows: list[dict]) -> dict | None:
    resolved = stats.theme_resolution(conn)
    try:
        wanted = int(theme) if theme else None
    except ValueError:
        wanted = None
    if wanted is not None and wanted in resolved:
        root = resolved[wanted]
        for row in rows:
            if row["id"] == root:
                return row
        t = conn.execute("SELECT id, name, description FROM themes WHERE id = ?", (root,)).fetchone()
        st = status_for(conn, root)
        return {
            "id": t["id"], "name": t["name"], "description": t["description"], "volume": 0,
            "kinds": {}, "status": st["status"], "status_label": st["label"], "note": st["note"],
        }
    return rows[0] if rows else None


@router.get("/pain", response_class=HTMLResponse)
def pain(
    request: Request,
    theme: str | None = None,
    saved: str | None = None,
    issued: str | None = None,
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    rows = queries.theme_rows(conn, f, limit=50)
    sel = _selected(conn, theme, rows)
    evidence, comparison = [], None
    settings = request.app.state.settings
    trackers = tracker_status(conn, settings.config, sel["id"], env=settings.env) if sel is not None else []
    if sel is not None:
        sample = stats.sample_messages(conn, f.start, f.end, theme_id=sel["id"], limit=8, channels=f.channels)
        evidence = cards_by_ids(conn, [m["message_id"] for m in sample])
        comparison = shipped_comparison(conn, sel["id"], f.now)
    return render(
        request, "pain.html", conn, f, "pain",
        rows=rows, sel=sel, evidence=evidence, comparison=comparison,
        statuses=[(s, LABELS[s]) for s in STATUSES], saved=bool(saved),
        trackers=trackers, issued={"created": "Issue created.", "exists": "Already sent; here is the issue."}.get(issued),
    )


@router.post("/pain/{theme_id}/status")
def save_status(
    request: Request,
    theme_id: int,
    status: str = Form(...),
    note: str = Form(""),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    try:
        set_status(conn, theme_id, status, note, f.now)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    root = stats.theme_resolution(conn)[theme_id]
    return RedirectResponse(f"/pain{f.qs(theme=root, saved=1)}", status_code=303)


@router.post("/pain/{theme_id}/issue")
def send_to_tracker(
    request: Request,
    theme_id: int,
    tracker: str = Form(...),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    settings = request.app.state.settings
    client = settings.http_client_factory() if settings.http_client_factory else None
    try:
        result = send_issue(conn, settings.config, theme_id, tracker, f.now, client=client, env=settings.env)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except TrackerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    finally:
        if client is not None:
            client.close()
    root = stats.theme_resolution(conn)[theme_id]
    issued = "created" if result["created"] else "exists"
    return RedirectResponse(f"/pain{f.qs(theme=root, issued=issued)}", status_code=303)
