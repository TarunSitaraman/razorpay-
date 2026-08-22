"""In-memory state for the sandbox.

The sandbox owns payment-side state only — links it issued, debits it was asked
to attempt, and the webhooks it emitted. It deliberately does NOT read Yukti's
tables: it stands in for a third party, and letting it reach into the control
plane's database would hide exactly the integration seams the design is meant
to make explicit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class PaymentLink:
    id: str
    obligation_id: str
    merchant_id: str
    customer_id: str
    amount_paise: int
    short_url: str
    status: str = "created"
    notified_via: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class DebitAttempt:
    id: str
    obligation_id: str
    merchant_id: str
    customer_id: str
    amount_paise: int
    rail: str
    status: str
    decline_code: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


class SandboxStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.links: dict[str, PaymentLink] = {}
        self.attempts: dict[str, DebitAttempt] = {}
        self.emitted: list[dict[str, Any]] = []
        # Idempotency map, mirroring how a real PSP treats a repeated request
        # carrying the same key: return the ORIGINAL resource rather than
        # creating a second one. Without this the sandbox would happily charge
        # twice and mask a bug in Yukti's dispatcher.
        self.idempotency: dict[str, str] = {}

    def put_link(self, link: PaymentLink, idem: str | None = None) -> None:
        with self._lock:
            self.links[link.id] = link
            if idem:
                self.idempotency[idem] = link.id

    def put_attempt(self, attempt: DebitAttempt, idem: str | None = None) -> None:
        with self._lock:
            self.attempts[attempt.id] = attempt
            if idem:
                self.idempotency[idem] = attempt.id

    def resolve_idempotent(self, idem: str | None) -> str | None:
        if not idem:
            return None
        with self._lock:
            return self.idempotency.get(idem)

    def record_emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.emitted.append(event)

    def reset(self) -> None:
        with self._lock:
            self.links.clear()
            self.attempts.clear()
            self.emitted.clear()
            self.idempotency.clear()


store = SandboxStore()
