"""The supervisor: gather evidence, delegate, record, resume.

Mirrors the pattern Razorpay published for Project Viveka — a supervisor over
parallel specialists, with evidence in an explicit store rather than in an
accumulating context window. The reason to copy it is operational rather than
stylistic: evidence in a table is what makes a crashed run resumable and a
narrative checkable.

**The rule that makes this safe to add at all.** The agent can NARROW what the
deterministic layer considers and ANNOTATE what it decided. It can never widen
either. There is no path from here to a larger budget, a new action kind, a
lifted stop, or a policy override — not because those are checked and refused,
but because the interfaces do not carry them. `advice()` returns a set of action
kinds to REMOVE and some text. That is the entire surface.

So the worst outcome from a model that is wrong, unavailable, or successfully
prompt-injected is that Yukti considers fewer options and explains itself less
well. The money still moves under the allocator, the policy engine and the
stopping rules exactly as it would with no agent at all.

**Resumption.** A run is identified in `agent_run`; evidence and conclusions are
keyed to it. Re-entering a run reads what is already there instead of re-calling
the model, so a crash costs the work in flight and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from yukti.agent import memory
from yukti.agent.schemas import Posture
from yukti.agent.specialists import (
    ComposerSpecialist,
    PlannerSpecialist,
    RCASpecialist,
)
from yukti.domain.enums import ActionKind
from yukti.domain.ids import run_id as new_run_id
from yukti.intelligence import degradation

log = logging.getLogger(__name__)

# Postures that mean "do not contact anyone on this dimension right now".
# Mapped to a candidate restriction rather than acted on directly — the agent
# names a posture, the deterministic layer decides what that costs.
SUPPRESSING_POSTURES = frozenset({Posture.SUPPRESS_AND_WAIT, Posture.SILENT_RETRY_LATER})

# Action kinds a suppressing posture removes. Contact only: a silent retry is
# still fine during an issuer wobble, and removing it would leave nothing that
# could recover the money.
CONTACT_KINDS = frozenset({
    ActionKind.MESSAGE.value, ActionKind.VOICE_CALL.value,
    ActionKind.DISCOUNT_OFFER.value,
})


@dataclass(slots=True)
class Advice:
    """Everything the agent contributes to a planning cycle.

    Read by `plan_cycle` and applied as a filter. Note what is absent: no
    budget, no amount, no action to add, no verdict. There is nowhere in this
    object to express any of them.
    """

    run_id: str
    # Action kinds to drop, per issuer. The only field with any effect.
    deprioritise_by_issuer: dict[str, frozenset[str]] = field(default_factory=dict)
    # Merchant-facing narrative per issuer, for the console and the audit trail.
    narratives: dict[str, str] = field(default_factory=dict)
    # Counted and reported: a fleet quietly running on fallbacks looks exactly
    # like one that is working.
    llm_calls: int = 0
    fallbacks: int = 0

    def drops_for(self, issuer: str | None) -> frozenset[str]:
        if not issuer:
            return frozenset()
        return self.deprioritise_by_issuer.get(issuer, frozenset())

    @property
    def degraded(self) -> bool:
        """True when some conclusion came from a fallback rather than the model."""
        return self.fallbacks > 0


class Supervisor:
    def __init__(
        self, conn: psycopg.Connection, rca: RCASpecialist | None = None,
        planner: PlannerSpecialist | None = None,
        composer: ComposerSpecialist | None = None,
    ) -> None:
        self.conn = conn
        self.rca = rca or RCASpecialist()
        self.planner = planner or PlannerSpecialist()
        self.composer = composer or ComposerSpecialist()

    # -- run lifecycle -------------------------------------------------------

    def open_run(self, merchant_id: str, trace_id: str) -> str:
        rid = new_run_id()
        self.conn.execute(
            "INSERT INTO agent_run (id, merchant_id, kind, trace_id, model, status) "
            "VALUES (%s, %s, 'supervisor', %s, %s, 'running')",
            (rid, merchant_id, trace_id, self.rca._model),
        )
        return rid

    def close_run(self, run_id: str, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE agent_run SET status = %s, finished_at = now() WHERE id = %s",
            (status, run_id),
        )

    # -- evidence ------------------------------------------------------------

    def gather(
        self, run_id: str, merchant_id: str, as_of: datetime
    ) -> list[degradation.HealthSignal]:
        """Scan for degradations and persist what the scan found.

        Every fact written here is computed by SQL. The model reads these rows
        and cannot add to them — which is what makes "cites only retrieved
        evidence" a checkable property rather than a hopeful instruction.
        """
        signals = degradation.degraded(self.conn, as_of, dimension="issuer")
        if not signals:
            return []

        for signal in signals:
            facts = [{
                "dimension": signal.dimension, "value": signal.value,
                "baseline_success_rate": round(signal.baseline_sr, 4),
                "observed_success_rate": round(signal.observed_sr, 4),
                "absolute_drop": round(signal.drop, 4),
                "z_score": round(signal.z_score, 2),
                "sample_size": signal.sample_size,
                "window": f"{signal.window_start:%Y-%m-%d %H:%M} to "
                          f"{signal.window_end:%Y-%m-%d %H:%M}",
            }]
            # The diagnostic tell: an auth regression and a capacity problem
            # produce the same headline drop and different decline mixes.
            mix = degradation.decline_mix(
                self.conn, signal.dimension, signal.value,
                signal.window_start, signal.window_end,
            )
            facts.extend(
                {"decline_code": m["decline_code"], "count": m["n"],
                 "share_of_failures": round(m["share"], 3)}
                for m in mix
            )
            memory.record(self.conn, run_id, "degradation_scan", facts,
                          subject=signal.value)
        return signals

    # -- the one thing plan_cycle consumes -----------------------------------

    def advise(
        self, run_id: str, merchant_id: str, as_of: datetime, resume: bool = True
    ) -> Advice:
        """Produce the cycle's advice. Safe to call again after a crash."""
        advice = Advice(run_id=run_id)

        if resume:
            prior = {
                c["subject"]: c for c in memory.conclusions(self.conn, run_id, "rca")
                if c["subject"]
            }
        else:
            prior = {}

        signals = self.gather(run_id, merchant_id, as_of)
        for signal in signals:
            issuer = signal.value

            if issuer in prior:
                # Already concluded in an earlier attempt at this run. Re-read
                # rather than re-call: the model would very likely say the same
                # thing, and paying for it twice is the cost a resume is
                # supposed to avoid.
                verdict = prior[issuer]["output"]
                posture = Posture(verdict["posture"])
                narrative = verdict["narrative"]
                provenance = prior[issuer]["provenance"]
            else:
                evidence = memory.retrieve(self.conn, run_id, "degradation_scan",
                                           subject=issuer)
                result = self.rca.analyse(signal.describe(), evidence)
                posture = result.output.posture
                narrative = result.output.narrative
                provenance = result.provenance
                memory.conclude(
                    self.conn, run_id, "rca", result.output.model_dump(mode="json"),
                    result.cited_ids, subject=issuer, model=result.model,
                    usage=result.usage, provenance=provenance,
                )

            advice.llm_calls += 1
            if provenance != "llm":
                advice.fallbacks += 1

            advice.narratives[issuer] = narrative
            if posture in SUPPRESSING_POSTURES:
                # The posture becomes a candidate restriction and nothing more.
                # It cannot stop a case, spend a budget or override a rule.
                advice.deprioritise_by_issuer[issuer] = frozenset(CONTACT_KINDS)

        return advice

    # -- composition ---------------------------------------------------------

    def compose(
        self, run_id: str, action_kind: str, channel: str, language: str = "en",
        context: str | None = None,
    ) -> tuple[str, str]:
        """Render channel copy. Returns (body, provenance).

        The body still contains {amount} and {link}; substitution happens in the
        dispatch layer from values code computed. The model never sees a rupee
        figure it could get wrong and never produces a URL.
        """
        result = self.composer.compose(action_kind, channel, language, context)
        memory.conclude(
            self.conn, run_id, "composer", result.output.model_dump(mode="json"),
            [], subject=f"{action_kind}:{channel}:{language}",
            model=result.model, usage=result.usage, provenance=result.provenance,
        )
        return result.output.body, result.provenance


def no_advice() -> Advice:
    """What a cycle uses when the agent is switched off.

    An explicit object rather than None, so `plan_cycle` has exactly one code
    path whether or not the agent ran. Two paths would mean the deterministic
    behaviour is only exercised when the agent is disabled, which is the half
    that must never be allowed to rot.
    """
    return Advice(run_id="")
