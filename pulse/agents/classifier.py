"""Closed-set first-pass classifier (Jev): questions, result type, answer parsing.

Jev only picks from the labels offered, so it covers the decisions triage makes
(needs a reply, kind, sentiment); topics and prose stay with the LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulse.models import KINDS

QUESTIONS_VERSION = "jev-q1"
SENTIMENT_LABELS = ("-2", "-1", "0", "1", "2")

# Wire format for /systemone questions. Wording matches the 2026-09-30 benchmark.
JEV_QUESTIONS = {
    "needs_reply": {
        "type": "noul",
        "instructions": "Would a developer product's community team want a staff member to respond to this Discord message?",
        "criteria": {
            "true": "A non-staff user asks a question about using the product, or reports a bug, outage, error, "
                    "broken or wrong docs, or data loss (even without a question mark), or says they have the same "
                    "problem as someone else.",
            "false": "The author is staff (is_team true); or the message is aimed at other community members "
                     "(social plans, polls, 'anyone else using X?', meetups); or it is thanks, praise, chit-chat, "
                     "a feature request with nothing blocking the user, an answer, or the user says they solved it.",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of message is this, about the developer product?",
        "criteria": {
            "bug": "Something is broken or behaves wrongly.",
            "docs": "The documentation is missing, wrong, outdated, or confusing, including a how-to question caused by the docs skipping a step.",
            "question": "Asking how to do something with the product, when the docs are not the stated problem.",
            "feature_request": "Asking for something that does not exist yet.",
            "praise": "Compliments or enthusiasm about the product.",
            "other": "Everything else: staff announcements, short thanks, social chat.",
        },
    },
    "sentiment": {
        "type": "choice",
        "instructions": "How does the author feel about their experience with the product? Staff announcements and "
                        "answers are neutral. Developer slang like 'this is sick' is positive; sarcasm like "
                        "'love how it breaks every update' is negative.",
        "criteria": {
            "-2": "Angry, blocked, or reporting real damage (outage, data loss, considering switching).",
            "-1": "Frustrated, confused, or reporting something broken.",
            "0": "Neutral: questions, information, chit-chat.",
            "1": "Positive, thanks.",
            "2": "Enthusiastic praise.",
        },
    },
}


@dataclass(frozen=True)
class ClassifierResult:
    needs_reply_p: float
    kind: str
    kind_confidence: float
    sentiment: int
    sentiment_confidence: float
    reported_cost: float | None = None


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float
    reported_cost: float | None = None


class Classifier(Protocol):
    def classify(self, model: str, state: dict) -> ClassifierResult: ...

    def choose(self, model: str, state: dict, question: dict) -> ChoiceResult: ...


def parse_answers(data: dict) -> ClassifierResult:
    """Parse a /systemone response body. Raises on anything outside the offered labels."""
    answers = data["answers"]
    p = float(answers["needs_reply"]["noul"])
    kind = str(answers["kind"]["choice"])
    sentiment = str(answers["sentiment"]["choice"])
    kind_confidence = float(answers["kind"]["confidence"])
    sentiment_confidence = float(answers["sentiment"]["confidence"])
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"needs_reply probability out of range: {p}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if sentiment not in SENTIMENT_LABELS:
        raise ValueError(f"unknown sentiment {sentiment!r}")
    if not 0.0 <= kind_confidence <= 1.0:
        raise ValueError(f"kind_confidence out of range: {kind_confidence}")
    if not 0.0 <= sentiment_confidence <= 1.0:
        raise ValueError(f"sentiment_confidence out of range: {sentiment_confidence}")
    return ClassifierResult(
        needs_reply_p=p,
        kind=kind,
        kind_confidence=kind_confidence,
        sentiment=int(sentiment),
        sentiment_confidence=sentiment_confidence,
    )


def theme_question(themes) -> dict:
    """A single choice question over existing themes (rows or dicts with id, name, description)."""
    criteria = {str(t["id"]): f"{t['name']}: {t['description']}" for t in themes}
    criteria["none"] = "None of these themes fits this message."
    return {
        "type": "choice",
        "instructions": "Which recurring community theme is this Discord message about?",
        "criteria": criteria,
    }


def parse_choice(data: dict, question: dict) -> ChoiceResult:
    answer = data["answers"]["choice"]
    choice = str(answer["choice"])
    confidence = float(answer["confidence"])
    if choice not in question["criteria"]:
        raise ValueError(f"choice {choice!r} was not offered")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    return ChoiceResult(choice=choice, confidence=confidence)
