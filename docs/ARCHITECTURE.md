# ARCHITECTURE

Yukti is an agent that runs a bounded recovery workflow across a whole batch.
Everything below exists to answer one question: **how does an agent stay
trustworthy when it acts on thousands of cases at once, under regulation, with
someone else's money?**

The answer, structurally, is that the probabilistic parts propose and the
deterministic parts dispose — and the seam between them is enforced by the type
system and the schema rather than by discipline.

---

## The four planes, and why four

```
Payments Sandbox ──HMAC webhooks──▶ Edge (Go) ──▶ Kafka ──▶ Control Plane (Python)
                                                               │
   1 Opportunity   case FSM · dedup · versioning · promise-to-pay ledger
   2 Intelligence  transience · uplift · debit timing · downtime · degradation
   3 Allocator     Lagrangian knapsack over contact / discount budgets     ← deterministic
   4 Stopping      named rules: lost cause · open PTP · budget · NPCI cap  ← deterministic
   5 Agent         supervisor + RCA / planner / composer specialists       ← LLM only here
   6 Policy        RegPack + MerchantPack → ALLOW / BLOCK / ESCALATE       ← deterministic
   7 Dispatcher    idempotency ledger · transactional outbox               ← deterministic
                                                               │
                        bounded tool layer ──▶ sandbox ──▶ outcomes ──▶ loop
```

They are separated by **consistency and latency requirements**, not by taste:

| Plane | Why it is its own thing |
|---|---|
| **Edge — Go** | Untrusted, bursty, must never lose an event, must scale horizontally and cheaply. HMAC verification and replay protection belong at the boundary. Go because Razorpay's own MCP server is Go and their AI roles list it — the choice is defensible on their terms, not just on throughput. |
| **Control — Python** | Where models, optimisation and the agent live. Latency budget is seconds to minutes, not milliseconds. |
| **Policy — an in-process library** | A library rather than a service, deliberately: it is on the path of *every* action and must have **no network failure mode**. Versioned and separately unit-tested. |
| **Measurement — offline** | Analytical, replayable from the event log, never on the hot path. |

**Why Kafka and not a queue.** Partitioned ordering per `merchant_id`, and
**replay from offset**. Replay is not a nice-to-have: it is how the evaluation
re-runs the same cases across six arms deterministically. A queue destroys
messages on consumption; this needs the log.

---

## The seam: what the LLM does, and what it cannot

| The LLM does | It never does |
|---|---|
| Root-cause an aggregate degradation from retrieved evidence rows | Compute an amount to charge, refund or discount |
| Classify novel free-text decline reasons, clamped to a closed enum | Decide whether a policy is satisfied |
| Propose which action kinds *not* to fund for a cohort | Choose a holdout assignment |
| Compose channel copy, bound to a registered DLT template | Issue refunds or payouts |

Three mechanisms make that a guarantee rather than an intention:

**The output surface cannot widen anything.** `Advice` carries one effective
field: a set of action kinds to *remove*. There is nowhere to express "also try
X". If the agent is unavailable, wrong, or prompt-injected, the worst it
achieves is that Yukti considers fewer options and explains itself less well.

**Refunds are absent, not denied.** `ActionKind` has eight members and none is a
refund, payout, settlement or mandate-cancellation. The tool layer has no
function to call. That is a different and stronger claim than a runtime check,
and it is why the injection suite is close to free: there is nothing to invoke.

**Untrusted text is enveloped, never concatenated.** Decline text and order notes
are generated messy free text — the realistic injection vector — and are wrapped
in an untrusted-content envelope rather than interpolated into instructions.

**Nothing is load-bearing on a model.** With no credential configured, the
stopping rules, allocator, policy engine and dispatcher all run unchanged. This
is verified live rather than asserted: `yukti agent` on a genuinely detected
degradation reports `provenance: {'fallback': 2}` and withholds contact rather
than guessing. Every conclusion records whether it came from a model or a
fallback, because a fleet quietly running on defaults behaves plausibly and
looks identical to one that is working.

---

## Event correctness

The hardest problem here is **exactly-once effect under at-least-once
delivery**: a duplicated or out-of-order `payment.failed` must never produce a
second discount or a second debit attempt.

| Concern | Mechanism |
|---|---|
| Duplicate webhooks | HMAC verify → `event_id` seen-set in Redis (TTL) → Postgres unique index as backstop |
| Out-of-order events | Per-aggregate monotonic `version`; the FSM refuses backward transitions and records the late event as *superseded* rather than applying it. No distributed locking. |
| Exactly-once **effect** | Idempotency ledger on a **derived fingerprint** — `blake2b(merchant ‖ obligation ‖ action_kind ‖ channel ‖ scheduled_day ‖ discount)`. Derived rather than minted, so a replayed cycle collides on `recovery_action.idempotency_key UNIQUE` instead of dispatching twice. |
| Transactional side-effects | **Transactional outbox** — decision and action-intent commit in one Postgres transaction; a relay publishes to Kafka. No dual write. |
| Agent crash mid-plan | Runs are event-sourced; plans are *proposals*; nothing executes until the dispatcher commits. A crash can lose planning work, never emit a duplicate action. |
| Source of truth | Razorpay (here, the sandbox). Yukti never infers success from its own action succeeding — only from a correlated `payment.captured`. |

