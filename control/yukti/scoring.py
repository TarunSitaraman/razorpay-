"""Scorers — how a policy decides what an action is worth.

Pulled out of the pipeline because swapping this is what distinguishes the
evaluation arms from each other. `plan_cycle` runs the same allocator, the same
stopping rules and the same policy engine for every arm; only the number it
optimises changes. Keeping that difference in one injectable object is what
makes the comparison fair — if each arm had its own pipeline, a difference in
the result could always be a difference in the plumbing.

    UpliftScorer      the causal effect of acting              — Yukti
    PropensityScorer  P(recover | treated)                     — arm B3
    ConstantScorer    the same number for everything           — arms B1, B2
    ZeroScorer        nothing is worth anything                — degraded mode

`UpliftScorer` raises when no model is fitted rather than returning zeros. That
distinction turns out to matter more than it looks: zero uplift is
indistinguishable from "this customer will never pay", so a system falling back
to zeros stops every case with `LOST_CAUSE` and tells the merchant their entire
book is unrecoverable. It is not — we just could not score it. An operational
failure must not be reported as a business fact.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from yukti.candidates import Candidate
from yukti.dispatch.tools import CHANNEL_COST_PAISE
from yukti.domain.decline import lookup
from yukti.domain.enums import ActionKind, Transience
from yukti.intelligence import registry
from yukti.intelligence.features import build_candidate_frame

# The (case_id, action_kind, channel, discount_pct) tuple a score is keyed on.
ScoreKey = tuple


def key_for(c: Candidate) -> ScoreKey:
    return (c.case_id, c.action_kind.value, c.channel.value, c.discount_pct)


class Scorer(Protocol):
    name: str

    def score(
        self, cases: pd.DataFrame, proposals: dict[str, list[Candidate]]
    ) -> dict[ScoreKey, float]: ...


def _rows(proposals: dict[str, list[Candidate]]) -> list[dict]:
    return [
        {
            "case_id": c.case_id,
            "action_kind": c.action_kind.value,
            "action_channel": c.channel.value,
            "scheduled_for": c.scheduled_for,
            "cost_paise": CHANNEL_COST_PAISE.get(c.channel, 0),
            "discount_paise": c.discount_paise,
            "discount_pct": c.discount_pct,
            "_key": key_for(c),
        }
        for cands in proposals.values() for c in cands
    ]


class _ModelScorer:
    """Shared batching for the two model-backed scorers.

    One prediction call for every candidate in the cycle rather than one per
    candidate: with tens of thousands of cases and half a dozen proposals each,
    per-row inference would dominate the cycle and be entirely avoidable.
    """

    artifact_kind: str
    name: str

    def __init__(self, artifact=None) -> None:
        self._artifact = artifact

    def _load(self):
        if self._artifact is None:
            self._artifact = registry.load(self.artifact_kind)
        return self._artifact

    def _predict(self, cases: pd.DataFrame, proposals: dict[str, list[Candidate]]):
        rows = _rows(proposals)
        if not rows:
            return [], None
        frame_rows = pd.DataFrame(rows)
        keys = list(frame_rows.pop("_key"))
        frame = build_candidate_frame(cases, frame_rows)
        return keys, self._load().score(frame)


class UpliftScorer(_ModelScorer):
    """The causal effect of acting. This is what Yukti optimises."""

    artifact_kind = "uplift_action"
    name = "uplift"

    def score(self, cases, proposals):
        keys, scores = self._predict(cases, proposals)
        if not keys:
            return {}
        return dict(zip(keys, scores.uplift, strict=True))


class PropensityScorer(_ModelScorer):
    """P(recover | treated) — what a conventional recovery system ranks on.

    The strongest realistic competitor, and the one the whole thesis is about:
    it spends its budget on customers who were going to pay anyway. Given the
    same features and the same learner, so any difference in the result is the
    objective and not the implementation.
    """

    artifact_kind = "uplift_action"
    name = "propensity"

    def score(self, cases, proposals):
        keys, scores = self._predict(cases, proposals)
        if not keys:
            return {}
        return dict(zip(keys, scores.p_treated, strict=True))


class ConstantScorer:
    """Every action is worth the same fixed effect.

    Backs the rule-based arms. A fixed-cadence dunning system does not estimate
    anything — it assumes contacting helps by some amount and acts on everything
    it can afford, which is exactly this.
    """

    name = "constant"

    def __init__(self, value: float = 0.05) -> None:
        self.value = value

    def score(self, cases, proposals):
        return {
            key_for(c): (0.0 if c.action_kind is ActionKind.SUPPRESS else self.value)
            for cands in proposals.values() for c in cands
        }


class ReasonCodeScorer:
    """A best-practice static routing table, keyed on the decline code.

    Arm B2. Better than a flat cadence and entirely non-causal: it encodes what
    a thoughtful engineer would write after reading the decline taxonomy, with
    no model and no notion of who the customer is.
    """

    name = "reason_code"

    # Assumed effect by transience. Deliberately plausible rather than tuned —
    # the point of the arm is that a sensible rule table still loses to a causal
    # estimate, not that it was set up to fail.
    BY_TRANSIENCE = {
        Transience.TRANSIENT_FUNDS: 0.08,
        Transience.TRANSIENT_AUTH: 0.06,
        Transience.TRANSIENT_SYSTEM: 0.04,
        Transience.SEMI_PERMANENT: 0.05,
        Transience.PERMANENT: 0.0,
        Transience.UNCLASSIFIED: 0.02,
    }

    def score(self, cases, proposals):
        out: dict[ScoreKey, float] = {}
        for cands in proposals.values():
            for c in cands:
                if c.action_kind is ActionKind.SUPPRESS:
                    out[key_for(c)] = 0.0
                    continue
                spec = lookup(c.decline_code)
                base = self.BY_TRANSIENCE.get(spec.transience, 0.02)
                # A silent retry is free, so the table favours it wherever the
                # code says it can work — which is what a good rules engine does.
                if c.action_kind is ActionKind.SILENT_RETRY and spec.retryable_silently:
                    base *= 1.5
                out[key_for(c)] = base
        return out


class FixedScorer:
    """Scores supplied directly. For tests, and for replaying a past decision."""

    name = "fixed"

    def __init__(self, scores: dict[ScoreKey, float] | float) -> None:
        self._scores = scores

    def score(self, cases, proposals):
        if isinstance(self._scores, dict):
            return dict(self._scores)
        return {
            key_for(c): (0.0 if c.action_kind is ActionKind.SUPPRESS else self._scores)
            for cands in proposals.values() for c in cands
        }


class ZeroScorer:
    """Nothing is worth anything.

    Not a fallback — an explicit choice, so that a cycle running with no model
    is something the operator asked for rather than something that happened to
    them. See the module docstring for why silent zeros are dangerous.
    """

    name = "zero"

    def score(self, cases, proposals):
        return {key_for(c): 0.0 for cands in proposals.values() for c in cands}


def default_scorer() -> Scorer:
    return UpliftScorer()


ARMS: dict[str, type] = {
    "uplift": UpliftScorer,
    "propensity": PropensityScorer,
    "reason_code": ReasonCodeScorer,
    "constant": ConstantScorer,
    "zero": ZeroScorer,
}
