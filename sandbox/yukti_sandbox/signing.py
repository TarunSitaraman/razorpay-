"""Razorpay-compatible webhook signing.

Mirrors Razorpay's documented scheme exactly rather than inventing a convenient
one, because the point of the sandbox is that swapping it for the real thing is
a config change and not a rewrite:

  * HMAC-SHA256, **hex** encoded (not base64)
  * computed over the **raw request body bytes**, never a re-serialised dict
  * delivered in the ``X-Razorpay-Signature`` header
  * keyed with a webhook secret that is a distinct value from the API key/secret

Razorpay's own docs warn that the body must not be parsed or cast before
verification. That warning describes a real bug class: JSON round-tripping
changes key order and whitespace, so a signature computed over re-marshalled
bytes silently stops matching. Every function here takes and returns ``bytes``
so there is no place for that mistake to hide.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of the raw body."""
    if not isinstance(body, bytes):
        raise TypeError("sign() requires raw bytes, not a parsed payload")
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time signature check.

    ``compare_digest`` rather than ``==``: a byte-wise comparison returns early
    on the first mismatch, which leaks the correct prefix through timing and
    lets an attacker recover a valid signature byte by byte.
    """
    if not isinstance(body, bytes):
        raise TypeError("verify() requires raw bytes, not a parsed payload")
    return hmac.compare_digest(sign(body, secret), signature or "")
