# INTERVIEW — the answers, cold

Written to be defended, not recited. Where a question has an uncomfortable
answer, the uncomfortable answer is here, because the alternative is discovering
it live.

---

## On the idea

**"Why wouldn't Razorpay already have this?"**
Because their agent fleet is about five months old and arbitration only bites
once more than about three agents are live per merchant. The gap is created by
Razorpay's *own* 2026 roadmap — Agent Studio ships a Subscription Recovery Agent
and an Abandoned Cart Conversion Agent with per-agent guardrails and per-agent
budgets. That is a stronger position than "you forgot something": the problem
gets worse as the marketplace succeeds.

**"Isn't this just an LLM wrapper?"**
No, and the seam is structural rather than stylistic. The allocation is a
constrained optimisation, the enforcement is a compiled deterministic policy
engine, the proof is a holdout-based causal estimator. The model does four
bounded jobs and its output surface — `Advice` — carries one effective field: a
set of action kinds to *remove*. There is nowhere to express "also try X". With
no credential configured at all, the system still stops, allocates, enforces
policy and dispatches; that is verified live, not asserted.

**"Could a competent developer build this in a weekend?"**
The CRUD and a prompt, yes. Not the uplift model plus the budget allocator plus
the policy engine plus the holdout estimator plus an idempotent event pipeline —
and above all not a *defensible* incremental-lift number, which is where most of
the difficulty actually was.

**"Who pays for it?"**
The merchant, out of margin it can be shown was created. Yukti is the only thing
in this market that computes that number honestly — and it makes every *other*
Agent Studio agent more sellable by proving its lift.

---

## On the engineering

**"Hardest technical problem?"**
Exactly-once *effect* under at-least-once delivery. A duplicated or out-of-order
`payment.failed` must never produce a second discount or a second debit. Four
layers: HMAC → Redis dedup set → Postgres unique index → idempotency ledger on a
**derived** fingerprint. Derived rather than minted, so a replayed cycle collides
on a unique index instead of dispatching again. The layers are not equal — the
last one is the only one that protects money.

**"Out-of-order events?"**
Per-aggregate monotonic version. The FSM refuses backward transitions and
records the late event as *superseded* rather than applying it. No distributed
locking.

**"Agent crashes mid-plan?"**
Runs are event-sourced; plans are proposals; nothing executes until the
dispatcher commits. A crash can lose planning work, never emit a duplicate
action.

**"Why Kafka and not a queue?"**
Partitioned ordering per merchant, and replay from offset. Replay is how the
evaluation re-runs identical cases across six arms deterministically. A queue
destroys messages on consumption.

**"Why not LangGraph? Razorpay uses it."**
The agent's state machine is already a Kafka consumer with Postgres checkpoints.
A framework checkpointer would be a second, competing source of truth for the
same state. I copied Viveka's *shape* — supervisor, parallel specialists,
evidence written to a store so the supervisor reasons over retrieved facts — and
departed on orchestration. I would use LangGraph if the state were not already
durable somewhere else.

**"At 10M transactions?"**
Only 2–5% fail and become opportunities. The edge is stateless and partitions by
merchant. The allocator is per merchant per window, so it shards perfectly —
that per-merchant solve is the actual bottleneck, which is why it is windowed
rather than per-event. The LLM is off the hot path: roughly one call per 10³
opportunities, and message composition is O(templates) not O(cases) because code
injects every amount and URL.

---

## On the measurement

**"How do you know your lift number is right?"**
The best question, and the answer is that at this volume **it is not**, and the
report says so. Per-case standard deviation is ₹12,870 against a true per-case
effect of ₹315 — an effect size of 0.024σ. At an 11% holdout, resolving that at
80% power needs 136,887 cases; the merchant has 3,475. So the report prints the
requirement rather than a confident number.

I tried the obvious fix first — post-stratifying the difference on amount decile
— and it made the error *worse*, from −414% to −504%. That measurement is what
located the variance: it lives in whether individual large obligations happened
to recover, not in any imbalance between groups. So the stratification came out
rather than staying in as complexity that sounded principled and did nothing.

The useful consequence: honest incrementality measurement has to pool across
merchants. That is a statistical-power argument for federated intelligence, not
just a privacy one.

