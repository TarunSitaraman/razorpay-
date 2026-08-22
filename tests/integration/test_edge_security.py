"""Adversarial tests against the Go ingest gateway.

The edge is the only component reachable by anything other than our own
services, so every one of these is a property an attacker would probe. They run
against the real binary over real HTTP rather than a mock, because the bugs
being guarded against — re-serialised bodies, timing-unsafe comparison,
unbounded reads — all live in the wiring rather than the logic.

Requires the stack and `edge/bin/ingest-gw` running. Skipped otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

EDGE = "http://localhost:9100"
SECRET = "yukti_dev_webhook_secret"
URL = f"{EDGE}/webhooks/razorpay"

pytestmark = pytest.mark.integration


def _edge_up() -> bool:
    try:
        return httpx.get(f"{EDGE}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _edge_up(), reason="ingest-gw not running"),
]


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def body_for(event_id: str, *, created_at: int | None = None, **extra) -> bytes:
    payload = {
        "event_id": event_id,
        "event_type": "payment.failed",
        "merchant_id": "mrc_test",
        **extra,
    }
    if created_at is not None:
        payload["created_at"] = created_at
    # Serialise ONCE; these exact bytes are both signed and sent. Signing a dict
    # and re-serialising later is the bug this whole test module exists for.
    return json.dumps(payload, separators=(",", ":")).encode()


def post(body: bytes, signature: str) -> httpx.Response:
    return httpx.post(
        URL, content=body, timeout=10.0,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


def unique(tag: str) -> str:
    return f"evt_{tag}_{time.time_ns()}"


class TestSignature:
    def test_valid_signature_accepted(self):
        b = body_for(unique("ok"), created_at=int(time.time()))
        r = post(b, sign(b))
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"

    def test_tampered_body_with_real_signature_rejected(self):
        # The core attack: capture a legitimate signature, change the amount.
        original = body_for(unique("t1"), created_at=int(time.time()))
        signature = sign(original)
        tampered = body_for(unique("t2"), created_at=int(time.time()), amount=99_999_999)
        r = post(tampered, signature)
        assert r.status_code == 401 and r.json()["reason"] == "bad_signature"

    @pytest.mark.parametrize(
        "signature", ["", "a" * 64, "not-hex", "0" * 128],
        ids=["empty", "wrong-hex", "not-hex", "too-long"],
    )
    def test_bad_signatures_rejected(self, signature):
        b = body_for(unique("bad"), created_at=int(time.time()))
        assert post(b, signature).status_code == 401

    def test_signature_from_a_different_secret_rejected(self):
        b = body_for(unique("sec"), created_at=int(time.time()))
        other = hmac.new(b"attacker-secret", b, hashlib.sha256).hexdigest()
        assert post(b, other).status_code == 401

    def test_whitespace_variant_does_not_verify(self):
        # Guards the re-serialisation bug directly: semantically identical JSON
        # with different spacing is a different message and must not verify.
        compact = body_for(unique("ws"), created_at=int(time.time()))
        spaced = json.dumps(json.loads(compact), separators=(", ", ": ")).encode()
        assert spaced != compact
        assert post(spaced, sign(compact)).status_code == 401


class TestReplayWindow:
    def test_stale_event_rejected_even_with_a_fresh_id(self):
        # Dedup cannot catch this: the attacker picks an id we have never seen.
        # Only a timestamp bound closes the replay hole.
        b = body_for(unique("stale"), created_at=int(time.time()) - 3600)
        r = post(b, sign(b))
        assert r.status_code == 401 and r.json()["reason"] == "stale_event"

    def test_implausibly_future_event_rejected(self):
        b = body_for(unique("future"), created_at=int(time.time()) + 172_800)
        assert post(b, sign(b)).status_code == 401

    def test_modest_clock_skew_tolerated(self):
        b = body_for(unique("skew"), created_at=int(time.time()) + 30)
        assert post(b, sign(b)).status_code == 200

    def test_event_without_timestamp_rejected(self):
        # Unreplayable-checkable means refused, not waved through.
        b = body_for(unique("nots"))
        assert post(b, sign(b)).status_code == 401


class TestDeduplication:
    def test_repeated_delivery_is_suppressed(self):
        b = body_for(unique("dup"), created_at=int(time.time()))
        s = sign(b)
        first, second, third = post(b, s), post(b, s), post(b, s)

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"
        assert third.json()["status"] == "duplicate"

    def test_duplicates_return_200_not_an_error(self):
        # A PSP retries on non-2xx with backoff, so erroring on something we
        # already handled would generate more of the traffic we are suppressing.
        b = body_for(unique("dup2"), created_at=int(time.time()))
        s = sign(b)
        post(b, s)
        assert post(b, s).status_code == 200


class TestMalformedInput:
    def test_malformed_json_rejected_not_crashed(self):
        b = b"this is not json"
        r = post(b, sign(b))
        assert r.status_code == 400 and r.json()["reason"] == "malformed_json"
        # The service must still be alive afterwards.
        assert httpx.get(f"{EDGE}/health", timeout=5.0).status_code == 200

    def test_missing_identifiers_rejected(self):
        b = json.dumps({"event_type": "payment.failed",
                        "created_at": int(time.time())}).encode()
        r = post(b, sign(b))
        assert r.status_code == 400 and r.json()["reason"] == "missing_identifiers"

    def test_oversized_body_rejected(self):
        # An unbounded read on an attacker-controlled body is a one-line DoS.
        b = json.dumps({
            "event_id": unique("big"), "event_type": "payment.failed",
            "merchant_id": "m", "created_at": int(time.time()),
            "pad": "A" * 5_000_000,
        }).encode()
        assert post(b, sign(b)).status_code == 413

    def test_get_not_allowed(self):
        assert httpx.get(URL, timeout=5.0).status_code == 405


class TestMetricsReconcile:
    def test_counters_add_up(self):
        text = httpx.get(f"{EDGE}/metrics", timeout=5.0).text
        vals: dict[str, float] = {}
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key, _, v = line.rpartition(" ")
            vals[key] = float(v)

        received = vals.get("yukti_edge_webhooks_received_total", 0)
        accepted = vals.get("yukti_edge_webhooks_accepted_total", 0)
        duplicate = vals.get("yukti_edge_webhooks_duplicate_total", 0)
        rejected = sum(v for k, v in vals.items()
                       if k.startswith("yukti_edge_webhooks_rejected_total"))

        # Every request lands in exactly one bucket. If this drifts, some path
        # is returning without accounting for itself.
        assert received == accepted + duplicate + rejected
