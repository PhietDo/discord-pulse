from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from pulse import stats
from pulse.citations import render_html
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sentiment_chart
from pulse.web.context import open_queue_count
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    now = f.now
    week_start = now - timedelta(days=7)
    week = stats.period_summary(conn, week_start, now, channels=f.channels)
    prev = stats.period_summary(conn, week_start - timedelta(days=7), week_start, channels=f.channels)
    window = stats.period_summary(conn, f.start, f.end, channels=f.channels)
    attention = queries.queue_cards(conn, f, limit=3)
    open_items = queries.open_queue_summary(conn, f.channels)
    praise = stats.sample_messages(
        conn, f.start, f.end, kinds=("praise",), most_negative=False, limit=3, channels=f.channels
    )
    digest = queries.latest_digest(conn)
    return render(
        request, "overview.html", conn, f, "overview",
        week=week,
        prev=prev,
        vol_delta=queries.pct_change(week["messages"], prev["messages"]),
        replies=stats.reply_stats(conn, week_start, now, now, channels=f.channels),
        chart=sentiment_chart(
            stats.sentiment_series(conn, f.start, f.end, channels=f.channels),
            queries.launch_markers(conn, f.start, f.end),
        ),
        rising=queries.theme_rows(conn, f, limit=5),
        mix=queries.kind_mix(window["by_kind"]),
        breakdown=stats.channel_breakdown(conn, f.start, f.end, now, channels=f.channels),
        queue_total=open_queue_count(conn, f.channels),
        frustrated=open_items["frustrated"],
        oldest=open_items["oldest"],
        attention=attention,
        praise=cards_by_ids(conn, [m["message_id"] for m in praise]),
        digest=digest,
        digest_html=Markup(render_html(digest["markdown"], conn)) if digest else None,
    )