Four layers guard duplicates, and the ordering matters: the last one is the only
one that protects **money**. Verified live — re-publishing the full event stream
produced 17,550 duplicates detected and **0 duplicate cases**.

---

## The decision path

`plan_cycle` is a pure function of database state, so it is replayable and
crash-safe with no extra machinery.

```
load open cases  (arm=holdout → planned, then never acted on)
  → intelligence:  transience, uplift over case × candidate action, debit timing
  → stopping:      first-match-wins, named reason, persisted
  → candidates:    (case, action) pairs, generated deterministically
  → policy:        feasibility filter — never spend budget on what will be blocked
  → allocator:     allocate() under contact / discount / per-customer budgets
  → policy:        full evaluate before dispatch (defence in depth)
  → persist:       agent_decision + policy_evaluation + audit_event, one transaction
  → dispatch:      idempotent, through the outbox
```

**Policy is consulted twice on purpose.** Once as a feasibility filter so the
allocator never funds an action that will be blocked; once in full before
dispatch. The second is not redundancy — it is what catches a bug in the first,
and it is the one whose verdict is recorded.

**Stopping comes before allocation** because it is cheap, and because a stop is a
different *kind* of statement from a policy block. "We chose not to work this" is
a business decision; "we were not allowed to" is a compliance one. Collapsing
them would make the console unreadable and the stopping-rules metric meaningless
— and that metric is half of what the merchant is paying to learn.

**Three components, three failure modes:**

| Component | Question | How it fails |
|---|---|---|
| StoppingRules | Should we work this case at all? | Burns budget on the unrecoverable |
| Allocator | Which actions maximise net margin under budget? | Spends on sure things |
| PolicyEngine | Is this action permitted, right now, for this customer? | Regulatory breach |

### The allocator

Maximises `Σ uplift × amount × (1 − mdr) − discount − channel_cost` subject to a
per-merchant daily contact budget, a discount budget, and a **per-customer
contact cap across every open case on every surface**. That last constraint is
the cross-agent arbitration claim made concrete — it is the whole reason this
layer exists, since a per-agent budget cannot see a customer's total exposure.

Multi-dimensional knapsack, so exact solve is NP-hard. Lagrangian relaxation
prices each budget and bisects until they bind. The dual is a provable upper
bound, which makes the optimality claim checkable rather than asserted:

| | Lagrangian only | + greedy + fill |
|---|--:|--:|
| exact optimum | 89% | **99%** |
| mean ratio | 0.9731 | **0.9996** |
| **worst case** | **0.187** | **0.947** |
| budget violations | 0 | 0 |

The first cut looked fine on the average and was quietly catastrophic in the
tail. Taking the better of two heuristics costs one linear pass.

### The costless-action rule

One rule in the allocator is worth stating because it was arrived at the hard
way, and because it is the interview answer to "what did you get wrong?"

> A candidate with zero rupee cost that does not contact the customer is funded
> whenever the policy engine permits it, **without consulting its margin**.

For such an action, `expected_margin` reduces to `uplift × amount`, so funding
on `margin > 0` is funding on `sign(uplift)` alone. A silent retry cannot reach
the customer, so it has no downside branch — its true effect is small but never
negative. A point estimate hovering near zero therefore gets the sign wrong
roughly half the time, and the allocator was dropping ~892 free retries per
cycle on estimator noise.

The principle: **the allocator exists to ration scarce resources.** A costless,
invisible action is not scarce, so handing its adjudication to a noisy estimate
buys nothing and costs variance. Its one genuinely scarce resource is the NPCI
re-presentation attempt, and that is capped upstream by RegPack — a hard
regulatory limit, not an economic judgement, which is where it belongs.

The same mistake had been made in four places. The invariant is now asserted
once, over the rules as a set.

---

## Guardrails

**RegPack — not merchant-disableable.** `RBI_PREDEBIT_24H`, `RBI_AFA_LIMIT`
(₹15k, ₹1L for exempt categories), `NPCI_REPRESENT_CAP` (per reason code, from
the shared decline table so the classifier and the guardrails cannot drift),
`TRAI_QUIET_HOURS`, `TRAI_DLT_TEMPLATE`, `DPDP_CONSENT`. Citations in
[RESEARCH.md](RESEARCH.md) §2.

**MerchantPack — configurable, and only ever more restrictive.** Contact caps,
discount ceilings and stacking, blackout periods, minimum obligation value, and
approval thresholds above which the verdict is `ESCALATE` rather than `ALLOW`.

Two precedence rules, both tested by trying to violate them:

- **BLOCK outranks ESCALATE.** An illegal above-threshold action must not reach a
  human, because approving it leaves it illegal.
- **RegPack outranks MerchantPack.** Verified by firing all six regulatory
  violations at a maximally permissive merchant configuration.

Adversarial suite: 10 locally optimal actions, **zero escapes**.

