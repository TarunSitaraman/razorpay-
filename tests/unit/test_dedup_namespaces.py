"""The two dedup stages must not share a Redis keyspace.

Regression test for a bug that silently broke the entire pipeline: the Go edge
and the Python control plane both used `yukti:evt:<id>`. The edge sets the key
first, so the control plane saw a key that "proved" it had already handled the
event and discarded all of it. The pipeline ingested 8,775 events and opened
zero cases, reporting a clean run the whole way.

The two stages answer different questions about the same event id:
  edge          — has this webhook been delivered to us before?
  control plane — have we already formed an opportunity from it?

Both answers are legitimate and independent, so they need separate namespaces.
"""

from __future__ import annotations

import pathlib
import re

from yukti.opportunity.service import DEDUP_KEY_PREFIX

EDGE_DEDUP_GO = pathlib.Path(__file__).resolve().parents[2] / "edge/internal/dedup/dedup.go"


def edge_prefix() -> str:
    src = EDGE_DEDUP_GO.read_text()
    match = re.search(r'KeyPrefix\s*=\s*"([^"]+)"', src)
    assert match, "edge KeyPrefix constant not found — did the file move?"
    return match.group(1)


class TestNamespacesAreDistinct:
    def test_prefixes_differ(self):
        assert edge_prefix() != DEDUP_KEY_PREFIX

    def test_neither_prefix_contains_the_other(self):
        # A shared prefix would still collide under any scan or eviction policy
        # keyed on prefix, even if the full keys differ.
        edge, opp = edge_prefix(), DEDUP_KEY_PREFIX
        assert not edge.startswith(opp)
        assert not opp.startswith(edge)

    def test_keys_for_the_same_event_id_do_not_collide(self):
        event_id = "evt_01ABCDEF"
        assert f"{DEDUP_KEY_PREFIX}{event_id}" != f"{edge_prefix()}{event_id}"

    def test_both_stay_under_a_common_root_for_operability(self):
        # Distinct, but still greppable and flushable as one application's keys.
        assert DEDUP_KEY_PREFIX.startswith("yukti:")
        assert edge_prefix().startswith("yukti:")


class TestValidation:
    def test_missing_required_fields_are_rejected(self):
        import pytest
        from yukti.opportunity.service import MalformedEvent, validate

        complete = {
            "merchant_id": "m", "customer_id": "c",
            "obligation_id": "o", "ts": "2026-05-01T00:00:00",
        }
        validate(complete)  # must not raise

        for field in complete:
            partial = {k: v for k, v in complete.items() if k != field}
            with pytest.raises(MalformedEvent, match=field):
                validate(partial)

    def test_empty_string_counts_as_missing(self):
        import pytest
        from yukti.opportunity.service import MalformedEvent, validate

        with pytest.raises(MalformedEvent):
            validate({"merchant_id": "", "customer_id": "c",
                      "obligation_id": "o", "ts": "2026-05-01T00:00:00"})
