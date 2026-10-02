import json

import pytest

from pulse.agents.base import BackendResult
from pulse.config import ConfigError, ModelRef
from pulse.eval import GATE, gate_lines, load_gold, main, run_eval, write_sample
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import FIXED_NOW, FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, msg

ROWS = [
    ("g1", "it is broken", -1, "bug", True, "u1", False),
    ("g2", "love it", 1, "praise", False, "u2", False),
    ("g3", "broken again?", -2, "bug", True, "u3", False),
    ("g4", "fixed in 2.1", 0, "other", False, "t1", True),
]


def gold_path(tmp_path):
    path = tmp_path / "gold.jsonl"
    lines = []
    for i, (mid, content, s, k, nr, author, team) in enumerate(ROWS):
        lines.append(json.dumps({
            "message_id": mid, "channel_id": "100", "channel_name": "help", "author_id": author,
            "author_name": author, "is_team": team, "created_at": f"2026-09-28T12:0{i}:00Z",
            "content": content, "labels": {"sentiment": s, "kind": k, "needs_reply": nr},
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


def llm_handler(user):
    items = json.loads(user)["messages"]
    return BackendResult({"results": [
        {"message_id": m["message_id"], "sentiment": -1 if "broken" in m["content"] else 1, "confidence": 0.9,
         "kind": "bug" if "broken" in m["content"] else "praise", "topics": [],
         "needs_reply": "broken" in m["content"]}
        for m in items
    ]}, 100, 20)


def jev_handler(state):
    if "broken" in state["content"]:
        return jev_result(p=0.9, kind="bug", sentiment=-1)
    return jev_result(p=0.1, kind="praise", sentiment=1)


def run(tmp_path, specs, *, backend=None, classifier=None, max_usd=1.0, **config_over):
    gold = load_gold(gold_path(tmp_path))
    config = make_config(db_path=tmp_path / "pulse.db", **config_over)
    backend = backend or FakeBackend(handler=llm_handler)
    classifier = classifier or FakeClassifier(handler=jev_handler)
    results = run_eval(
        gold, config, specs, backends_for=lambda cfg: {"anthropic": backend},
        classifier_for=lambda: classifier, max_usd=max_usd, now=lambda: FIXED_NOW,
    )
    return gold, results, backend, classifier


def test_llm_eval_scores_and_costs_without_touching_the_real_db(tmp_path):
    _, [r], backend, _ = run(tmp_path, ["anthropic:m-triage"])
    s = r.scores
    assert s.sentiment_exact == 0.5 and s.sentiment_within_1 == 1.0
    assert s.needs_reply_agreement == 1.0 and s.needs_reply_recall == 1.0 and s.missing == 0
    assert s.kind_macro_f1 == pytest.approx((1 + 0 + 2 / 3) / 3)
    assert r.cost_usd == pytest.approx(0.0002)
    assert [c["model"] for c in backend.calls] == ["m-triage"]
    assert not (tmp_path / "pulse.db").exists()


def test_jev_eval_overrides_staff_and_passes_the_gate(tmp_path):
    _, [r], backend, classifier = run(tmp_path, ["jev:jev-latest"])
    assert backend.calls == [] and len(classifier.calls) == 4
    assert r.scores.sentiment_exact == 0.75 and r.scores.needs_reply_agreement == 1.0
    assert r.cost_usd == pytest.approx(0.00008)
    assert gate_lines([r]) == ["jev:jev-latest: needs-reply agreement 100% meets the 90% gate."]


def test_jev_below_gate_recommends_turning_it_off(tmp_path):
    bad = FakeClassifier(handler=lambda s: jev_result(p=0.1, kind="praise", sentiment=1))
    _, [r], _, _ = run(tmp_path, ["jev:jev-latest"], classifier=bad)
    assert r.scores.needs_reply_agreement == 0.5 < GATE
    assert gate_lines([r]) == [
        "jev:jev-latest: needs-reply agreement 50% is below the 90% gate. "
        "Recommend setting [classifier] enabled = false."
    ]


def test_jev_threshold_comes_from_config(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    config = make_config(classifier=classifier_config(needs_reply_threshold=0.95))
    [r] = run_eval(gold, config, ["jev:jev-latest"], backends_for=lambda c: {},
                   classifier_for=lambda: FakeClassifier(handler=jev_handler), max_usd=1.0, now=lambda: FIXED_NOW)
    assert r.scores.needs_reply_recall == 0.0  # p = 0.9 is below the 0.95 threshold


def test_hybrid_runs_two_stage_triage(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    backend, classifier = FakeBackend(handler=llm_handler), FakeClassifier(handler=jev_handler)
    config = make_config(classifier=classifier_config())
    [r] = run_eval(gold, config, ["hybrid"], backends_for=lambda c: {"anthropic": backend},
                   classifier_for=lambda: classifier, max_usd=1.0, now=lambda: FIXED_NOW)
    assert len(classifier.calls) == 4 and len(backend.calls) == 1  # the three non-staff messages escalate in one batch (negative or praise)
    assert r.scores.missing == 0


def test_bad_specs_raise_config_error(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    for spec, config in [
        ("hybrid", make_config()),
        ("nope:model", make_config()),
        ("openai:gpt-x", make_config()),
    ]:
        with pytest.raises(ConfigError):
            run_eval(gold, config, [spec], backends_for=lambda c: {}, classifier_for=FakeClassifier, max_usd=1.0)


def test_budget_cap_leaves_messages_missing(tmp_path):
    _, [r], _, _ = run(tmp_path, ["anthropic:m-triage"], max_usd=0.0)
    assert r.scores.missing == 4 and r.cost_usd == 0.0
    assert any("skipped_budget" in n for n in r.notes)


TOML = """
[server]
guild_id = "900"
team_member_ids = ["t1"]
[models]
triage = "anthropic:m-triage"
theme = "anthropic:m-theme"
digest = "anthropic:m-digest"
investigate = "anthropic:m-investigate"
[pricing."anthropic:m-triage"]
input = 1.0
output = 5.0
[pricing."anthropic:m-theme"]
input = 1.0
output = 5.0
[pricing."anthropic:m-digest"]
input = 1.0
output = 5.0
[pricing."anthropic:m-investigate"]
input = 1.0
output = 5.0
"""


def cli(tmp_path, argv, env=None, capsys=None):
    (tmp_path / "pulse.toml").write_text(TOML)
    backend = FakeBackend(handler=llm_handler)
    return main(
        ["--config", str(tmp_path / "pulse.toml"), *argv],
        env={"ANTHROPIC_API_KEY": "x"} if env is None else env,
        backends_for=lambda cfg: {"anthropic": backend},
        classifier_for=lambda: FakeClassifier(handler=jev_handler),
    )


def test_cli_prints_a_table_and_writes_json(tmp_path, capsys):
    out_json = tmp_path / "r.json"
    code = cli(tmp_path, ["--gold", str(gold_path(tmp_path)), "--json", str(out_json)])
    out = capsys.readouterr().out
    assert code == 0
    assert "4 labelled messages" in out and "reply recall" in out and "anthropic:m-triage" in out
    data = json.loads(out_json.read_text())
    assert data[0]["model"] == "anthropic:m-triage" and data[0]["needs_reply_agreement"] == 1.0


def test_cli_missing_key_and_missing_gold_exit_2(tmp_path, capsys):
    assert cli(tmp_path, ["--gold", str(gold_path(tmp_path))], env={}) == 2
    assert "ANTHROPIC_API_KEY must be set" in capsys.readouterr().err
    assert cli(tmp_path, ["--gold", str(tmp_path / "none.jsonl")]) == 2
    assert "--sample" in capsys.readouterr().err


def test_sample_writes_unlabelled_rows_and_refuses_to_overwrite(tmp_path, capsys):
    conn = connect(tmp_path / "pulse.db")
    upsert_messages(conn, [msg(f"m{i}", f"text {i}", minutes=i) for i in range(5)] + [msg("b", "bot", is_bot=True)],
                    frozenset({"t1"}))
    out = tmp_path / "label-me.jsonl"
    assert write_sample(conn, out, 3) == 3
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert all(r["labels"] == {"sentiment": None, "kind": None, "needs_reply": None} for r in rows)
    assert all(r["message_id"] != "b" for r in rows)
    assert load_gold(out).unlabeled == 3
    with pytest.raises(FileExistsError):
        write_sample(conn, out, 3)
    conn.close()
    assert cli(tmp_path, ["--sample", "2", "--out", str(tmp_path / "new.jsonl")]) == 0
    assert "fill in each labels object" in capsys.readouterr().out
