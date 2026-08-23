"""Disk cache for structured LLM responses.

Two jobs, and the second is the one that matters for a demo.

**Cost.** Free tiers have small rate limits. The composer in particular is
called once per (action_kind, channel, language) — never per case, because
message bodies carry `{amount}` and `{link}` placeholders that code fills in
afterwards. That makes composition O(templates), not O(cases), and a cache turns
the second demo run into zero API calls.

**Reproducibility.** A demo that calls a model live is a demo that can fail in
front of an audience: rate limits, latency, a provider having a bad afternoon.
Cached responses replay exactly, and because the key covers the prompt and the
schema, a changed prompt correctly misses rather than silently serving a stale
answer.

The key deliberately excludes the provider and the model. A cached answer is
keyed on the QUESTION, so switching providers or re-ordering the chain does not
invalidate work already done — which is the behaviour you want when the whole
point of the chain is that any provider can serve any call. The provider that
actually answered is stored in the payload, so the audit trail stays honest
about where a narrative came from.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from dataclasses import dataclass
from typing import Any

DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "llm-cache"


@dataclass(frozen=True, slots=True)
class CachedResponse:
    payload: dict[str, Any]
    provider: str
    model: str
    cached_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.cached_at


def key_for(system: str, prompt: str, schema_name: str, tier: str) -> str:
    """Hash the question, not the answerer. See the module note."""
    material = json.dumps(
        {"system": system, "prompt": prompt, "schema": schema_name, "tier": tier},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.blake2b(material, digest_size=16).hexdigest()


class ResponseCache:
    def __init__(self, directory: pathlib.Path | None = None, enabled: bool = True) -> None:
        self.dir = directory or DEFAULT_DIR
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> pathlib.Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            raw = json.loads(path.read_text())
            self.hits += 1
            return CachedResponse(
                payload=raw["payload"], provider=raw.get("provider", "unknown"),
                model=raw.get("model", "unknown"), cached_at=raw.get("cached_at", 0.0),
            )
        except (OSError, ValueError, KeyError):
            # A corrupt entry is a miss, not an error. The cost of being wrong
            # is one API call; the cost of raising is a failed demo.
            self.misses += 1
            return None

    def put(self, key: str, payload: dict[str, Any], provider: str, model: str) -> None:
        if not self.enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"payload": payload, "provider": provider, "model": model,
                 "cached_at": time.time()},
                indent=2, default=str,
            ))
            # Atomic replace: a crash mid-write must not leave a half-written
            # entry that reads as valid JSON with missing fields.
            tmp.replace(self._path(key))
        except OSError:
            pass   # a cache that cannot write is still a working system

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses,
                "entries": len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0}

    def clear(self) -> int:
        if not self.dir.exists():
            return 0
        removed = 0
        for path in self.dir.glob("*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed
