# Niyama (Internal codename: Yukti)

**Razorpay Buildathon · Track 03 — AI Revenue Recovery**

Niyama is an arbitration layer that sits above revenue recovery agents (failed payments, abandoned checkouts, subscriptions). Rather than maximizing gross recovery, it bounds workflows using a Lagrangian knapsack allocator. 

It optimizes for net incremental margin under strict Indian payment regulations (RBI, NPCI, TRAI) and merchant budgets. The system spends most of its compute proving which customers *should not* be contacted.

---

## 🏗️ Implementation Details

This is an event-driven, four-plane distributed system built for exactly-once execution. It is not an LLM wrapper.

### 1. Architecture
1. **Edge (Go)**: A stateless webhook ingest gateway. Handles HMAC verification, replay protection, and async Kafka writes. 
2. **Control (Python)**: The core intelligence layer. Consumes from Kafka (with replayability). Houses the allocator, uplift models, and a transactional outbox.
3. **Policy (In-Process)**: A deterministic compiled rule engine (RegPack & MerchantPack) evaluated pre-planning and pre-dispatch.
4. **Measurement (Offline)**: A holdout-based causal estimator to compute true incremental lift.

### 2. The Lagrangian Allocator
Current industry tools limit budgets per agent. Niyama arbitrates globally across all agents. 
It maximizes `Σ (uplift × amount × (1 − mdr)) − discount − channel_cost` subject to daily merchant budgets and NPCI fatigue caps. The problem is solved via Lagrangian relaxation plus a density-greedy alternative, a fill pass and a windowed exchange pass — matching the exact optimum on all 400 enumerable test instances, with a tested dual certificate standing in at production scale. Scarce resources are never wasted on unrecoverable cases.

### 3. Event Correctness
- **Exactly-Once Effect**: Four layers of deduplication (HMAC -> Redis TTL -> Postgres Unique Index -> Derived Idempotency Ledger) ensure a duplicated webhook never causes a double charge.
- **Transactional Outbox**: Decisions commit to Postgres in a single ACID transaction before a relay publishes to Kafka.
- **Idempotent Replay**: Webhook replays push asynchronously to Kafka without blocking on ISR acks.

### 4. The LLM Boundary
The LLM acts strictly as a proposer. It never decides amounts, policy verdicts, or initiates refunds. 
It performs root-cause analysis on retrieved evidence, classifies free-text decline reasons, and interpolates DLT-compliant channel copy. Its output schema only allows it to *remove* action candidates.

### 5. Batch and Streaming Allocation
A batch knapsack is the right shape for an overdue invoice and the wrong one for an abandoned cart, where intent decays in minutes. The Lagrangian solve already yields the fix: `lambda_contact` and `lambda_discount` are *prices*, and a price turns a combinatorial decision into a local one. `allocator/streaming.py` computes the shadow price offline and admits events in real time — no solver in the request path.

Measured: **99.87% of batch margin at 1.6 µs/event**, with named refusal reasons (`below_price`, `budget_exhausted`, `customer_capped`) and a `utilisation` signal for price staleness. Online has no hindsight, so it *cannot* beat batch; `admission_gap` reports the shortfall rather than asserting it is small.

### Scaffolding, named as such
Two items are wired end-to-end but are not load-bearing, and are listed here rather than beside the allocator so the capability list stays honest:

- **OpenTelemetry** — FastAPI, HTTPX and SQLAlchemy are instrumented with the standard SDK, exporting to `ConsoleSpanExporter`. That is a seam for OTLP, not observability.
- **MCP server** — `control/yukti/mcp_server.py` exposes revenue-at-risk, pipeline counts, stopping rules and policy blocks as read-only tools; nothing on that surface can act. Useful, peripheral to the thesis.

---

## 📊 Evaluation Results

### What is being claimed, and how strongly

This project is evaluated inside a simulator it also wrote. That is stated first
because it determines how much any number below is worth, and because the
defences usually offered for it — the latent archetype is never a feature,
treatment was randomised — answer a narrower objection than the one a reviewer
should raise. They establish the learner did not *cheat*. They cannot establish
that the structure it learned exists in real Indian payment data.

So the results come in three tiers, strongest first:

| Tier | What it establishes | Depends on the simulator? |
|---|---|---|
| **Mechanism** | The allocator matches the exact optimum on every enumerable instance, budgets are never breached, holdouts are never treated, the audit chain detects tampering. | No — properties, checked against exact enumeration and mutation tests. |
| **Frontier** | *Which assumptions about customers the uplift thesis needs in order to pay.* | The simulator is the instrument, not the evidence. |
| **Headline** | Uplift beat propensity by ₹4.05L on one 3,475-case book. | **Yes, entirely.** |

The headline is the weakest of the three and is presented last for that reason.

---

### Tier 1 — Mechanism

