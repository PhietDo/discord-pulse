from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse import community
from pulse.web.cards import cards_by_ids
from pulse.web.charts import heatmap
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def _with_cards(conn, items: list[dict]) -> list[dict]:
    cards = {c["message_id"]: c for c in cards_by_ids(conn, [i["message_id"] for i in items])}
    return [{**i, "card": cards[i["message_id"]]} for i in items if i["message_id"] in cards]


@router.get("/community", response_class=HTMLResponse)
def community_view(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    tz = request.app.state.settings.config.timezone
    heat = community.activity_heatmap(conn, f.start, f.end, tz, f.channels)
    nc = community.newcomers(conn, f.start, f.end, f.channels, tz)
    return render(
        request, "community.html", conn, f, "community",
        tz=tz,
        heat_questions=heatmap(heat["questions"], "Questions needing a reply by weekday and hour"),
        heat_staff=heatmap(heat["staff"], "Staff messages by weekday and hour"),
        gaps=community.coverage_gaps(heat),
        nc=nc,
        unreplied=cards_by_ids(conn, nc["unreplied_ids"]),
        helpers=community.helpers(conn, f.start, f.end, f.channels),
        wanted=_with_cards(conn, community.most_wanted(conn, f.start, f.end, f.channels)),
        reacted=_with_cards(conn, community.top_reacted(conn, f.start, f.end, f.channels)),
    )
