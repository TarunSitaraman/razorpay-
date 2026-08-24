"""The holdout is never acted on, and is never scored as though it were.

Every incremental number this project reports is a difference against the
holdout. If a held-out customer is contacted, or merely *recorded* in a way that
a downstream reader mistakes for a contact, then the denominator moves and every
lift figure moves with it — silently, and in the flattering direction.

That is not hypothetical. `_act` deliberately writes a decision naming the
action it *would* have taken before declining to take it, because the console
needs the counterfactual. The evaluation harness read `agent_decision.action_kind`
back as though it were an action performed, and so scored 213 held-out cases as
treated. Nothing failed; the arms simply reported inflated lift.

So there are two distinct properties here, and they need separate tests because
they broke separately:

  1. no held-out case is ever ACTED on           (the dispatch guarantee)
  2. no held-out case is ever SCORED as acted    (the measurement guarantee)
"""

from __future__ import annotations

from datetime import UTC, datetime

from yukti.domain.enums import CaseState
from yukti.eval import oracle_bridge
from yukti.eval.arms import Arm
from yukti.eval.harness import _run_arm
from yukti.pipeline import plan_cycle
from yukti.scoring import FixedScorer

# The fixtures live in the module that owns the spine tests; importing them
# rebinds them here, which is how pytest shares module-scoped fixtures.
from test_plan_cycle import (  # noqa: F401
    AS_OF,
    _make_case,
    adapters,
    merchant_with_policy,
)


# A large positive effect, so every case is worth acting on and a holdout that
# leaks shows up immediately. A neutral scorer would let this test pass because
# nothing was worth doing, which is the wrong reason to be green.
EAGER = FixedScorer(0.30)


def test_holdout_cases_are_never_acted_on(conn, merchant_with_policy, adapters):
    ads, rzp = adapters
    held = [_make_case(conn, merchant_with_policy, arm="holdout") for _ in range(4)]
    treated = [_make_case(conn, merchant_with_policy, arm="treatment") for _ in range(4)]

    plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=EAGER)

    held_ids = tuple(c["case_id"] for c in held)
    acted = conn.execute(
        "SELECT count(*) AS n FROM recovery_action ra "
        "  JOIN agent_decision d ON d.id = ra.decision_id "
        " WHERE d.case_id = ANY(%s)", (list(held_ids),)
    ).fetchone()["n"]
    assert acted == 0, "a held-out case was dispatched an action"

    # And the treated arm did act, so the assertion above means "the holdout was
    # excluded", not "nothing happened this cycle".
    treated_acted = conn.execute(
        "SELECT count(*) AS n FROM recovery_action ra "
        "  JOIN agent_decision d ON d.id = ra.decision_id "
        " WHERE d.case_id = ANY(%s)",
        (list(c["case_id"] for c in treated),)
    ).fetchone()["n"]
    assert treated_acted > 0, "nothing was dispatched at all — test proves nothing"


def test_held_out_cases_land_in_held_out_not_scheduled(
    conn, merchant_with_policy, adapters
):
    """SCHEDULED would claim an action is pending on a case that will never get
    one — which is what the console reads, and what made this bug visible."""
    ads, _ = adapters
    held = _make_case(conn, merchant_with_policy, arm="holdout")

    plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=EAGER)

    state = conn.execute(
        "SELECT state FROM recovery_case WHERE id = %s", (held["case_id"],)
    ).fetchone()["state"]
    assert state == CaseState.HELD_OUT.value


def test_harness_scores_holdout_from_the_baseline_not_the_decision(
    conn, merchant_with_policy, adapters
):
    """The measurement guarantee, asserted against the recorded counterfactual.

    `_act` writes a decision naming a real action for a held-out case. This
    asserts the harness ignores it and scores the baseline instead — so the
    outcome is byte-identical to doing nothing, whatever the decision says.
    """
    ads, _ = adapters
    held = _make_case(conn, merchant_with_policy, arm="holdout")
    _make_case(conn, merchant_with_policy, arm="treatment")
    conn.commit()

    facts = oracle_bridge.load_case_facts(conn, merchant_with_policy, AS_OF, None)
    assert held["case_id"] in facts
    assert facts[held["case_id"]].assigned_arm == "holdout"

    baseline = {cid: oracle_bridge.score_no_action(f, 1) for cid, f in facts.items()}
    # A fixed scorer rather than the real `Y` arm: this asserts the harness's
    # holdout handling, and depending on a fitted model artifact would make it
    # fail for a reason that has nothing to do with the holdout.
    arm = Arm(key="T", label="test", scorer_factory=lambda: EAGER, description="")
    outcomes = _run_arm(
        conn, arm, merchant_with_policy, AS_OF, facts, baseline, None, 1,
    )
    scored = {o.case_id: o for o in outcomes}

    assert scored[held["case_id"]] == baseline[held["case_id"]], (
        "the harness scored a held-out case from its decision row rather than "
        "from the baseline — the denominator is contaminated"
    )

    # The decision row really does name an action, so the assertion above is
    # about the harness ignoring it rather than about there being nothing to
    # ignore.
    kind = conn.execute(
        "SELECT action_kind FROM agent_decision WHERE case_id = %s "
        "ORDER BY created_at DESC LIMIT 1", (held["case_id"],)
    ).fetchone()
    assert kind is not None and kind["action_kind"] != "suppress", (
        "no counterfactual action was recorded, so this test would pass even "
        "if the harness read the decision row"
    )