**"Your headline is one merchant. What about the others?"**
On the d2c book — ₹597 mean obligation — the ordering holds but every interval
crosses zero. Contact economics scale with obligation value: a contact costs the
same chasing ₹597 as ₹24,000. The honest claim is that this technique pays in
proportion to what is at stake, and I would not sell it to a low-ticket
subscription merchant on these numbers.

**"Isn't the oracle just telling you what you want to hear?"**
It is a potential-outcomes model with the archetype structure baked in, so in a
sense yes — the *shape* of the answer is designed. What is not designed is
whether a model can recover that shape from observable features. The archetype
is never a feature; `features.py` asserts the column is absent and a test builds
a frame through the real code path and fails if it or a proxy appears. And the
oracle punished me repeatedly — it is how the target leak, the four
costless-action bugs and the holdout contamination were all caught.

---

## On what went wrong

**"What's the worst bug you shipped?"**
Yukti lost its own first evaluation to a fixed cadence, and the cause was four
instances of one mistake. A silent retry costs nothing and the customer never
sees it, so its true effect is small but never negative — it has no downside
branch. That makes `margin > 0` a test of the *estimate's sign*, and near zero
the sign of a noisy estimate is a coin flip. The allocator, two stopping rules
and a budget rule each declined free money on that flip, dropping ~892 free
retries per cycle.

The fix is a design correction, not a threshold: the allocator exists to ration
*scarce* resources, and a costless invisible action is not scarce. Its one
scarce resource is the NPCI re-presentation attempt, which RegPack already caps
— a regulatory limit rather than an economic judgement, which is where it
belongs.

**"What's the pattern in your bugs?"**
Every serious one destroyed or biased data *while reporting success*. A target
leak that made a discount look like +0.92 uplift. A cleanup that reopened 2,156
exploration cases and turned treated rows into controls. Two
`LIMIT`-without-`ORDER BY` bugs, one of which returned a training frame with
2,000 treated and 0 control rows. Holdout contamination that inflated every arm.
None was caught by a component's own tests, because each component was correct
in isolation — the defects were all in how they composed.

So every fix now ships with a test asserting the *invisible* consequence rather
than the visible symptom, and where the same mistake appeared four times the
invariant is asserted once over the rules as a set.

**"What would you do differently?"**
Write the costless-action invariant on day one. And distrust any metric that
looks fine on one sample — the bootstrap coverage test and the seven-split gate
both exist because a single split looked fine and wasn't.

**"What's unfinished?"**
No OpenTelemetry; it was scoped for the last day and cut for documentation. The
webhook replay path runs at ~13 events/s and I know both causes — a fresh HTTP
client per webhook, and `ProduceSync` per record with full ISR acks and no
linger — but the evaluation deliberately uses the direct-to-Kafka fast path, so
fixing it would be optimising something whose only requirement is demo
throughput. The MCP server is designed and not built.

---

## The uncomfortable ones

**"You have no Razorpay API access. Isn't this all hypothetical?"**
The sandbox implements Razorpay's *public* REST and webhook contract behind a
`RazorpayAdapter` interface, and it is the only implementation — I did not write
a `LiveRazorpay` I could never run. The adapter seam is what makes "swap in live
keys is a config change" a checkable claim rather than a hope. The HMAC scheme is
Razorpay's documented one: SHA-256 hex over the raw body, verified before
parsing.

**"Have you ever actually called a model?"**
The provider chain is unit-tested and verified end to end against a real HTTP
endpoint. In the build environment there is no credential, so the *failure* path
is what is verified live — and that turned out to be more informative: with
nothing configured, the agent on a genuinely detected degradation reports
`provenance: {'fallback': 2}` and withholds contact rather than guessing. A
mocked success would have demonstrated less.

**"Isn't the data too clean?"**
It is generated, and it is generated with the correlations that make the problem
hard rather than easy — salary-day balance availability, bursty bank-correlated
downtime, multiplicative contact fatigue across agents, a discount-farming
cohort, injected degradation episodes with characteristic decline-mix shifts. It
is not noise-free and it is not adversarial either. The honest statement is that
it validates the *decision logic*, not the deployment.

**"Why should we believe the 99%-of-optimal claim?"**
Because it is measured against brute-force enumeration on small instances, not
against the relaxation's own bound — which would flatter it. The first version
hit 89% of exact optimum with a worst case of 0.187; taking the better of two
heuristics moved that to 99% and 0.947. The mean was fine both times, which is
the point.
