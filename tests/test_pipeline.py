import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pulse.agents.base import BackendResult
from pulse.agents.digest import DigestResult
from pulse.agents.investigate import InvestigationResult
from pulse.agents.theme import ThemeStats
from pulse.agents.triage import TriageStats
from pulse.db import connect
from pulse.modqueue import ModQueueStats
from pulse.pipeline import PipelineReport, format_report, format_triage, ingest, run_pipeline, build_llm, format_digest, format_investigation, format_themes
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats
from tests.fakes import FakeBackend, make_config, make_llm, classifier_config

FIXTURES = Path(__file__).parent / "fixtures"


def label(user):
    results = []
    for m in json.loads(user)["messages"]:
        text = m["content"]
        sentiment = -2 if "fails" in text else -1 if "broken" in text else 0
        results.append({
            "message_id": m["message_id"], "sentiment": sentiment, "confidence": 0.9,
            "kind": "bug" if sentiment < 0 else "other", "topics": ["install"] if sentiment < 0 else [],
            "needs_reply": "?" in text,
        })
    return BackendResult({"results": results}, 100, 20)


def test_end_to_end_import_triage_and_queue(tmp_path):
    for name in ("dce_channel.json", "dce_thread.json", "messages.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path)
    llm = make_llm(conn, config, FakeBackend(handler=label))

    report = run_pipeline(
        conn, config, llm, source=FileSource(tmp_path),
        now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert report.ingest.inserted == 6
    assert len(report.ingest_errors) == 1  # messages.csv line 3
    assert report.triage.triaged == 5  # bot message 1004 skipped
    assert report.modqueue.opened == 2
    queue = {r["message_id"]: r["reason"] for r in conn.execute("SELECT * FROM mod_queue")}
    assert queue == {"1001": "frustrated", "3001": "unanswered"}


def test_format_report_mentions_budget():
    report = PipelineReport(
        ingest=UpsertStats(inserted=3), ingest_errors=["a.csv:2: missing created_at"],
        triage=TriageStats(triaged=1, failed_batches=1, skipped_budget_batches=2),
        modqueue=ModQueueStats(opened=1),
    )
    text = format_report(report)
    assert "inserted 3" in text
    assert "a.csv:2" in text
    assert "failed batches 1" in text
    assert "daily budget cap reached" in text
    assert "opened 1" in text


def test_ingest_filters_to_configured_channels_and_their_threads(tmp_path):
    for name in ("dce_channel.json", "dce_thread.json"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    other = json.loads((FIXTURES / "dce_channel.json").read_text())
    other["channel"]["id"] = "555"
    for i, m in enumerate(other["messages"]):
        m["id"] = f"555{i}"
    (tmp_path / "dce_channel_other.json").write_text(json.dumps(other))

    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path, channel_ids=("100",))
    stats, errors = ingest(conn, config, FileSource(tmp_path))

    ids = {r["id"] for r in conn.execute("SELECT id FROM messages")}
    assert ids == {"1001", "1002", "1004", "3001"}
    assert not any(i.startswith("555") for i in ids)

    src = FileSource(tmp_path)
    by_id = {m.id: m for m in src.fetch()}
    assert by_id["3001"].parent_channel_id == "100"


from datetime import timedelta

from pulse.modqueue import list_open, refresh_mod_queue
from pulse.pipeline import format_queue
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage


def test_format_queue_shows_author_channel_age_and_jump_link():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("q1", "how do I rotate keys?")], frozenset({"t1"}))
    set_triage(conn, "q1", needs_reply=True)
    now = T0 + timedelta(hours=24)
    refresh_mod_queue(conn, make_config(), now)
    text = format_queue(list_open(conn), now)
    assert "unanswered" in text
    assert "alice in #help" in text
    assert "24h ago" in text
    assert "how do I rotate keys?" in text
    assert "https://discord.com/channels/900/100/q1" in text


def test_format_queue_when_empty():
    assert format_queue([], T0) == "mod queue: nothing open"


