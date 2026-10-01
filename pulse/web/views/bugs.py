from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/bugs", response_class=HTMLResponse)
def bugs(request: Request, open: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    open_only = open == "1"
    return render(
        request, "bugs.html", conn, f, "bugs",
        groups=queries.bug_groups(conn, f, open_only=open_only), open_only=open_only,
    )
