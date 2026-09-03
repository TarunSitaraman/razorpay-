"""Payments sandbox implementing Razorpay's PUBLIC REST + webhook contract.

Why this exists: `api.razorpay.com` is unreachable from the build environment
and there are no merchant keys, so calling the real API is not an option. Rather
than mock at the function level — which would let the integration seams go
untested — the sandbox implements the wire contract Razorpay documents. Yukti
talks HTTP to it exactly as it would to the real thing, and the adapter boundary
in `control/yukti/dispatch/` is the only place that would change.

It is a SIMULATOR. It is labelled as one everywhere it surfaces, and no output
of this service is ever presented as Razorpay data.

Whether an action actually succeeds is not decided here — it is delegated to the
datagen outcome oracle, so the sandbox and the evaluation harness share one
model of the world. If they disagreed, the demo and the lift numbers would be
telling different stories.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Body, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from yukti_sandbox import world
from yukti_sandbox.signing import sign
from yukti_sandbox.store import DebitAttempt, PaymentLink, store

# One HTTP client for the process, built at startup.
#
# Measured, not assumed: constructing an httpx.AsyncClient per webhook cost
# 59.3ms against 2.3ms for a reused one — 25.8x, and it was the entire
# bottleneck in webhook replay (2,000 events took 2m30s). Each call was paying
# connection-pool construction and a fresh TCP handshake. Notably, adding
# client-side concurrency first changed nothing, because a blocked-per-call
# setup cost does not parallelise away.
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client
    # A short CONNECT timeout, separate from the read timeout. `emit` already
    # treats a dead sink as non-fatal -- "the sink being down must not fail the
    # merchant's API call" -- but a single 10s budget only made the failure
    # non-fatal, not fast: with no gateway listening, Windows leaves the SYN
    # unanswered and every webhook blocked ~2s before giving up. That is paid
    # once per sandbox POST and twice per contact action, which turned a
    # planning cycle from seconds into hours. Read and write keep the full 10s;
    # only the "is anything there at all" question is answered quickly.
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=0.25),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
    )
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(
    lifespan=lifespan,
    title="Yukti Payments Sandbox (SIMULATOR — not Razorpay)",
    version="0.1.0",
    description=(
        "Implements Razorpay's public REST and webhook contract for local "
        "development. This is a simulator; it is not connected to Razorpay."
    ),
)

WEBHOOK_SECRET = os.getenv("YUKTI_WEBHOOK_SECRET", "yukti_dev_webhook_secret")
# Where signed webhooks are delivered: the Go ingest gateway.
WEBHOOK_SINK = os.getenv("YUKTI_WEBHOOK_SINK", "http://localhost:9100/webhooks/razorpay")


def _rid(prefix: str) -> str:
    from yukti.domain.ids import new_id

    return new_id(prefix)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Webhook emission
# ---------------------------------------------------------------------------

async def emit(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Sign and POST a webhook, mirroring Razorpay's delivery shape.

    The body is serialised ONCE and the signature is computed over those exact
    bytes, which are then the bytes sent. Signing a dict and serialising again
    afterwards is the classic way to produce a signature that cannot verify.
    """
    from yukti.domain.ids import event_id

    # An id supplied by the caller is honoured; otherwise one is minted.
    #
    # Made explicit rather than left to dict-spread ordering. Replaying the same
    # event log must present the SAME event ids so the edge recognises the
    # redelivery and suppresses it — that is what makes replay idempotent. A
    # freshly minted id per emit would defeat every dedup layer downstream and
    # make a re-run silently double every case.
    envelope = {
        "event_id": payload.pop("event_id", None) or event_id(),
        "event_type": event_type,
        "ts": _now().isoformat(),
        "created_at": int(_now().timestamp()),
        **payload,
    }
    body = json.dumps(envelope, separators=(",", ":")).encode()
    signature = sign(body, WEBHOOK_SECRET)
    store.record_emit(envelope)

    try:
        # Reuse the process-wide client. Constructing one here would reintroduce
        # the 25.8x regression measured above.
        client = _client or httpx.AsyncClient(timeout=10.0)
        resp = await client.post(
            WEBHOOK_SINK,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": envelope["event_id"],
            },
        )
        delivered = resp.status_code
    except httpx.HTTPError as exc:
        # A real PSP retries with backoff. Here we record and move on: the sink
        # being down must not fail the merchant's API call, and Yukti's own
        # replay path can always re-derive state from the event log.
        delivered = None
        envelope["_delivery_error"] = str(exc)

    return {"event": envelope, "signature": signature, "delivered": delivered}


