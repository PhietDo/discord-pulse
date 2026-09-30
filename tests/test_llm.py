import pytest

from pulse.agents.base import (
    BackendResult, BudgetExceeded, LLMError, OutputInvalid, ProviderError, TransientError,
)
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import FIXED_NOW, FakeBackend, make_config, make_llm

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


def ok(answer="yes", inp=1000, out=200, cache=0, cost=None):
    return BackendResult({"answer": answer}, inp, out, cache, cost)


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()


def call(llm, **kw):
    return llm.complete("triage", "sys", "user", SCHEMA, "answer_result", **kw)


def test_success_returns_data_and_logs_cost():
    conn = connect(":memory:")
    backend = FakeBackend([ok(inp=1000, out=200, cache=10_000)])
    resp = call(make_llm(conn, make_config(), backend))
    assert resp.data == {"answer": "yes"}
    [run] = runs(conn)
    assert resp.run_id == run["id"]
    assert (run["agent"], run["model"], run["status"]) == ("triage", "anthropic:m-triage", "ok")
    assert (run["input_tokens"], run["output_tokens"], run["cache_read_tokens"]) == (1000, 200, 10_000)
    # 1000*1.0 + 200*5.0 + 10000*0.1 per million
    assert run["cost_usd"] == pytest.approx(0.003)
    assert backend.calls[0]["model"] == "m-triage"


def test_reported_cost_overrides_pricing():
    conn = connect(":memory:")
    call(make_llm(conn, make_config(), FakeBackend([ok(cost=0.5)])))
    assert runs(conn)[0]["cost_usd"] == pytest.approx(0.5)


def test_schema_invalid_output_is_retried_once():
    conn = connect(":memory:")
    bad = BackendResult({"wrong": 1}, 100, 10)
    backend = FakeBackend([bad, ok()])
    assert call(make_llm(conn, make_config(), backend)).data == {"answer": "yes"}
    assert len(backend.calls) == 2
    [run] = runs(conn)
    assert run["input_tokens"] == 1100  # both attempts are billed


def test_invalid_twice_fails_and_records_error():
    conn = connect(":memory:")
    bad = BackendResult({"wrong": 1}, 100, 10)
    with pytest.raises(LLMError):
        call(make_llm(conn, make_config(), FakeBackend([bad, bad])))
    [run] = runs(conn)
    assert run["status"] == "failed"
    assert "invalid output" in run["error"]


def test_validate_callback_failure_triggers_retry():
    conn = connect(":memory:")
    backend = FakeBackend([ok("no"), ok("yes")])

    def must_be_yes(data):
        if data["answer"] != "yes":
            raise ValueError("answer must be yes")

    assert call(make_llm(conn, make_config(), backend), validate=must_be_yes).data == {"answer": "yes"}
    assert len(backend.calls) == 2


def test_validate_non_value_error_propagates():
    conn = connect(":memory:")
    backend = FakeBackend([ok()])

    def bad_validator(data):
        raise KeyError("bug in validator")

    with pytest.raises(KeyError, match="bug in validator"):
        call(make_llm(conn, make_config(), backend), validate=bad_validator)


def test_output_invalid_tokens_are_billed():
    conn = connect(":memory:")
    backend = FakeBackend([OutputInvalid("no tool block", 500, 50), ok(inp=1000, out=200)])
    call(make_llm(conn, make_config(), backend))
    assert runs(conn)[0]["input_tokens"] == 1500


def test_transient_errors_back_off_then_succeed():
    conn = connect(":memory:")
    sleeps = []
    backend = FakeBackend([TransientError("429"), TransientError("503"), ok()])
    call(make_llm(conn, make_config(), backend, sleeps=sleeps))
    assert sleeps == [1, 2]
    assert runs(conn)[0]["status"] == "ok"


def test_transient_errors_exhausted_fail():
    conn = connect(":memory:")
    sleeps = []
    backend = FakeBackend([TransientError("429")] * 3)
    with pytest.raises(LLMError):
        call(make_llm(conn, make_config(), backend, sleeps=sleeps))
    assert len(backend.calls) == 3
    assert sleeps == [1, 2]
    assert runs(conn)[0]["status"] == "failed"


def test_provider_error_fails_without_retry():
    conn = connect(":memory:")
    backend = FakeBackend([ProviderError("401 bad key")])
    with pytest.raises(LLMError, match="bad key"):
        call(make_llm(conn, make_config(), backend))
    assert len(backend.calls) == 1


def insert_spend(conn, cost, when):
    with conn:
        conn.execute(
            "INSERT INTO agent_runs (agent, model, cost_usd, status, started_at) VALUES ('x', 'y', ?, 'ok', ?)",
            (cost, to_iso(when)),
        )


def test_budget_cap_blocks_call():
    conn = connect(":memory:")
    insert_spend(conn, 5.0, FIXED_NOW.replace(hour=1))
    backend = FakeBackend([ok()])
    with pytest.raises(BudgetExceeded):
        call(make_llm(conn, make_config(daily_usd_cap=5.0), backend))
    assert backend.calls == []
    assert runs(conn)[-1]["status"] == "skipped_budget"


def test_yesterdays_spend_does_not_count():
    conn = connect(":memory:")
    insert_spend(conn, 5.0, FIXED_NOW.replace(day=28))
    llm = make_llm(conn, make_config(daily_usd_cap=5.0), FakeBackend([ok()]))
    assert llm.spent_today() == 0.0
    call(llm)
