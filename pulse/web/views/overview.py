from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    return render(request, "overview.html", conn, f, "overview")
