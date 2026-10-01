import pytest

from pulse.agents.base import (
    BudgetExceeded, LLMError, ProviderError, StepResult, ToolCall, ToolError, ToolSpec, TransientError,
)
from pulse.db import connect
from tests.fakes import FakeBackend, make_config, make_llm

TOOLS = [ToolSpec("lookup", "Look something up.", {"type": "object", "properties": {"q": {"type": "string"}}})]


def step(text=None, *calls, inp=100, out=20):
    return StepResult(text, tuple(calls), inp, out)


def run(backend, execute=lambda call: f"result for {call.arguments.get('q')}", **config_overrides):
    conn = connect(":memory:")
    sleeps = []
    llm = make_llm(conn, make_config(**config_overrides), backend, sleeps=sleeps)
    return conn, llm, sleeps


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs").fetchall()


def test_tool_round_then_final_answer():
    backend = FakeBackend(steps=[
        step("checking", ToolCall("c1", "lookup", {"q": "auth"})),
        step("Auth docs are the problem."),
    ])
    conn, llm, _ = run(backend)
    executed = []
    resp = llm.run_tools("investigate", "sys", "why?", TOOLS, lambda c: executed.append(c) or "3 hits")
    assert (resp.text, resp.tool_calls) == ("Auth docs are the problem.", 1)
    assert executed == [ToolCall("c1", "lookup", {"q": "auth"})]
    second = backend.step_calls[1]["transcript"]
    assert second[1] == {"role": "assistant", "text": "checking", "tool_calls": [ToolCall("c1", "lookup", {"q": "auth"})]}
    assert second[2] == {"role": "tool", "results": [{"id": "c1", "content": "3 hits"}]}
    [row] = runs(conn)
    assert (row["agent"], row["status"], row["input_tokens"], row["output_tokens"]) == ("investigate", "ok", 200, 40)
    assert resp.run_id == row["id"]


def test_tool_errors_go_back_to_the_model():
    def boom(call):
        raise ToolError("bad date")

    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {})), step("done")])
    conn, llm, _ = run(backend)
    llm.run_tools("investigate", "sys", "q", TOOLS, boom)
    assert backend.step_calls[1]["transcript"][2]["results"] == [{"id": "c1", "content": "error: bad date"}]


def test_tool_call_limit_forces_a_final_answer():
    backend = FakeBackend(steps=[
        step(None, ToolCall("c1", "lookup", {}), ToolCall("c2", "lookup", {})),
        step("final"),
    ])
    conn, llm, _ = run(backend)
    resp = llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "x", max_calls=2)
    assert resp.text == "final"
    assert [c["allow_tools"] for c in backend.step_calls] == [True, False]
    assert "final answer" in backend.step_calls[1]["transcript"][2]["note"]


def test_transient_errors_back_off_per_step():
    backend = FakeBackend(steps=[TransientError("429"), step("ok")])
    conn, llm, sleeps = run(backend)
    assert llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "").text == "ok"
    assert sleeps == [1]


def test_provider_error_fails_the_run():
    backend = FakeBackend(steps=[ProviderError("401")])
    conn, llm, _ = run(backend)
    with pytest.raises(LLMError, match="investigate"):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")
    assert runs(conn)[0]["status"] == "failed"


def test_empty_final_answer_fails():
    backend = FakeBackend(steps=[step("   ")])
    conn, llm, _ = run(backend)
    with pytest.raises(LLMError, match="empty"):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")


def test_budget_cap_blocks_the_run():
    backend = FakeBackend(steps=[step("never")])
    conn, llm, _ = run(backend, daily_usd_cap=0.0)
    with pytest.raises(BudgetExceeded):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")
    assert backend.step_calls == []
    assert runs(conn)[0]["status"] == "skipped_budget"


def test_unexpected_tool_exception_records_failed_run_and_reraises():
    def boom(call):
        raise KeyError("boom")

    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {}))])
    conn, llm, _ = run(backend)
    with pytest.raises(KeyError):
        llm.run_tools("investigate", "sys", "q", TOOLS, boom)
    [row] = runs(conn)
    assert row["status"] == "failed"
    assert "KeyError" in row["error"]
    assert row["cost_usd"] == pytest.approx((100 * 1.0 + 20 * 5.0) / 1_000_000)


def test_budget_cap_reached_mid_loop_stops_and_records():
    first_step_cost = (100 * 1.0 + 20 * 5.0) / 1_000_000
    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {}))])
    conn, llm, _ = run(backend, daily_usd_cap=first_step_cost)
    with pytest.raises(BudgetExceeded):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "x")
    assert len(backend.step_calls) == 1
    [row] = runs(conn)
    assert row["status"] == "failed"
    assert row["cost_usd"] == pytest.approx(first_step_cost)


def test_unexpected_backend_exception_records_paid_usage_and_reraises():
    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {})), AttributeError("custom tool call")])
    conn, llm, _ = run(backend)
    with pytest.raises(AttributeError):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "x")
    [row] = runs(conn)
    assert row["status"] == "failed"
    assert row["error"] == "backend raised AttributeError: custom tool call"
    assert row["cost_usd"] == pytest.approx((100 * 1.0 + 20 * 5.0) / 1_000_000)


def test_mid_run_budget_message_reports_spend_so_far():
    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {}), inp=100_000)])
    conn, llm, _ = run(backend, daily_usd_cap=0.05)
    with pytest.raises(BudgetExceeded, match=r"mid-run after \$0\.10 on this run"):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "x")
    assert runs(conn)[0]["error"] == "daily budget cap reached mid-run after $0.10 on this run"