# ---------------------------------------------------------------------------
# Outcome simulation
# ---------------------------------------------------------------------------

class _OracleUnavailable(RuntimeError):
    pass


def simulate_outcome(
    obligation_id: str, customer_id: str, amount_paise: int,
    action_kind: str, channel: str, decline_code: str,
    archetype: str, prior_contacts: int, at: datetime,
    in_downtime: bool = False, open_promise: bool = False,
    discount_pct: float = 0.0,
) -> dict[str, Any]:
    """Ask the shared outcome oracle whether this action worked."""
    from yukti.config import settings
    from yukti.domain.enums import ActionKind, Channel, UpliftArchetype
    from yukti_datagen.response import CaseContext, Intervention, evaluate

    ctx = CaseContext(
        case_id=obligation_id,
        archetype=UpliftArchetype(archetype),
        amount_paise=amount_paise,
        decline_code=decline_code,
        rail_is_mandate=action_kind in ("silent_retry", "schedule_debit"),
        preferred_channel=Channel(channel) if channel != "none" else Channel.NONE,
        prior_contacts_7d=prior_contacts,
        open_promise=open_promise,
        in_downtime=in_downtime,
    )
    iv = Intervention(
        kind=ActionKind(action_kind),
        channel=Channel(channel) if channel != "none" else Channel.NONE,
        at=at,
        discount_pct=discount_pct,
    )
    out = evaluate(ctx, iv, settings().seed)
    return {
        "recovered": out.recovered,
        "opted_out": out.opted_out,
        "recovered_paise": out.recovered_paise,
        "p_recover": round(out.p_recover, 4),
    }


# ---------------------------------------------------------------------------
# Razorpay-shaped REST surface
# ---------------------------------------------------------------------------

class PaymentLinkRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Amount in paise")
    currency: str = "INR"
    description: str | None = None
    # Razorpay carries arbitrary key/value in `notes`. It is also the field an
    # attacker controls, so Yukti treats it as untrusted throughout.
    notes: dict[str, Any] = Field(default_factory=dict)