def test_build_llm_attaches_jev_only_when_enabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    assert build_llm(connect(":memory:"), make_config(classifier=classifier_config())).has_classifier is True
    assert build_llm(connect(":memory:"), make_config(classifier=classifier_config(enabled=False))).has_classifier is False
    assert build_llm(connect(":memory:"), make_config()).has_classifier is False


def test_format_triage_reports_jev_split():
    text = format_triage(TriageStats(triaged=10, jev_labeled=7, escalated=3, classifier_failed=1))
    assert "jev: labeled 7, escalated to LLM 3, classifier failures 1" in text


def test_format_triage_without_jev_has_no_jev_line():
    assert "jev" not in format_triage(TriageStats(triaged=4))


def test_format_triage_reports_kept_llm_labels():
    text = format_triage(TriageStats(triaged=5, jev_labeled=3, escalated=1, kept_llm=2))
    assert "jev: labeled 3, escalated to LLM 1, classifier failures 0, kept existing LLM labels 2" in text


def test_format_triage_shows_left_untriaged_instead_of_skipped_batches():
    text = format_triage(TriageStats(triaged=2, skipped_budget_batches=1, left_untriaged=5))
    assert "daily budget cap reached: 5 messages left untriaged for the next run" in text
    assert "batches skipped" not in text


def label_or_theme(user):
    data = json.loads(user)
    if "themes" in data:
        ids = [m["message_id"] for m in data["messages"]]
        return BackendResult({"assignments": [], "merges": [], "renames": [],
                              "new_themes": [{"name": "Install", "description": "d", "message_ids": ids}]}, 10, 10)
    return label(user)


def test_pipeline_themes_triaged_messages(tmp_path):
    for name in ("dce_channel.json", "dce_thread.json", "messages.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path)
    report = run_pipeline(conn, config, make_llm(conn, config, FakeBackend(handler=label_or_theme)),
                          source=FileSource(tmp_path), now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))
    assert report.themes.created == 1
    assert report.themes.assigned == report.themes.considered > 0
    assert "themes:" in format_report(report)


def test_format_themes_lists_rejections_and_budget():
    text = format_themes(ThemeStats(considered=5, jev_assigned=2, llm_batches=1, created=1, assigned=3,
                                    rejected=["merge 1->2: both themes must be active and different"],
                                    skipped_budget=True))
    assert "considered 5" in text and "jev assigned 2" in text and "new themes 1" in text
    assert "rejected: merge 1->2" in text
    assert "daily budget cap reached" in text


def test_format_digest_and_investigation_render_citations():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "hi", author_name="alice")], frozenset())
    digest = DigestResult(7, "weekly", "2026-09-21T00:00:00.000000Z", "2026-09-28T00:00:00.000000Z",
                          "Good [[msg:m1]].", ["m1"], ["ghost"])
    text = format_digest(digest, conn)
    assert text.startswith("digest #7 (weekly")
    assert "(alice, https://discord.com/channels/900/100/m1)" in text
    assert "removed 1 citation" in text
    inv = InvestigationResult(3, "Because [[msg:m1]].", ["m1"], [], 2)
    assert "investigation #3 (2 tool calls)" in format_investigation(inv, conn)


def test_theme_failure_does_not_block_the_mod_queue(tmp_path, monkeypatch):
    for name in ("dce_channel.json", "dce_thread.json", "messages.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("theme bug")

    monkeypatch.setattr("pulse.pipeline.run_themes", boom)
    with pytest.raises(RuntimeError):
        run_pipeline(conn, config, make_llm(conn, config, FakeBackend(handler=label)),
                     source=FileSource(tmp_path), now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))
    assert conn.execute("SELECT COUNT(*) FROM mod_queue").fetchone()[0] == 2


def test_format_report_lists_mod_queue_before_themes():
    report = PipelineReport(
        ingest=UpsertStats(), ingest_errors=[], triage=TriageStats(), modqueue=ModQueueStats(opened=1),
        themes=ThemeStats(considered=2),
    )
    text = format_report(report)
    assert text.index("mod queue:") < text.index("themes:")
