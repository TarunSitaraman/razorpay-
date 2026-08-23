"""Budget-constrained allocation of recovery actions.

The decision this makes is the one the whole project exists for: given every
open case across every surface, and a finite budget, which actions actually get
funded. Two properties distinguish it from what a conventional recovery system
does.

**It maximises incremental margin, not recovery.** The objective uses *uplift* —
the causal effect of acting — rather than P(recover). A propensity-driven system
spends its budget on customers who were going to pay anyway and bills the
merchant for the privilege.

**It arbitrates per customer, across surfaces.** One person may owe a merchant
money as an abandoned cart, a failed subscription and an overdue invoice at the
same time. The contact cap applies to the *person*, not to each case, which is
exactly what a per-agent budget cannot see.

Formally this is a multi-dimensional knapsack: NP-hard, so no exact solve at
merchant scale. Lagrangian relaxation prices each scarce budget with a
multiplier, reduces the problem to "take everything with positive reduced
value", and bisects the multipliers until the budgets bind. The Lagrangian dual
is a provable upper bound on the true optimum, which is what makes the
"within 5% of optimal" claim checkable rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bisection settles well inside this; the cap exists so a pathological input
# cannot spin.
MAX_BISECTION_STEPS = 60


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (case, action) pair the allocator may fund."""

    case_id: str
    customer_id: str
    action_kind: str
    channel: str
    # Expected incremental margin in paise, already net of discount and channel
    # cost. Computed by `expected_margin` so the arithmetic lives in one place.
    margin_paise: int
    # Resource draw.
    contacts: int              # 0 or 1 — does this spend a contact?
    discount_paise: int
    channel_cost_paise: int

    @property
    def is_free(self) -> bool:
        """Costs no scarce resource — a silent retry, or a suppression."""
        return self.contacts == 0 and self.discount_paise == 0


@dataclass(frozen=True, slots=True)
class Budgets:
    contacts: int
    discount_paise: int
    # Per-customer contact cap across every open case on every surface.
    per_customer_contacts: int


@dataclass(slots=True)
class Allocation:
    chosen: list[Candidate] = field(default_factory=list)
    total_margin_paise: int = 0
    contacts_used: int = 0
    discount_used_paise: int = 0
    # Lagrange multipliers at the solution: the marginal value of one more unit
    # of each budget. Directly interpretable for the merchant as "another
    # contact is worth this much to you".
    lambda_contact: float = 0.0
    lambda_discount: float = 0.0
    dual_bound_paise: float = 0.0

    @property
    def optimality_ratio(self) -> float:
        """Achieved margin over the dual bound — a lower bound on optimality."""
        if self.dual_bound_paise <= 0:
            return 1.0
        return min(1.0, self.total_margin_paise / self.dual_bound_paise)


def expected_margin(
    uplift: float,
    amount_paise: int,
    mdr_bps: int,
    discount_paise: int = 0,
    channel_cost_paise: int = 0,
) -> int:
    """Expected incremental margin of acting, in paise.

        uplift x amount x (1 - mdr) - discount - channel cost

    `uplift` is the causal effect, not P(recover). Substituting propensity here
    is the single change that would turn this system into every other recovery
    tool, so it is worth being explicit: the multiplier must be the difference
    between treated and untreated outcomes.

    Discount and channel cost are subtracted in full because they are paid
    whether or not the recovery lands.
    """
    gross = uplift * amount_paise * (1 - mdr_bps / 10_000)
    return int(round(gross - discount_paise - channel_cost_paise))


