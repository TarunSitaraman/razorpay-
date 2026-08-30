# Evaluation Summary: Niyama (Yukti), Razorpay Buildathon Track 03

This project is evaluated inside a simulator it also wrote. That determines how
much any number below is worth, and the defences usually offered for it — the
latent archetype is never a feature, treatment was randomised — answer a narrower
objection than the one a reviewer should raise. They establish the learner did
not *cheat*. They cannot establish that the structure it learned exists in real
Indian payment data.

So the results come in three tiers, strongest first:

| Tier | What it establishes | Depends on the simulator? |
|---|---|---|
| **Mechanism** | The allocator matches the exact optimum on every enumerable instance, budgets are never breached, holdouts are never treated, the audit chain detects tampering. | No — properties, checked against exact enumeration and mutation tests. |
| **Frontier** | *Which assumptions about customers the uplift thesis needs in order to pay.* | The simulator is the instrument, not the evidence. |
| **Headline** | Uplift beat propensity by ₹3.61L on one 3,475-case book. | **Yes, entirely.** |

---

## Tier 1 — Mechanism (verified)

| The Bar | How We Met It | Status |
|---|---|---|
| **Measured money** | Holdout incremental ₹ with 95% CIs, plus the sample size required for statistical power. | ✅ |
| **Across a batch** | Lagrangian allocator over every open case per planning window. | ✅ |
| **Compliant escalation** | RegPack: RBI 24h pre-debit · ₹15k AFA limit · NPCI attempt caps · TRAI 9–21h quiet hours · DPDP consent. | ✅ |
| **Stopping rules** | Named `StopReason` on every stop; the console explicitly groups the money deliberately not chased. | ✅ |
| **Audit trail** | Append-only hash-chained `audit_event` per merchant. Tamper tests detect edits/deletions. | ✅ |
| **Bounded workflow** | `ActionKind` schema omits refund/payout/mandate-cancel entirely at the type level. | ✅ |

**Allocation quality**, measured against brute-force enumeration over 400 random
instances — not against the relaxation's own bound, which would flatter it:
mean **1.0000**, worst **1.0000**, zero budget violations. At production scale
there is no optimum to enumerate, so the quotable number is the dual
certificate, which reports ≥ 0.9999 and is itself tested
(`tests/unit/test_allocator_certificate.py`) for the two properties that make it
meaningful: the bound never falls below the true optimum, and the ratio never
overstates the true one.

**Key components** (all code, no hand-waving):
- **Lagrangian allocator** (`control/yukti/allocator/lagrangian.py`): dual bisection,
  density-greedy fill, windowed exchange pass (`_improve`).
- **Dual-bound certificate** (`tests/unit/test_allocator_certificate.py`): weak duality
  checked against exhaustive enumeration (`brute_force`), never below the true optimum,
  certified ratio never overstates the true one.
- **Four-layer dedup** (HMAC → Redis TTL → Postgres unique index → idempotency ledger):
  verified live — re-publishing the full event stream produced 17,550 duplicates
  detected and **0 duplicate cases**.
- **Costless-action rule** (`control/yukti/allocator/lagrangian.py`): silent retries
  funded on `sign(uplift)` alone, because they consume no scarce resource.
- **Transactional outbox** (Control plane): decision and action-intent commit in one
  Postgres ACID transaction before a relay publishes to Kafka.

**1093 unit / agent / eval tests pass** without external services.

---

## Tier 2 — The Assumption Frontier (swept, retrains at every grid point)

The honest reply to *"you built a world where you win"* is not a defence, it is a
sweep. `python -m yukti.eval.cli sensitivity` varies each load-bearing assumption
across a plausible range and reports **where Niyama stops winning** — refitting the
model at every grid point, because the question is whether uplift arbitration pays
given that you fitted it in that world, which is the position a deployment is
actually in.

It needs no database and no services: the world is generated, explored, learned
and graded in process, so the frontier is reproducible from a clean clone in
about a minute. Every component in the path is the production component — the
same oracle, the same feature frame *including its leakage guard*, the same
X-learner, the same allocator, the same bootstrap.

**The frontier** (`artifacts/sensitivity.json`, regenerate with `make sensitivity`):
Contact-attributable margin per cycle, measured against retry-only so the free-retry
mass every arm shares cancels out and only the contact decision remains:

| assumption swept | headline assumes | Niyama stops winning at | who wins past it |
|---|--:|--:|---|
| `persuadable_uplift` — headroom on the only profitable archetype | 0.46 | **below ≈ 0.097** | nobody: stop contacting |
| `sure_thing_uplift` — headroom on customers who pay anyway | 0.04 | **above ≈ 0.162** | propensity |
| `sleeping_dog_share` — share the contact actively harms | 0.15 | **below ≈ 0.019** | fixed cadence |
| `silent_retry_irritation` — opt-out risk of an "invisible" retry | 0.0 | no crossover in range | — |
| `fatigue_decay` — response decay per prior contact | 0.78 | no crossover in range | — |

Read plainly, that says: **the uplift objective needs persuadables to have at least
~10 points of headroom, and needs sure-things to have less than ~16.** Outside that
band it is not worth the complexity, and **three of the five axes contain a point
where this system loses** — to propensity, to fixed cadence, and to not contacting
anyone at all. Those rows are the reason to believe the others.

