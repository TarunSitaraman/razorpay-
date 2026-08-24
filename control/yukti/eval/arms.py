"""The five arms, and why they are the five.

Every arm runs the SAME allocator, the SAME stopping rules and the SAME policy
engine. Only the number being optimised changes — which is the point. If each
arm had its own pipeline, any difference in the result could always be a
difference in the plumbing, and the comparison would prove nothing.

That constraint is why `scoring.py` exists at all: the scorer was made
injectable during day 5 specifically so this file could be a table rather than
five implementations.

The arms are chosen to make the thesis falsifiable rather than to flatter it.
B3 is a well-built conventional recovery system given the same features and the
same learner as Yukti; if the uplift objective did not matter, B3 would win.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from yukti.scoring import (
    ConstantScorer,
    PropensityScorer,
    ReasonCodeScorer,
    Scorer,
    RetryOnlyScorer,
    UpliftScorer,
)


@dataclass(frozen=True, slots=True)
class Arm:
    key: str
    label: str
    # None for the holdout: it does not run a planning cycle at all.
    scorer_factory: Callable[[], Scorer] | None
    description: str

    @property
    def acts(self) -> bool:
        return self.scorer_factory is not None

    def scorer(self) -> Scorer | None:
        return self.scorer_factory() if self.scorer_factory else None


ARMS: tuple[Arm, ...] = (
    Arm(
        key="B0",
        label="holdout (no action)",
        scorer_factory=None,
        description=(
            "Nothing is done. This is the denominator every other recovery "
            "product omits: without it, organic recovery is silently billed as "
            "recovered revenue."
        ),
    ),
    Arm(
        key="B4",
        label="retry-only",
        scorer_factory=RetryOnlyScorer,
        description=(
            "Take every free silent retry and never contact anyone. Contacts "
            "score zero so none can clear its channel cost, while costless "
            "actions are funded regardless of score. It is the honest cheap "
            "baseline, and after the costless-action rule it is what every "
            "acting arm now has in common: the difference between B1, B2, B3 "
            "and Y is ONLY how they spend the contact budget on top of it."
        ),
    ),
    Arm(
        key="B1",
        label="fixed cadence",
        scorer_factory=lambda: ConstantScorer(value=0.05),
        description=(
            "The industry default. Assumes contact helps by some fixed amount "
            "and works whatever it can afford, in whatever order it finds."
        ),
    ),
    Arm(
        key="B2",
        label="reason-code rules",
        scorer_factory=ReasonCodeScorer,
        description=(
            "A best-practice static routing table over the decline taxonomy. "
            "What a thoughtful engineer writes with no model: better than a flat "
            "cadence, and with no notion of who the customer is."
        ),
    ),
    Arm(
        key="B3",
        label="propensity only",
        scorer_factory=PropensityScorer,
        description=(
            "P(recover | treated). The strongest realistic competitor and the "
            "one that matters — same features, same learner, same budget, "
            "different objective. If uplift did not matter, this would win."
        ),
    ),
    Arm(
        key="Y",
        label="Yukti (uplift)",
        scorer_factory=UpliftScorer,
        description=(
            "Maximises the causal effect of acting rather than the chance of "
            "recovery, so budget goes to persuadables instead of sure things."
        ),
    ),
)

BY_KEY: dict[str, Arm] = {a.key: a for a in ARMS}

# The arm every other arm is measured against.
HOLDOUT = BY_KEY["B0"]
# The cheap policy every acting arm shares. Measuring against THIS is what
# isolates the contact-allocation decision from the free-retry mass.
REFERENCE = BY_KEY["B4"]
# The one whose defeat is the actual claim.
RIVAL = BY_KEY["B3"]
