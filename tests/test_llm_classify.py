import pytest

from pulse.agents.base import BudgetExceeded, LLMError, OutputInvalid, ProviderError, TransientError
from pulse.agents.classifier import ChoiceResult
from pulse.config import Price
from pulse.db import connect
from tests.fakes import FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, make_llm

STATE = {"message_id": "m1", "content": "hi"}


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()


def setup(responses, **config_overrides):
    conn = connect(":memory:")
    fc = FakeClassifier(responses)
    config = make_config(classifier=classifier_config(), **config_overrides)
    sleeps = []
    return conn, fc, make_llm(conn, config, FakeBackend(), sleeps=sleeps, classifier=fc), sleeps


def test_classify_returns_result_and_logs_reported_cost():
    conn, fc, llm, _ = setup([jev_result(kind="bug", cost=0.00002)])
    resp = llm.classify(STATE)
    assert resp.result.kind == "bug"
    [run] = runs(conn)
    assert resp.run_id == run["id"]
    assert (run["agent"], run["model"], run["status"]) == ("classifier", "jev:jev-latest", "ok")
    assert run["cost_usd"] == pytest.approx(0.00002)
    assert fc.calls == [{"model": "jev-latest", "state": STATE}]


def test_per_request_price_used_when_no_reported_cost():
    pricing = dict(make_config().pricing, **{"jev:jev-latest": Price(0.0, 0.0, per_request=0.00004)})
    conn, _, llm, _ = setup([jev_result(cost=None)], pricing=pricing)
    llm.classify(STATE)
    assert runs(conn)[0]["cost_usd"] == pytest.approx(0.00004)


def test_classify_is_blocked_by_budget_cap():
    conn, fc, llm, _ = setup([jev_result()], daily_usd_cap=0.0)
    with pytest.raises(BudgetExceeded):
        llm.classify(STATE)
    assert fc.calls == []
    assert runs(conn)[0]["status"] == "skipped_budget"


def test_classify_retries_transient_errors():
    conn, fc, llm, sleeps = setup([TransientError("429"), jev_result()])
    llm.classify(STATE)
    assert sleeps == [1]
    assert len(fc.calls) == 2


def test_invalid_output_twice_fails_and_bills_both():
    bad = OutputInvalid("unknown kind", reported_cost=0.00001)
    conn, fc, llm, _ = setup([bad, bad])
    with pytest.raises(LLMError, match="classifier"):
        llm.classify(STATE)
    [run] = runs(conn)
    assert run["status"] == "failed"
    assert run["cost_usd"] == pytest.approx(0.00002)


def test_provider_error_fails_without_retry():
    conn, fc, llm, _ = setup([ProviderError("401")])
    with pytest.raises(LLMError):
        llm.classify(STATE)
    assert len(fc.calls) == 1


def test_classify_without_classifier_raises():
    llm = make_llm(connect(":memory:"), make_config(), FakeBackend())
    assert llm.has_classifier is False
    with pytest.raises(RuntimeError, match="classifier"):
        llm.classify(STATE)


def test_config_property_exposes_config():
    config = make_config(classifier=classifier_config())
    llm = make_llm(connect(":memory:"), config, FakeBackend(), classifier=FakeClassifier())
    assert llm.config is config
    assert llm.has_classifier is True


QUESTION = {"type": "choice", "criteria": {"1": "A", "none": "none"}}


def test_choose_returns_choice_and_logs_classifier_run():
    conn = connect(":memory:")
    fc = FakeClassifier(choices=[ChoiceResult("1", 0.8, 0.00001)])
    llm = make_llm(conn, make_config(classifier=classifier_config()), FakeBackend(), classifier=fc)
    resp = llm.choose({"message_id": "m1"}, QUESTION)
    assert (resp.result.choice, resp.result.confidence) == ("1", 0.8)
    [run] = runs(conn)
    assert (run["agent"], run["status"]) == ("classifier", "ok")
    assert run["cost_usd"] == pytest.approx(0.00001)
    assert fc.choose_calls == [{"model": "jev-latest", "state": {"message_id": "m1"}, "question": QUESTION}]


def test_choose_is_blocked_by_budget_cap():
    conn = connect(":memory:")
    fc = FakeClassifier(choices=[ChoiceResult("1", 0.8)])
    llm = make_llm(conn, make_config(classifier=classifier_config(), daily_usd_cap=0.0), FakeBackend(), classifier=fc)
    with pytest.raises(BudgetExceeded):
        llm.choose({}, QUESTION)
    assert fc.choose_calls == []
