import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pulse.agents.base import BackendResult
from pulse.agents.triage import TriageStats
from pulse.db import connect
from pulse.modqueue import ModQueueStats
from pulse.pipeline import PipelineReport, format_report, run_pipeline
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats
from tests.fakes import FakeBackend, make_config, make_llm

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