@app.post("/v1/payment_links", status_code=201)
async def create_payment_link(
    req: PaymentLinkRequest,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    existing = store.resolve_idempotent(x_idempotency_key)
    if existing:
        # Same behaviour as a real PSP: return the original resource rather than
        # creating a second one. Without this the sandbox would charge twice and
        # hide a dispatcher bug instead of exposing it.
        link = store.links[existing]
        return {"id": link.id, "short_url": link.short_url, "status": link.status,
                "amount": link.amount_paise, "idempotent_replay": True}

    lid = _rid("plink")
    link = PaymentLink(
        id=lid,
        obligation_id=str(req.notes.get("obligation_id", "")),
        merchant_id=str(req.notes.get("merchant_id", "")),
        customer_id=str(req.notes.get("customer_id", "")),
        amount_paise=req.amount,
        short_url=f"https://rzp.io/i/{lid[-8:]}",
        discount_pct=float(req.notes.get("discount_pct", 0) or 0),
        action_kind=str(req.notes.get("action_kind", "payment_link")),
        issuer=req.notes.get("issuer") or None,
    )
    store.put_link(link, x_idempotency_key)
    return {"id": lid, "short_url": link.short_url, "status": link.status,
            "amount": req.amount, "currency": req.currency}


@app.post("/v1/payment_links/{link_id}/notify_by/{medium}")
async def notify_by(link_id: str, medium: str) -> dict[str, Any]:
    link = store.links.get(link_id)
    if not link:
        raise HTTPException(404, {"error": {"code": "BAD_REQUEST_ERROR",
                                            "description": "payment link not found"}})
    if medium not in ("sms", "email", "whatsapp"):
        raise HTTPException(400, {"error": {"code": "BAD_REQUEST_ERROR",
                                            "description": f"unsupported medium {medium}"}})
    if medium in link.notified_via:
        # A repeated notify is a no-op rather than a second nudge. Real delivery
        # is at-least-once, and re-scoring the outcome on a redelivery would let
        # a retried HTTP call manufacture a recovery that never happened.
        return {"success": True, "idempotent_replay": True}

    link.notified_via.append(medium)

    # Sending the link is the intervention, so this is where the outcome is
    # decided. Creating the link is not: an unsent link cannot convert, and
    # scoring at creation would credit recoveries to actions never delivered.
    now = _now()
    truth = world.resolve(link.customer_id, link.obligation_id, link.issuer, now)
    outcome = simulate_outcome(
        obligation_id=link.obligation_id, customer_id=link.customer_id,
        amount_paise=link.amount_paise, action_kind=link.action_kind,
        channel=medium, decline_code="UNKNOWN", archetype=truth.archetype,
        prior_contacts=truth.prior_contacts_7d, at=now,
        in_downtime=truth.in_downtime, open_promise=truth.open_promise,
        discount_pct=link.discount_pct,
    )

    if outcome["opted_out"]:
        # Opt-out is global and immediate under DPDP. It is emitted as its own
        # event rather than folded into a failure, because the control plane has
        # to act on it across every surface, not just this case.
        link.status = "cancelled"
        await emit("customer.opted_out", {
            "merchant_id": link.merchant_id, "customer_id": link.customer_id,
            "obligation_id": link.obligation_id, "channel": medium, "version": 2,
        })
    elif outcome["recovered"]:
        link.status = "paid"
        await emit("payment.captured", {
            "merchant_id": link.merchant_id, "customer_id": link.customer_id,
            "obligation_id": link.obligation_id, "attempt_id": _rid("pay"),
            "amount_paise": outcome["recovered_paise"] or link.amount_paise,
            "rail": "upi_intent", "decline_code": None, "version": 2,
        })

    return {"success": True}


class ChargeRequest(BaseModel):
    """A mandate debit — UPI AutoPay, e-NACH or a recurring card.

    Every field here is one a real Razorpay call carries. `archetype`,
    `in_downtime`, `open_promise` and `prior_contacts` used to be accepted from
    the caller; they are ground truth and the simulator now resolves them
    itself (see `world.py`). Accepting them meant the control plane had to read
    `customer.archetype` in order to dispatch, which is precisely the leak the
    feature layer refuses — and it would have leaked through the component least
    likely to be re-read.

    `extra = "forbid"` is the enforcement. A dispatcher that regains the habit
    of sending ground truth gets a 422, not a silent success.
    """

    model_config = {"extra": "forbid"}

    amount: int = Field(..., gt=0)
    obligation_id: str
    merchant_id: str
    customer_id: str
    rail: str = "upi_autopay"
    decline_code: str = "INSUFFICIENT_FUNDS"
    issuer: str | None = None


@app.post("/v1/subscriptions/{sub_id}/charge")
async def charge(
    sub_id: str,
    req: ChargeRequest,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Attempt a mandate debit and emit the resulting webhook."""
    existing = store.resolve_idempotent(x_idempotency_key)
    if existing:
        a = store.attempts[existing]
        return {"id": a.id, "status": a.status, "decline_code": a.decline_code,
                "idempotent_replay": True}

    now = _now()
    truth = world.resolve(req.customer_id, req.obligation_id, req.issuer, now)
    outcome = simulate_outcome(
        obligation_id=req.obligation_id, customer_id=req.customer_id,
        amount_paise=req.amount, action_kind="schedule_debit", channel="none",
        decline_code=req.decline_code, archetype=truth.archetype,
        prior_contacts=truth.prior_contacts_7d, at=now,
        in_downtime=truth.in_downtime, open_promise=truth.open_promise,
    )

    pid = _rid("pay")
    attempt = DebitAttempt(
        id=pid, obligation_id=req.obligation_id, merchant_id=req.merchant_id,
        customer_id=req.customer_id, amount_paise=req.amount, rail=req.rail,
        status="captured" if outcome["recovered"] else "failed",
        decline_code=None if outcome["recovered"] else req.decline_code,
    )
    store.put_attempt(attempt, x_idempotency_key)

    await emit(
        "payment.captured" if outcome["recovered"] else "payment.failed",
        {
            "merchant_id": req.merchant_id, "customer_id": req.customer_id,
            "obligation_id": req.obligation_id, "attempt_id": pid,
            "amount_paise": req.amount, "rail": req.rail,
            "decline_code": attempt.decline_code,
            "version": 2,
        },
    )
    return {"id": pid, "status": attempt.status, "decline_code": attempt.decline_code}


@app.get("/v1/payments/{payment_id}")
async def fetch_payment(payment_id: str) -> dict[str, Any]:
    a = store.attempts.get(payment_id)
    if not a:
        raise HTTPException(404, {"error": {"code": "BAD_REQUEST_ERROR",
                                            "description": "payment not found"}})
    return {"id": a.id, "status": a.status, "amount": a.amount_paise,
            "method": a.rail, "error_code": a.decline_code,
            "created_at": int(a.created_at.timestamp())}


# ---------------------------------------------------------------------------
# Simulated non-Razorpay channels
#
# Razorpay does not expose a voice API, so this is NOT part of any Razorpay
# contract and is namespaced `/_sim/` so it can never be mistaken for one. It
# exists because a voice call costs Rs 9 against Rs 0.75 for WhatsApp, and an
# allocator that never sees an expensive channel is not being tested on the
# decision that matters.
# ---------------------------------------------------------------------------

class VoiceCallRequest(BaseModel):
    model_config = {"extra": "forbid"}

    amount: int = Field(..., gt=0)
    obligation_id: str
    merchant_id: str
    customer_id: str
    issuer: str | None = None
    discount_pct: float = 0.0


@app.post("/_sim/voice_calls")
async def voice_call(
    req: VoiceCallRequest,
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    existing = store.resolve_idempotent(x_idempotency_key)
    if existing:
        return {"id": existing, "status": "completed", "idempotent_replay": True}

    now = _now()
    truth = world.resolve(req.customer_id, req.obligation_id, req.issuer, now)
    outcome = simulate_outcome(
        obligation_id=req.obligation_id, customer_id=req.customer_id,
        amount_paise=req.amount, action_kind="voice_call", channel="voice",
        decline_code="UNKNOWN", archetype=truth.archetype,
        prior_contacts=truth.prior_contacts_7d, at=now,
        in_downtime=truth.in_downtime, open_promise=truth.open_promise,
        discount_pct=req.discount_pct,
    )

    cid = _rid("call")
    store.put_call(cid, x_idempotency_key)

    if outcome["opted_out"]:
        await emit("customer.opted_out", {
            "merchant_id": req.merchant_id, "customer_id": req.customer_id,
            "obligation_id": req.obligation_id, "channel": "voice", "version": 2,
        })
    elif outcome["recovered"]:
        await emit("payment.captured", {
            "merchant_id": req.merchant_id, "customer_id": req.customer_id,
            "obligation_id": req.obligation_id, "attempt_id": _rid("pay"),
            "amount_paise": outcome["recovered_paise"] or req.amount,
            "rail": "upi_intent", "decline_code": None, "version": 2,
        })

    return {"id": cid, "status": "completed"}


# ---------------------------------------------------------------------------
# Test/demo affordances (clearly namespaced — not part of Razorpay's contract)
# ---------------------------------------------------------------------------

@app.post("/_sandbox/emit")
async def sandbox_emit(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Emit an arbitrary signed webhook. Used by the replayer and by tests."""
    event_type = payload.pop("event_type", "payment.failed")
    return await emit(event_type, payload)


@app.get("/_sandbox/emitted")
async def sandbox_emitted(limit: int = 50) -> list[dict[str, Any]]:
    return store.emitted[-limit:]


@app.post("/_sandbox/reset")
async def sandbox_reset() -> dict[str, str]:
    store.reset()
    return {"status": "reset"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "yukti-payments-sandbox", "mode": "SIMULATOR"}