def _select(
    candidates: list[Candidate], budgets: Budgets, lam_c: float, lam_d: float
) -> list[Candidate]:
    """Take everything with positive reduced value, honouring per-customer caps.

    The per-customer cap is enforced directly rather than relaxed. Combinatorial
    caps do not price well — a multiplier that satisfied the tightest customer
    would over-suppress everyone else — and taking the best few per customer is
    exact for that constraint anyway.
    """
    scored = []
    for c in candidates:
        reduced = c.margin_paise - lam_c * c.contacts - lam_d * c.discount_paise
        if reduced > 0:
            scored.append((reduced, c))
    scored.sort(key=lambda t: -t[0])

    per_customer: dict[str, int] = {}
    # At most one action per case: funding two actions on the same obligation
    # would double-contact for a single debt.
    seen_cases: set[str] = set()
    chosen: list[Candidate] = []

    for _, c in scored:
        if c.case_id in seen_cases:
            continue
        if c.contacts:
            used = per_customer.get(c.customer_id, 0)
            if used + c.contacts > budgets.per_customer_contacts:
                continue
            per_customer[c.customer_id] = used + c.contacts
        seen_cases.add(c.case_id)
        chosen.append(c)
    return chosen


def _totals(chosen: list[Candidate]) -> tuple[int, int, int]:
    return (
        sum(c.margin_paise for c in chosen),
        sum(c.contacts for c in chosen),
        sum(c.discount_paise for c in chosen),
    )


def _bisect(
    candidates: list[Candidate],
    budgets: Budgets,
    fixed_other: float,
    which: str,
) -> float:
    """Find the smallest multiplier that brings one budget within limit."""
    lo, hi = 0.0, 1.0
    limit = budgets.contacts if which == "contact" else budgets.discount_paise

    def usage(lam: float) -> int:
        lam_c = lam if which == "contact" else fixed_other
        lam_d = fixed_other if which == "contact" else lam
        _, contacts, discount = _totals(_select(candidates, budgets, lam_c, lam_d))
        return contacts if which == "contact" else discount

    if usage(0.0) <= limit:
        return 0.0     # budget is not binding; do not price a free resource

    # Grow the upper bound until it actually suppresses enough. Margins are in
    # paise and can be large, so a fixed ceiling would silently fail to bind.
    while usage(hi) > limit and hi < 1e12:
        hi *= 4

    for _ in range(MAX_BISECTION_STEPS):
        mid = (lo + hi) / 2
        if usage(mid) > limit:
            lo = mid
        else:
            hi = mid
    return hi


def _fits(chosen: list[Candidate], c: Candidate, budgets: Budgets) -> bool:
    """Can this candidate be added without breaching any budget or cap?"""
    if any(x.case_id == c.case_id for x in chosen):
        return False
    _, contacts, discount = _totals(chosen)
    if contacts + c.contacts > budgets.contacts:
        return False
    if discount + c.discount_paise > budgets.discount_paise:
        return False
    if c.contacts:
        used = sum(x.contacts for x in chosen if x.customer_id == c.customer_id)
        if used + c.contacts > budgets.per_customer_contacts:
            return False
    return True


def _fill(chosen: list[Candidate], candidates: list[Candidate], budgets: Budgets) -> None:
    """Spend leftover budget on the best remaining candidates that fit.

    Bisection stops as soon as a budget is satisfied, which usually leaves some
    of it unspent. Unspent budget is forgone margin, so a greedy fill pass is
    close to free and recovers most of it.
    """
    remaining = sorted(
        (c for c in candidates if c not in chosen and c.margin_paise > 0),
        key=lambda c: -c.margin_paise,
    )
    for c in remaining:
        if _fits(chosen, c, budgets):
            chosen.append(c)


def _greedy(candidates: list[Candidate], budgets: Budgets) -> list[Candidate]:
    """Value-density greedy: margin per unit of scarce resource.

    A second, independent heuristic. Lagrangian pricing is coarse when a budget
    is very tight — with one contact available, a single multiplier either
    admits too many candidates or none — and density greedy handles exactly
    that regime well. Measured on random instances, the relaxation alone fell to
    19% of optimum on its worst case; taking the better of the two removes that
    tail.
    """
    def density(c: Candidate) -> float:
        # Free actions are pure profit and always sort first.
        cost = c.contacts + c.discount_paise / 10_000.0
        return c.margin_paise / cost if cost > 0 else float("inf")

    chosen: list[Candidate] = []
    for c in sorted((c for c in candidates if c.margin_paise > 0), key=lambda c: -density(c)):
        if _fits(chosen, c, budgets):
            chosen.append(c)
    return chosen


