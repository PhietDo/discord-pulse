from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.models import KINDS
from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()
MAX_PAGE = 10000


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.get("/messages", response_class=HTMLResponse)
def messages(
    request: Request,
    q: str | None = None,
    author_id: str | None = None,
    theme: str | None = None,
    kind: str | None = None,
    mood: str | None = None,
    page: str | None = None,
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    page_no = min(max(_int(page) or 1, 1), MAX_PAGE)
    theme_id = _int(theme)
    kind = kind if kind in KINDS else None
    mood = mood if mood in queries.MOODS else None
    cards, total = queries.search_cards(
        conn, f, text=(q or "").strip() or None, author_id=author_id or None,
        theme_id=theme_id, kind=kind, mood=mood, page=page_no,
    )
    author = None
    if author_id:
        row = conn.execute("SELECT author_name FROM messages WHERE author_id = ? LIMIT 1", (author_id,)).fetchone()
        author = row["author_name"] if row else author_id
    theme_name = None
    if theme_id is not None:
        row = conn.execute("SELECT name FROM themes WHERE id = ?", (theme_id,)).fetchone()
        theme_name = row["name"] if row else None
    keep = {"q": q or "", "author_id": author_id or "", "theme": theme_id or "", "kind": kind or "", "mood": mood or ""}
    return render(
        request, "messages.html", conn, f, "messages",
        cards=cards, total=total, page=page_no, per_page=queries.PER_PAGE, keep=keep,
        author=author, theme_name=theme_name, kinds=KINDS,
    )
