import json
from pathlib import Path

import httpx
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.classifier import JEV_QUESTIONS, ClassifierResult
from pulse.agents.providers.jev_backend import SYSTEMONE_URL, JevBackend

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "jev_response.json").read_text())
STATE = {"message_id": "m1", "author": "alice", "is_team": False, "channel": "help",
         "content": "Install fails on M1", "reply_to": None, "context": []}


def backend_with(handler):
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return JevBackend(client=httpx.Client(transport=httpx.MockTransport(wrapped)), api_key="k"), seen


def test_posts_questions_and_parses_answers():
    backend, seen = backend_with(lambda r: httpx.Response(200, json=FIXTURE))
    result = backend.classify("jev-latest", STATE)
    assert result == ClassifierResult(
        needs_reply_p=0.93, kind="bug", kind_confidence=0.94, sentiment=-1,
        sentiment_confidence=0.8, reported_cost=pytest.approx(1.7724e-05),
    )
    [req] = seen
    assert str(req.url) == SYSTEMONE_URL
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body == {"model": "jev-latest", "state": STATE, "questions": JEV_QUESTIONS}
    assert set(body["questions"]) == {"needs_reply", "kind", "sentiment"}


def test_questions_cover_every_kind_and_sentiment():
    assert set(JEV_QUESTIONS["kind"]["criteria"]) == {"bug", "question", "feature_request", "docs", "praise", "other"}
    assert set(JEV_QUESTIONS["sentiment"]["criteria"]) == {"-2", "-1", "0", "1", "2"}
    assert JEV_QUESTIONS["needs_reply"]["type"] == "noul"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_are_transient(status):
    backend, _ = backend_with(lambda r: httpx.Response(status, text="busy"))
    with pytest.raises(TransientError):
        backend.classify("jev-latest", STATE)


def test_connection_error_is_transient():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    backend, _ = backend_with(boom)
    with pytest.raises(TransientError):
        backend.classify("jev-latest", STATE)


def test_bad_request_is_provider_error():
    backend, _ = backend_with(lambda r: httpx.Response(400, json={"error": {"message": "invalid_union"}}))
    with pytest.raises(ProviderError, match="400"):
        backend.classify("jev-latest", STATE)


def test_non_json_body_is_output_invalid():
    backend, _ = backend_with(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_unknown_kind_is_output_invalid_with_cost():
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"]["kind"]["choice"] = "rant"
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid) as exc:
        backend.classify("jev-latest", STATE)
    assert exc.value.reported_cost == pytest.approx(1.7724e-05)


@pytest.mark.parametrize("path,value", [
    (("sentiment", "choice"), "3"),
    (("needs_reply", "noul"), 1.4),
])
def test_out_of_range_answers_are_output_invalid(path, value):
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"][path[0]][path[1]] = value
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_missing_answer_is_output_invalid():
    bad = json.loads(json.dumps(FIXTURE))
    del bad["answers"]["sentiment"]
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_missing_cost_leaves_reported_cost_none():
    no_cost = json.loads(json.dumps(FIXTURE))
    del no_cost["usage"]["cost"]
    backend, _ = backend_with(lambda r: httpx.Response(200, json=no_cost))
    assert backend.classify("jev-latest", STATE).reported_cost is None


def test_non_numeric_cost_is_treated_as_missing():
    bad = json.loads(json.dumps(FIXTURE))
    bad["usage"]["cost"] = "n/a"
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    result = backend.classify("jev-latest", STATE)
    assert result.reported_cost is None


def test_usage_not_a_dict_is_treated_as_missing():
    bad = json.loads(json.dumps(FIXTURE))
    bad["usage"] = ["not", "a", "dict"]
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    result = backend.classify("jev-latest", STATE)
    assert result.reported_cost is None


def test_answers_not_a_dict_is_output_invalid():
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"] = []
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_confidence_out_of_range_is_output_invalid():
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"]["kind"]["confidence"] = 1.5
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


from pulse.agents.classifier import ChoiceResult, theme_question

THEMES = [{"id": 3, "name": "Auth docs", "description": "token step missing"},
          {"id": 7, "name": "M1 install", "description": "arm64 wheels"}]
CHOICE_FIXTURE = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {"choice": {"type": "choice", "choice": "7", "probabilities": {"3": 0.05, "7": 0.9, "none": 0.05}, "confidence": 0.9}},
    "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 1.2e-05},
}


def test_theme_question_offers_each_theme_and_none():
    q = theme_question(THEMES)
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"3", "7", "none"}
    assert q["criteria"]["7"] == "M1 install: arm64 wheels"


def test_choose_posts_single_choice_question_and_parses():
    backend, seen = backend_with(lambda r: httpx.Response(200, json=CHOICE_FIXTURE))
    q = theme_question(THEMES)
    result = backend.choose("jev-latest", STATE, q)
    assert result == ChoiceResult(choice="7", confidence=0.9, reported_cost=pytest.approx(1.2e-05))
    assert json.loads(seen[0].content)["questions"] == {"choice": q}


def test_choose_rejects_a_choice_not_offered():
    bad = json.loads(json.dumps(CHOICE_FIXTURE))
    bad["answers"]["choice"]["choice"] = "42"
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.choose("jev-latest", STATE, theme_question(THEMES))


def test_choose_maps_server_errors_to_transient():
    backend, _ = backend_with(lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(TransientError):
        backend.choose("jev-latest", STATE, theme_question(THEMES))