`sure_thing_uplift` is the one to sit with. It is not a robustness quibble — it is
the thesis's own logic running in reverse. If customers who would have paid anyway
*also* respond strongly to contact, then P(recover | treated) and uplift rank the
same people and the causal machinery buys nothing. Propensity wins past 0.162
because past 0.162 propensity is *right*.

**The mechanism, which is far more stable than the money.** Ground-truth archetype
of who each arm actually spent its 200 contacts on, in the default world:

| arm | persuadable | sure thing | lost cause | sleeping dog |
|---|--:|--:|--:|--:|
| fixed cadence | 42 | 69 | 54 | 35 |
| propensity only | **3** | 168 | 0 | 29 |
| **Niyama (uplift)** | **66** | 96 | 17 | 21 |

Propensity spends 168 of 200 contacts on customers who were going to pay anyway and
reaches **three** persuadables. That is the entire product thesis as a count rather
than as a claim, and unlike the rupee columns it does not move with the seed.

**What the frontier does not establish.** Each individual grid point is
under-powered — Niyama's bootstrap CI at the default world is
[−6,35,261, +22,22,373] per 1,000 and comfortably contains zero, exactly as the
power analysis in EVALUATION.md §3 predicts it must. The signal is the *shape*
across an axis and the targeting counts, not any single cell. A frontier is
evidence about direction and boundaries; it is not a p-value.

The frontier is the defensible claim. **The headline result is one point on it.**

---

## Tier 3 — The Headline (NBFC lending book, 3,475 cases)

| Arm | Contacts | Recovered | Contact-attributable ₹ | Per 1k (95% CI) |
|---|--:|--:|--:|---|
| Fixed Cadence | 86 | 1,158 | −1,31,998 | −37,985 [−104,025, +25,003] |
| Propensity Only | 89 | 1,160 | −43,638 | −12,558 [−59,323, +24,368] |
| **Niyama (uplift)** | **88** | **1,172** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

**Two disclosures that belong beside this table, not below it.**

**That confidence interval is narrower than reality permits.** It is a paired
bootstrap against the oracle's *known* counterfactual — the one term a production
estimator can never observe. It captures customer heterogeneity, not
counterfactual uncertainty.

**At this sample size, a real deployment could not tell these arms apart.** The
true per-case effect is ₹315 against a per-case spread of ₹12,870 — an effect
size of **0.024σ**, needing **136,887 cases** for 80% power against the 3,475
available, a 39× shortfall. That is a property of the data (a Bernoulli draw
times a heavy-tailed amount), not of the estimator, and no estimator fixes it. It
is also the strongest argument in the repository: honest incrementality measurement
has to pool across merchants, which makes federated inference a
statistical-power necessity rather than a privacy nicety.

Full working, including the arms that lost and the segment where the technique
is close to irrelevant, in [EVALUATION.md](docs/EVALUATION.md).

---

## How to Reproduce

### Mechanism (verified, no services needed)
```bash
make test            # 1093 unit/agent/eval tests pass in ~2 min
python -m yukti.eval.cli sensitivity  # service-free sweep
```

### Assumption Frontier (service-free, ~1 min)
```bash
make sensitivity     # all axes
python -m yukti.eval.cli sensitivity  # single axis
```

### Headline (requires local stack: Kafka + Postgres + Redis)
```bash
make demo            # Cold clone → Seed → Train → Plan → Eval
```

---

## Honesty Commitments

1. **Mechanism tier is proven** against exact enumeration, not extrapolated.
2. **Frontier tier varies every load-bearing assumption** and retrains the model at each grid point.
3. **Headline openly concedes** its CI is narrower than reality and that 136k cases
   (39×) are needed for 80% power — no estimator fixes this; it is a data property.
4. **Sensitivity axes are the five load-bearing assumptions** the thesis implicitly
   depends on; the sweep reports exactly where the thesis stops surviving.
5. **No claim is made that the headline generalizes** beyond the synthetic simulator
   — the frontier answers the only objection that matters.

---

## Quick Reference: Crossover Points

| Axis | Headline Assumption | Crossover (sign change) | Fails On |
|---|---|---|---|
| `persuadable_uplift` | 0.46 | **below ≈ 0.097** | nobody wins |
| `sure_thing_uplift` | 0.04 | **above ≈ 0.162** | propensity wins |
| `sleeping_dog_share` | 0.15 | **below ≈ 0.019** | fixed cadence wins |
| `silent_retry_irritation` | 0.0 | never (no crossover) | — |
| `fatigue_decay` | 0.78 | never (no crossover) | — |

---

## Quick Reference: Key Artifacts

| Artifact | Generated By | Description |
|---|---|---|
| `artifacts/sensitivity.json` | `make sensitivity` / `python -m yukti.eval.cli sensitivity` | Full sweep data for all 5 axes |
| `artifacts/demo-results.json` | `make demo-light` / `python gen_demo_results.py` | Service-free demo data for the console |
| `artifacts/eval-report.json` | `make eval` (requires local stack) | Full six-arm evaluation report |
| `tests/unit/test_allocator_certificate.py` | `pytest` | Dual-bound certificate: 250 instances, verified never below optimum |
| `tests/unit/test_sensitivity.py` | `pytest` | Sensitivity machinery: leak guard, axis construction, crossover sign change |