"""Adapters — the only code that talks to the outside world.

`RazorpayAdapter` is a Protocol with exactly one implementation, `SandboxRazorpay`,
which speaks HTTP to the payments sandbox. There is deliberately no `LiveRazorpay`:
`api.razorpay.com` is unreachable from this environment and there are no merchant
keys, so a live implementation would be code that has never been executed. Writing
it would make the repository look more complete and be less trustworthy.

What the seam buys is that the claim "swap in live keys and it works" is checkable
rather than rhetorical. Everything above this file is written against the Protocol;
the sandbox implements Razorpay's documented request and response shapes; a live
implementation would be the same calls against a different base URL. If the
control plane had grown a habit of sending fields Razorpay does not accept, that
swap would fail — which is exactly why the sandbox rejects unknown fields.

Notification and voice are separate adapters because they are separate vendors in
reality. Razorpay sends a payment link over a channel; it does not run your
outbound voice campaign.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from yukti.config import settings


class AdapterError(RuntimeError):
    """A call to an external system failed.

    Distinct from a refusal. An `AdapterError` means we do not know whether the
    action happened, so the caller must not release the budget or mark the case
    done — it must leave the intent recorded and let the idempotency key make a
    retry safe.
    """


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What an adapter did. `external_id` is what the vendor called it."""

    external_id: str
    status: str
    detail: dict[str, Any]

    @property
    def was_replay(self) -> bool:
        """The vendor recognised our idempotency key and did not act again."""
        return bool(self.detail.get("idempotent_replay"))


class RazorpayAdapter(Protocol):
    def create_payment_link(
        self, *, amount_paise: int, obligation_id: str, merchant_id: str,
        customer_id: str, idempotency_key: str, notes: dict[str, Any] | None = None,
    ) -> DispatchResult: ...

    def notify_payment_link(self, *, link_id: str, medium: str) -> DispatchResult: ...

    def charge_mandate(
        self, *, amount_paise: int, obligation_id: str, merchant_id: str,
        customer_id: str, rail: str, decline_code: str, issuer: str | None,
        idempotency_key: str,
    ) -> DispatchResult: ...


class _HttpBase:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self.base_url = (base_url or settings().sandbox_url).rstrip("/")
        # One client per adapter instance, not one per call. Constructing a
        # client per request cost 25.8x on the webhook path when it was measured
        # on day 2; the same mistake here would be paid on every dispatch.
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, path: str, json: dict | None = None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"X-Idempotency-Key": idempotency_key} if idempotency_key else {}
        try:
            resp = self._client.post(f"{self.base_url}{path}", json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterError(f"POST {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise AdapterError(f"POST {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json()


class SandboxRazorpay(_HttpBase):
    """Razorpay's public REST contract, served by the local simulator."""

    def create_payment_link(
        self, *, amount_paise: int, obligation_id: str, merchant_id: str,
        customer_id: str, idempotency_key: str, notes: dict[str, Any] | None = None,
    ) -> DispatchResult:
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            # Razorpay carries merchant context in `notes`. Identifiers only —
            # nothing here describes the customer's behaviour, because the
            # control plane does not know it and must not appear to.
            "notes": {
                "obligation_id": obligation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                **(notes or {}),
            },
        }
        body = self._post("/v1/payment_links", payload, idempotency_key)
        return DispatchResult(body["id"], body.get("status", "created"), body)

    def notify_payment_link(self, *, link_id: str, medium: str) -> DispatchResult:
        body = self._post(f"/v1/payment_links/{link_id}/notify_by/{medium}")
        return DispatchResult(link_id, "notified", body)

    def charge_mandate(
        self, *, amount_paise: int, obligation_id: str, merchant_id: str,
        customer_id: str, rail: str, decline_code: str, issuer: str | None,
        idempotency_key: str,
    ) -> DispatchResult:
        payload = {
            "amount": amount_paise, "obligation_id": obligation_id,
            "merchant_id": merchant_id, "customer_id": customer_id,
            "rail": rail, "decline_code": decline_code, "issuer": issuer,
        }
        body = self._post(f"/v1/subscriptions/{obligation_id}/charge",
                          payload, idempotency_key)
        return DispatchResult(body["id"], body.get("status", "unknown"), body)


class VoiceAdapter(_HttpBase):
    """Outbound voice, simulated.

    Namespaced `/_sim/` on the sandbox because Razorpay has no voice API and this
    must never read as one. It exists because voice costs Rs 9 against Rs 0.75
    for WhatsApp, and an allocator that has never seen an expensive channel has
    not been tested on the trade-off it is for.
    """

    def call(
        self, *, amount_paise: int, obligation_id: str, merchant_id: str,
        customer_id: str, issuer: str | None, discount_pct: float,
        idempotency_key: str,
    ) -> DispatchResult:
        body = self._post("/_sim/voice_calls", {
            "amount": amount_paise, "obligation_id": obligation_id,
            "merchant_id": merchant_id, "customer_id": customer_id,
            "issuer": issuer, "discount_pct": discount_pct,
        }, idempotency_key)
        return DispatchResult(body["id"], body.get("status", "completed"), body)


@dataclass(slots=True)
class Adapters:
    """Everything the dispatcher can reach. Constructed once per cycle."""

    razorpay: RazorpayAdapter
    voice: VoiceAdapter

    @classmethod
    def sandbox(cls, base_url: str | None = None) -> Adapters:
        return cls(razorpay=SandboxRazorpay(base_url), voice=VoiceAdapter(base_url))

    def close(self) -> None:
        for a in (self.razorpay, self.voice):
            if hasattr(a, "close"):
                a.close()
