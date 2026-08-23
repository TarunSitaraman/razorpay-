"""Wrapping text the merchant or customer controls.

Decline strings, order notes and support messages all reach the model, and all
of them are attacker-controlled in a real deployment. The generator already
produces deliberately messy decline text for this reason.

The defence here is *framing*, and it is the weaker of the two layers by
design. Untrusted text goes inside a delimited envelope with an explicit
instruction that it is data; it is never concatenated into the instruction
itself. That reduces how often an injection lands.

The layer that actually holds is `dispatch/tools.py`, which has no refund
function. A model fully persuaded by an injection still has nothing to call. So
this module is defence in depth, not the defence — and it is worth being clear
about which is which, because a system whose only protection is prompt framing
is one convincing paragraph away from a payout.
"""

from __future__ import annotations

import re

# Sequences a payload would use to look like it is closing our envelope and
# opening a new instruction. Neutralised rather than removed, so an operator
# reading the audit trail can still see what was attempted.
_ESCAPE_PATTERNS = (
    (re.compile(r"</?untrusted[^>]*>", re.I), "[tag-removed]"),
    (re.compile(r"</?(system|assistant|human)[^>]*>", re.I), "[tag-removed]"),
)

MAX_LEN = 2000


def envelope(label: str, text: str | None, max_len: int = MAX_LEN) -> str:
    """Wrap untrusted text so it reads as data rather than instruction."""
    if not text:
        return f"<untrusted_{label}>(none)</untrusted_{label}>"

    cleaned = str(text)[:max_len]
    for pattern, replacement in _ESCAPE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)

    return (
        f"<untrusted_{label}>\n"
        f"{cleaned}\n"
        f"</untrusted_{label}>"
    )


def evidence_block(rows: list[dict]) -> str:
    """Render evidence rows for a prompt, each tagged with its id.

    Ids are included so a specialist can cite them and so the citation can be
    checked against what it was actually shown. An RCA that cites `ev_7` when
    only `ev_1..ev_3` were provided has invented a source, and that is
    detectable rather than merely implausible.
    """
    if not rows:
        return "<evidence>(no evidence gathered)</evidence>"
    lines = [
        f"  [id={r['id']}] source={r['source']} subject={r.get('subject') or '-'} "
        f"fact={r['fact']}"
        for r in rows
    ]
    return "<evidence>\n" + "\n".join(lines) + "\n</evidence>"
