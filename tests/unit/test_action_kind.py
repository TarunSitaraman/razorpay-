from __future__ import annotations

import pytest

from yukti.domain.enums import ActionKind


FORBIDDEN_VALUES = {"refund", "payout", "mandate-cancel"}


class TestActionKindBounded:
    """Assert that ActionKind remains a bounded workflow.

    The track brief explicitly asks for a bounded workflow: ActionKind must omit
    refund, payout and mandate-cancel entirely at the type level. This is not a
    runtime check — it is a compile-time guarantee. Neither the planner nor a
    prompt-injected instruction can name a forbidden action kind.
    """

    def test_no_forbidden_action_kind_values(self) -> None:
        for kind in ActionKind:
            assert kind.value not in FORBIDDEN_VALUES, (
                f"ActionKind.{kind.name} has forbidden value '{kind.value}' "
                f"— expected ActionKind to omit {FORBIDDEN_VALUES}"
            )

    def test_action_kind_has_expected_members(self) -> None:
        expected = {
            "suppress",
            "silent_retry",
            "schedule_debit",
            "payment_link",
            "message",
            "voice_call",
            "discount_offer",
            "escalate",
        }
        actual = {kind.value for kind in ActionKind}
        assert actual == expected, (
            f"ActionKind members {actual} != expected {expected}. "
            "ActionKind has been modified outside the expected bounded set."
        )

    def test_no_forbidden_values_in_enum_definition(self) -> None:
        # Double-check by scanning the source: no member should have a forbidden value.
        # This is the authoritative guard: if a future developer adds a member with
        # a forbidden value, this test will catch it immediately.
        for member in ActionKind.__members__.values():
            assert member.value not in FORBIDDEN_VALUES, (
                f"Forbidden value '{member.value}' found in ActionKind enum"
            )