| The Bar | How We Met It | Status |
|---|---|---|
| **Measured money** | Holdout incremental ₹ with 95% CIs, plus the sample size required for statistical power. | ✅ |
| **Across a batch** | Lagrangian allocator over every open case per planning window. | ✅ |
| **Compliant escalation** | RegPack: RBI 24h pre-debit · ₹15k AFA limit · NPCI attempt caps · TRAI 9–21h quiet hours · DPDP consent. On the NBFC book the AFA ceiling refuses the allocator's first-choice action on 1,759 decisions covering ₹5.34 Cr (2,160 and ₹8.72 Cr across all merchants), and the console names the rule that did it. | ✅ |
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

Worth volunteering: the suite previously asserted `>= 0.95` over 60 seeds and
passed, while the *same generator* extended to 400 seeds contained an instance at
**0.944**. The acceptance criterion had been fitted to its sample. A windowed
exchange pass closes it; the widened range is what stops it reopening.

---

### Tier 2 — The assumption frontier

The honest reply to *"you built a world where you win"* is not a defence, it is a
sweep. `python -m yukti.eval.cli sensitivity` varies each load-bearing assumption
across a plausible range and reports **where Niyama stops winning** — refitting
the model at every grid point, because the question is whether uplift arbitration
pays given that you fitted it in that world, which is the position a deployment
is actually in.

It needs no database and no services: the world is generated, explored, learned
and graded in process, so the frontier is reproducible from a clean clone in
about a minute. Every component in the path is the production component — the
same oracle, the same feature frame *including its leakage guard*, the same
X-learner, the same allocator, the same bootstrap.

**The frontier** (`artifacts/sensitivity.json`, regenerate with `make sensitivity`).
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
power analysis in [EVALUATION.md](docs/EVALUATION.md) §3 predicts it must. The
signal is the *shape* across an axis and the targeting counts, not any single cell.
A frontier is evidence about direction and boundaries; it is not a p-value.

The frontier is the defensible claim. **The headline result is one point on it.**

---

### Tier 3 — The headline (NBFC lending book, 3,475 cases)

| Arm | Contacts | Recovered | Contact-attributable ₹ | Per 1k (95% CI) |
|---|--:|--:|--:|---|
| Fixed Cadence | 86 | 1,158 | −1,31,998 | −37,985 [−104,025, +25,003] |
| Propensity Only | 89 | 1,160 | −43,638 | −12,558 [−59,323, +24,368] |
| **Niyama (uplift)** | **88** | **1,172** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

Niyama spends the same budget as naive approaches and is the only arm whose
interval excludes zero. Note that propensity **recovers more cases than it is
paid for** — the divergence between gross recovery and incremental margin is the
entire product.

**Two disclosures that belong beside this table, not below it.**

**That confidence interval is narrower than reality permits.** It is a paired
bootstrap against the oracle's *known* counterfactual — the one term a production
estimator can never observe. It captures customer heterogeneity, not
counterfactual uncertainty.

**At this sample size, a real deployment could not tell these arms apart.** The
true per-case effect is ₹315 against a per-case spread of ₹12,870 — an effect
size of **0.024σ**, needing **136,887 cases** for 80% power against the 3,475
available, a 39× shortfall. That is a property of the data (a Bernoulli draw times
a heavy-tailed amount), not of the estimator, and no estimator fixes it. It is
also the strongest argument in the repository: honest incrementality measurement
has to pool across merchants, which makes federated inference a
statistical-power necessity rather than a privacy nicety.

Full working, including the arms that lost and the segment where the technique is
close to irrelevant, in [EVALUATION.md](docs/EVALUATION.md).

---

## 🚀 Running It Locally

The local stack runs natively (Kafka 4.3.1 in KRaft mode, system Postgres 16, Redis). No Docker daemon is required.

```bash
# 1. Setup your environment
cp .env.example .env
# Optional: Add an LLM key in .env. 

# 2. Run the full demo (Cold clone -> Seed -> Train -> Plan -> Eval)
make demo      

# 3. View the console
open http://localhost:8080

# 4. Run the 1,276-test suite
make test      
```

No Kafka, no Postgres, no Redis? `make demo-light` runs the assumption sweep in
process and writes the console a service-free result bundle, so the measured
comparison is viewable from a cold clone in about a minute. The console labels
it as the simulation it is, and the panels that genuinely need the stack say so
rather than rendering blank.


*(See [docs/DEMO.md](docs/DEMO.md))*

---

## 🧠 Documentation

| File | Description |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The four planes, the exact LLM boundary, event idempotency, and the decision path. |
| [EVALUATION.md](docs/EVALUATION.md) | The comparison arms, the headline result, and what honest lift measurement costs. |
| [RESEARCH.md](docs/RESEARCH.md) | Every claim about Razorpay, Indian regulation, and the competitive market, sourced. |
| [INTERVIEW.md](docs/INTERVIEW.md) | The hardest questions answered coldly—including a self-critique of the math. |
| [DEMO.md](docs/DEMO.md) | The pitch script and walkthrough. |

---

## 📂 Layout

```text
edge/        Go — webhook ingest, producer
control/     Python — domain, uplift intelligence, allocator (batch + streaming), policy engine, dispatcher, eval, mcp_server
sandbox/     Razorpay-contract-shaped simulator
datagen/     Synthetic world generator
infra/       Terraform + Kubernetes manifests
tests/       1,276 tests
```
