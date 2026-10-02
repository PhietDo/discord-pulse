"""Message citations in agent markdown: [[msg:<message_id>]]."""
from __future__ import annotations

import html
import logging
import re
import secrets
import sqlite3
from html.parser import HTMLParser

import markdown

from pulse.links import jump_link

log = logging.getLogger(__name__)

CITATION_RE = re.compile(r"\[\[msg:([^\]\s]+)\]\]")
# Near-misses models write: [[msg:<id>]] and [[msg: id ]].
_SLOPPY_RE = re.compile(r"\[\[msg:\s*<?\s*([^\]\s<>]+)\s*>?\s*\]\]")
# Any other [[msg:...]] token left after strict parsing.
_LENIENT_RE = re.compile(r"\[\[msg:([^\]]*)\]\]")


def cited_ids(markdown: str) -> list[str]:
    seen: list[str] = []
    for mid in CITATION_RE.findall(markdown):
        if mid not in seen:
            seen.append(mid)
    return seen


def strip_unknown(markdown: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove citations to messages the agent was not shown, and malformed citation tokens.

    Near-miss forms are normalized to [[msg:<id>]] first. Returns the cleaned markdown and
    the removed ids (or malformed token contents), deduped in order.
    """
    removed: list[str] = []

    def drop(value: str) -> str:
        if value not in removed:
            removed.append(value)
        return ""

    def replace(match: re.Match) -> str:
        mid = match.group(1)
        return match.group(0) if mid in allowed else drop(mid)

    def sweep(match: re.Match) -> str:
        if CITATION_RE.fullmatch(match.group(0)):
            return match.group(0)
        return drop(match.group(1).strip())

    markdown = _SLOPPY_RE.sub(r"[[msg:\1]]", markdown)
    markdown = CITATION_RE.sub(replace, markdown)
    return _LENIENT_RE.sub(sweep, markdown), removed


def render_text(markdown: str, conn: sqlite3.Connection) -> str:
    """Plain-text rendering for the CLI: each citation becomes the author and a jump link."""

    def replace(match: re.Match) -> str:
        row = conn.execute(
            "SELECT guild_id, channel_id, author_name FROM messages WHERE id = ?", (match.group(1),)
        ).fetchone()
        if row is None:
            return "[missing message]"
        return f"({row['author_name']}, {jump_link(row['guild_id'], row['channel_id'], match.group(1))})"

    return CITATION_RE.sub(replace, markdown)


_SAFE_SCHEMES = ("http:", "https:", "mailto:")
# Browsers ignore ASCII whitespace and control characters inside a URL scheme.
_URL_JUNK_RE = re.compile(r"[\x00-\x20\x7f]+")


def safe_href(value: str) -> bool:
    """True for http(s), mailto, site-relative (/...) and fragment (#...) links.
    Protocol-relative //host and /\\host are rejected: browsers treat them as another site."""
    url = _URL_JUNK_RE.sub("", value).lower()
    if url.startswith(("//", "/\\")):
        return False
    return url.startswith(_SAFE_SCHEMES) or url.startswith(("/", "#"))


class _Sanitizer(HTMLParser):
    """Rebuilds markdown output: drops <img>, drops unsafe hrefs, swaps citation tokens
    in text for their anchors and removes them from attribute values."""

    def __init__(self, anchors: dict[str, str], token_re: re.Pattern):
        super().__init__(convert_charrefs=True)
        self.anchors, self.token_re, self.out = anchors, token_re, []

    def _tag(self, tag: str, attrs, close: str) -> None:
        if tag == "img":
            return
        kept = []
        for name, value in attrs:
            value = self.token_re.sub("", value or "")
            if name in ("href", "src") and not safe_href(value):
                continue
            kept.append(f' {name}="{html.escape(value)}"')
        self.out.append(f"<{tag}{''.join(kept)}{close}>")

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs, "")

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs, " /")

    def handle_endtag(self, tag):
        if tag != "img":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        pos = 0
        for m in self.token_re.finditer(data):
            self.out.append(html.escape(data[pos:m.start()], quote=False))
            self.out.append(self.anchors.get(m.group(0), ""))
            pos = m.end()
        self.out.append(html.escape(data[pos:], quote=False))


def render_html(markdown_text: str, conn: sqlite3.Connection) -> str:
    """HTML for agent reports. Raw HTML from the model is escaped first; each [[msg:id]]
    becomes "@author" linked to the message in Discord (excerpt on hover); unknown ids
    render as "[missing message]" and are logged.

    Citations are swapped for inert tokens before markdown runs and their anchors are put
    back afterwards, so message content and author names never pass through markdown.
    The markdown output is then sanitized: no <img>, only http(s)/mailto/relative links."""
    prefix = f"pulsecite{secrets.token_hex(8)}n"
    token_re = re.compile(re.escape(prefix) + r"\d+e")
    anchors: dict[str, str] = {}

    def anchor(mid: str) -> str:
        row = conn.execute(
            "SELECT guild_id, channel_id, author_name, content FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            log.warning("citation to unknown message %s", mid)
            return "[missing message]"
        link = jump_link(row["guild_id"], row["channel_id"], mid)
        excerpt = row["content"][:140]
        return (
            f'<a class="cite" href="{html.escape(link)}" title="{html.escape(excerpt)}"'
            f' target="_blank" rel="noopener">@{html.escape(row["author_name"])}</a>'
        )

    def to_token(match: re.Match) -> str:
        token = f"{prefix}{len(anchors)}e"
        anchors[token] = anchor(match.group(1))
        return token

    tokenized = CITATION_RE.sub(to_token, markdown_text)
    rendered = markdown.markdown(html.escape(tokenized, quote=False), extensions=["sane_lists"])
    sanitizer = _Sanitizer(anchors, token_re)
    sanitizer.feed(rendered)
    sanitizer.close()
    return "".join(sanitizer.out)
