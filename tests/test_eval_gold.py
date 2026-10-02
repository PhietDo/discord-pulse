import json

import pytest

from pulse.eval import GoldRow, Prediction, load_gold, macro_f1, score


def line(mid, *, labels=..., **over):
    raw = {
        "message_id": mid, "channel_id": "100", "channel_name": "help", "author_id": "u1",
        "author_name": "alice", "created_at": "2026-09-28T12:00:00Z", "content": f"message {mid}",
        "labels": {"sentiment": -1, "kind": "bug", "needs_reply": True} if labels is ... else labels,
    }
    raw.update(over)
    return json.dumps(raw)


def write(tmp_path, lines):
    path = tmp_path / "gold.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_gold_parses_rows_and_reports_bad_lines(tmp_path):
    path = write(tmp_path, [
        line("1"),
        line("2", labels={"sentiment": None, "kind": None, "needs_reply": None}),
        "{not json",
        line("4", labels={"sentiment": 0, "kind": "complaint", "needs_reply": False}),
        line("5", labels={"sentiment": True, "kind": "bug", "needs_reply": True}),
        "",
        line("1"),
        line("8", content=""),
        line("9", labels={"sentiment": 0, "kind": "bug"}),
        line("10", is_team=True, thread_id="300", parent_channel_id="100", reply_to_id="1"),
    ])
    gold = load_gold(path)
    assert [r.message.id for r in gold.rows] == ["1", "10"]
    assert gold.unlabeled == 1
    assert [e.split(":")[0] for e in gold.errors] == ["line 3", "line 4", "line 5", "line 7", "line 8", "line 9"]
    assert "unknown kind 'complaint'" in gold.errors[1]
    assert "duplicate message_id 1" in gold.errors[3]
    ten = gold.rows[1]
    assert ten.is_team and ten.message.thread_id == "300" and ten.message.parent_channel_id == "100"
    assert ten.message.reply_to_id == "1" and ten.message.guild_id == "0" and ten.message.source == "eval"
    assert ten.message.created_at.tzinfo is not None


def test_load_gold_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_gold(tmp_path / "nope.jsonl")


def test_shipped_example_gold_set_loads_cleanly():
    gold = load_gold("eval/gold.example.jsonl")
    assert gold.errors == [] and gold.unlabeled == 0
    assert len(gold.rows) == 141
    assert sum(r.needs_reply for r in gold.rows) == 67


def _gold(mid, sentiment, kind, needs_reply):
    from pulse.models import Message
    from tests.fakes import T0

    m = Message(id=mid, guild_id="0", channel_id="100", author_id="u1", author_name="a", content="x", created_at=T0)
    return GoldRow(m, False, sentiment, kind, needs_reply)


def test_score_counts_missing_predictions_as_wrong():
    gold = [_gold("1", -1, "bug", True), _gold("2", 0, "question", True),
            _gold("3", 1, "praise", False), _gold("4", 0, "other", False)]
    preds = {"1": Prediction(-2, "bug", True), "2": Prediction(0, "docs", False), "3": Prediction(1, "praise", True)}
    s = score(gold, preds)
    assert s.n == 4 and s.missing == 1
    assert s.sentiment_exact == 0.5 and s.sentiment_within_1 == 0.75
    assert s.needs_reply_agreement == 0.25
    assert s.needs_reply_recall == 0.5 and s.needs_reply_precision == 0.5
    assert s.kind_macro_f1 == pytest.approx(0.5)


def test_score_perfect_and_empty_cases():
    gold = [_gold("1", 1, "praise", False)]
    s = score(gold, {"1": Prediction(1, "praise", False)})
    assert s.sentiment_exact == 1.0 and s.kind_macro_f1 == 1.0 and s.needs_reply_agreement == 1.0
    assert s.needs_reply_recall is None and s.needs_reply_precision is None
    with pytest.raises(ValueError):
        score([], {})


def test_macro_f1_averages_gold_kinds_only():
    assert macro_f1([("bug", "bug"), ("bug", "docs")]) == pytest.approx(2 / 3)
