"""Classifying a provider failure into what the chain should do next.

The distinction that matters is between "this provider cannot serve me" and
"this request was bad". The first should move to the next provider; the second
will fail identically everywhere and moving on just burns the whole chain to
arrive at the same answer more slowly.

Getting this wrong is expensive in a specific way: a malformed request
classified as a provider problem walks the entire chain, spends every rate
limit, and reports "all providers unavailable" — which sends whoever is
debugging it to check API keys when the real problem is a schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Disposition(StrEnum):
    RETRY = "retry"              # transient, same provider, once
    FALL_THROUGH = "fall_through"  # this provider cannot serve us; try the next
    DISABLE = "disable"          # and do not try it again this process
    FATAL = "fatal"              # the request itself is wrong; stop


@dataclass(frozen=True, slots=True)
class Failure:
    provider: str
    disposition: Disposition
    reason: str
    status: int | None = None

    def __str__(self) -> str:
        code = f" [{self.status}]" if self.status else ""
        return f"{self.provider}{code}: {self.reason}"

    @property
    def is_permanent_for_provider(self) -> bool:
        return self.disposition is Disposition.DISABLE


# Statuses that mean "this provider will not serve us, and not on a retry
# either". A bad key or a policy denial does not improve by waiting, so the
# provider is disabled for the process rather than re-tried on every call — the
# difference between one connect timeout and one per decision.
_DISABLE_STATUSES = frozenset({401, 402, 403, 404})
# Worth another provider, but the current one might recover later.
_FALL_THROUGH_STATUSES = frozenset({408, 409, 413, 429, 500, 502, 503, 504})


def classify(provider: str, exc: BaseException) -> Failure:
    """Decide what one exception means for the chain."""
    status = _status_of(exc)
    name = type(exc).__name__
    detail = str(exc)[:200]

    # Connection-level failure: refused, DNS, TLS, or a proxy CONNECT denial.
    # Indistinguishable from the outside, and the answer is the same for all of
    # them — this host is not reachable from here, stop paying to find out.
    if _is_connection_error(exc):
        return Failure(provider, Disposition.DISABLE,
                       f"unreachable ({name}: {detail})", status)

    if status in _DISABLE_STATUSES:
        return Failure(provider, Disposition.DISABLE,
                       f"rejected and will not recover ({detail})", status)

    if status in _FALL_THROUGH_STATUSES:
        return Failure(provider, Disposition.FALL_THROUGH,
                       f"temporarily unavailable ({detail})", status)

    # A 400 usually means the request shape is wrong — but compatibility layers
    # also return 400 for an unsupported parameter, which IS provider-specific.
    # Treated as fall-through so an unsupported `response_format` costs one
    # provider rather than the whole cycle.
    if status == 400:
        return Failure(provider, Disposition.FALL_THROUGH,
                       f"rejected the request ({detail})", status)

    if isinstance(exc, (TimeoutError,)) or "timeout" in name.lower():
        return Failure(provider, Disposition.FALL_THROUGH, f"timed out ({detail})")

    # A response that did not validate against the schema. Worth one retry here
    # — sampling varies — then the next provider, since another model may be
    # better at instruction-following.
    if isinstance(exc, ValueError):
        return Failure(provider, Disposition.RETRY,
                       f"response did not match the schema ({detail})")

    return Failure(provider, Disposition.FALL_THROUGH, f"{name}: {detail}", status)


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _is_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"APIConnectionError", "ConnectError", "ConnectTimeout",
                "ConnectionRefusedError", "ProxyError", "SSLError"}:
        return True
    if isinstance(exc, (ConnectionError, OSError)) and not _status_of(exc):
        return True
    # The agent proxy answers a policy denial as a 403 to CONNECT, which some
    # clients surface as a connection error carrying the text rather than a
    # status. Matched on the wire wording because that is all that reaches us.
    return "CONNECT" in str(exc) and "403" in str(exc)
