from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, status: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    status = status if status in queries.RUN_STATUSES else None
    return render(
        request, "runs.html", conn, f, "runs",
        days=queries.daily_costs(conn, f.now), runs=queries.recent_runs(conn, status=status), status=status,
    )
