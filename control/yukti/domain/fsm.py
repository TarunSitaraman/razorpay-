"""State machines for obligations and recovery cases.

Two properties matter enough to encode here rather than trusting callers:

1. **No illegal transition.** A case that has been stopped must not silently
   resume; a recovered obligation must not reopen because a stale event arrived.
2. **Version monotonicity.** Every aggregate carries a version. Webhook delivery
   is at-least-once and unordered, so a late event carrying an older version is
   recorded as *superseded* rather than applied. This is the mechanism that makes
   out-of-order delivery safe without distributed locking.
"""

from __future__ import annotations

from dataclasses import dataclass

from yukti.domain.enums import CaseState, ObligationState


class IllegalTransition(ValueError):
    """Raised when a transition is not permitted by the state machine."""


class StaleEvent(ValueError):
    """Raised when an event carries a version at or behind current state."""


# A terminal state has no outgoing edges. That is the whole point of terminality:
# once a case stops, only a human or a new obligation starts new work.
_CASE_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.OPEN: frozenset({CaseState.PLANNING, CaseState.STOPPED, CaseState.RECOVERED}),
    CaseState.PLANNING: frozenset({CaseState.SCHEDULED, CaseState.STOPPED, CaseState.RECOVERED}),
    CaseState.SCHEDULED: frozenset(
        {CaseState.ACTING, CaseState.PLANNING, CaseState.STOPPED, CaseState.RECOVERED}
    ),
    CaseState.ACTING: frozenset(
        {CaseState.AWAITING_OUTCOME, CaseState.STOPPED, CaseState.RECOVERED}
    ),
    CaseState.AWAITING_OUTCOME: frozenset(
        {CaseState.PLANNING, CaseState.RECOVERED, CaseState.LOST, CaseState.STOPPED}
    ),
    CaseState.STOPPED: frozenset(),
    CaseState.RECOVERED: frozenset(),
    CaseState.LOST: frozenset(),
}

_OBLIGATION_TRANSITIONS: dict[ObligationState, frozenset[ObligationState]] = {
    ObligationState.OPEN: frozenset(
        {ObligationState.RECOVERED, ObligationState.LOST, ObligationState.EXPIRED}
    ),
    ObligationState.RECOVERED: frozenset(),
    ObligationState.LOST: frozenset(),
    ObligationState.EXPIRED: frozenset(),
}


def can_transition_case(src: CaseState, dst: CaseState) -> bool:
    return dst in _CASE_TRANSITIONS[src]


def assert_case_transition(src: CaseState, dst: CaseState) -> None:
    if not can_transition_case(src, dst):
        raise IllegalTransition(f"case: {src} -> {dst} is not permitted")


def can_transition_obligation(src: ObligationState, dst: ObligationState) -> bool:
    return dst in _OBLIGATION_TRANSITIONS[src]


def assert_obligation_transition(src: ObligationState, dst: ObligationState) -> None:
    if not can_transition_obligation(src, dst):
        raise IllegalTransition(f"obligation: {src} -> {dst} is not permitted")


@dataclass(frozen=True, slots=True)
class VersionCheck:
    """Outcome of comparing an inbound event's version to current state."""

    apply: bool
    superseded: bool
    reason: str


def check_version(current_version: int, event_version: int) -> VersionCheck:
    """Decide whether an inbound event should be applied.

    Equal versions are treated as duplicates rather than as errors: at-least-once
    delivery makes redelivery of the *same* event ordinary, and it must be a
    no-op, not a failure that lands the message in a DLQ.
    """
    if event_version > current_version:
        return VersionCheck(apply=True, superseded=False, reason="ahead")
    if event_version == current_version:
        return VersionCheck(apply=False, superseded=False, reason="duplicate")
    return VersionCheck(apply=False, superseded=True, reason="stale/out-of-order")