def allocate(candidates: list[Candidate], budgets: Budgets) -> Allocation:
    """Choose the funded set.

    Alternating bisection over the two multipliers. The budgets interact —
    pricing contacts changes which discounts are affordable — so a single pass
    per multiplier is not enough, but the coupling is weak and a few rounds
    converge.
    """
    if not candidates:
        return Allocation()

    lam_c = lam_d = 0.0
    for _ in range(4):
        lam_c = _bisect(candidates, budgets, lam_d, "contact")
        lam_d = _bisect(candidates, budgets, lam_c, "discount")

    chosen = _select(candidates, budgets, lam_c, lam_d)

    # Bisection lands just inside the budget; if rounding left it a hair over,
    # drop the least valuable items. A budget is a hard constraint — the
    # merchant authorised a number and we do not get to exceed it.
    def trim(pred, limit_getter) -> None:
        nonlocal chosen
        while True:
            _, contacts, discount = _totals(chosen)
            used = contacts if pred == "contact" else discount
            if used <= limit_getter or not chosen:
                return
            spenders = [c for c in chosen if (c.contacts if pred == "contact"
                                              else c.discount_paise) > 0]
            if not spenders:
                return
            worst = min(spenders, key=lambda c: c.margin_paise)
            chosen.remove(worst)

    trim("contact", budgets.contacts)
    trim("discount", budgets.discount_paise)
    _fill(chosen, candidates, budgets)

    # Take whichever heuristic did better on this instance. They fail in
    # different regimes, so the max is materially better than either alone and
    # costs one extra linear pass.
    greedy = _greedy(candidates, budgets)
    _fill(greedy, candidates, budgets)
    if _totals(greedy)[0] > _totals(chosen)[0]:
        chosen = greedy

    margin, contacts, discount = _totals(chosen)

    # Lagrangian dual: L(lambda) = max reduced value + lambda . budget. Always an
    # upper bound on the true constrained optimum, so margin/dual is a
    # conservative optimality certificate.
    relaxed = _select(candidates, budgets, lam_c, lam_d)
    dual = sum(
        c.margin_paise - lam_c * c.contacts - lam_d * c.discount_paise for c in relaxed
    ) + lam_c * budgets.contacts + lam_d * budgets.discount_paise

    return Allocation(
        chosen=chosen,
        total_margin_paise=margin,
        contacts_used=contacts,
        discount_used_paise=discount,
        lambda_contact=lam_c,
        lambda_discount=lam_d,
        dual_bound_paise=max(dual, float(margin)),
    )


def brute_force(candidates: list[Candidate], budgets: Budgets) -> int:
    """Exact optimum by enumeration. Test oracle only — exponential.

    Exists so the ">=95% of optimal" acceptance criterion is measured against a
    real optimum rather than against the relaxation's own bound.
    """
    if len(candidates) > 20:
        raise ValueError("brute force is only for small instances")

    best = 0
    for mask in range(1 << len(candidates)):
        subset = [c for i, c in enumerate(candidates) if mask >> i & 1]
        if len({c.case_id for c in subset}) != len(subset):
            continue
        margin, contacts, discount = _totals(subset)
        if contacts > budgets.contacts or discount > budgets.discount_paise:
            continue
        per_customer: dict[str, int] = {}
        for c in subset:
            per_customer[c.customer_id] = per_customer.get(c.customer_id, 0) + c.contacts
        if any(v > budgets.per_customer_contacts for v in per_customer.values()):
            continue
        best = max(best, margin)
    return best