Every evaluation writes a `policy_evaluation` row with pack, rule id, verdict and
reason, so the console can show *"the agent wanted this; this rule stopped it"* —
and on the NBFC book it does, unprompted: most obligations exceed the ₹15,000
AFA ceiling, so auto-debit is unavailable and a payment link is the only move
left. The regulation is visibly deciding the action.

**The audit chain** is append-only and hash-chained per merchant:
`hash = H(prev_hash ‖ canonical row)`. `audit.verify` walks it; tamper tests
detect edits and deletions separately. Truncation is whole-merchant rather than
row-level, because deleting from the middle is exactly what the chain detects,
and leaving a knowingly-broken chain behind would train us to ignore the one
signal that matters.

---

## Technology choices, and what was rejected

| Layer | Choice | Reason |
|---|---|---|
| Edge | **Go 1.24**, franz-go | Pure Go, so `CGO_ENABLED=0` gives a static binary. `confluent-kafka-go` wraps librdkafka and needs cgo — a C toolchain problem in the one component that must scale horizontally. |
| Control | **Python 3.11 + FastAPI + Pydantic** | Models, optimiser, agent and evaluation all live here. Pydantic gives typed structured output from the LLM. Keeps `confluent-kafka` — different constraints, different choice, and both are defensible. |
| Bus | **Kafka 4.3.1, KRaft, single node** | Replay from offset. Runs from the Apache tarball on Java 21 with **no Docker daemon**. |
| OLTP | **PostgreSQL 16** | Outbox, idempotency ledger, audit chain, agent event store. Needs real ACID. |
| Cache | **Redis 7** | Fatigue counters with TTL, rate limits, dedup set, dispatch locks. |
| Console | **Server-rendered on the existing FastAPI app** | No Node toolchain, no build step. A build that must work from a cold clone is exactly what fails during a demo. |
| Agent | **Anthropic SDK + a custom event-sourced state machine**, not LangGraph | The agent's state machine *is* a Kafka consumer with Postgres checkpoints; a framework checkpointer would be a second, competing source of truth. Razorpay uses LangGraph for Viveka, so this is a deliberate departure — while copying Viveka's supervisor + specialists + evidence-in-memory shape. |

**Explicitly rejected:** a vector DB (policies are structured; nothing needs
semantic retrieval), Spark/Trino locally (DuckDB is the right laptop-scale
analogue and the production mapping is articulable), Kubernetes deployment (no
daemon; manifests only), microservice-per-domain (four planes, not fifteen
services).

**Provider-agnostic LLM layer.** Every free provider worth having speaks the
OpenAI chat-completions wire format, so there is one adapter plus a table rather
than nine integrations. Providers are tried in order; one with no key is skipped
**without opening a socket** (a nine-provider walk costs 1.8s, and a test asserts
zero sockets opened); one that fails permanently is dropped for the rest of the
process by a circuit breaker rather than re-tried on every call. Structured
output degrades in three rungs — `json_schema`, `json_object`, schema-in-prompt
— each ending in Pydantic validation.

---

## Scale

Only ~2–5% of transactions fail and become opportunities. The edge is stateless
and partitions by `merchant_id`. The allocator runs per merchant per planning
window, so it shards perfectly — and that per-merchant solve is the real
bottleneck, which is why it is windowed rather than per-event. The LLM is off
the hot path entirely: it is invoked per *cohort strategy*, roughly one call per
10³ opportunities.

The composer is the clearest case. Message bodies carry `{amount}` and `{link}`
placeholders injected by code, so a body is per *(action_kind, channel,
language)* — **O(templates), not O(cases)**. About 24 calls ever, then cached.
That is also the token-cost answer.

---

## Known limits

- **Synthetic data throughout**, fixed seed, generated by `datagen/`. Never
  presented as Razorpay data.
- **`sandbox/` is a simulator** of Razorpay's public REST and webhook contract.
  `api.razorpay.com` is unreachable from the build environment and there are no
  keys. The adapter seam is what makes "swap in live keys" a config change
  rather than a claim.
- **`PaymentIntelligenceProvider` is a Vulcan-*shaped* interface** with a
  simulated implementation. It is not Vulcan.
- **The webhook replay path is slow** (~13 events/s) and two causes are
  identified: a fresh `httpx.AsyncClient` per webhook, and `ProduceSync` per
  record with full ISR acks and no linger. Not fixed, because the evaluation
  uses the direct-to-Kafka fast path deliberately and the demo replays at a
  controlled speed — the honest reason is proportionality, not that it went
  unnoticed.
- **No OpenTelemetry.** Instrumentation was scoped for the final day and cut in
  favour of documentation and the cold-clone guarantee.
- **The dispatcher catches `Exception` as a last resort.** Deliberate and
  narrow in intent: a planning cycle covers thousands of cases, and letting one
  unrecognised exception escape aborts every case after it. The chaos suite
  found exactly that — a raw `ConnectionError` from an adapter propagated out of
  `plan_cycle` and ended the run. It is broad but not silent: the exception type
  is recorded in the action's failure reason and logged with a stack trace, so a
  programming error surfaces as thousands of identical failures rather than
  disappearing.
