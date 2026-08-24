# Yukti — the recovery agent that knows when to stop

**Razorpay Buildathon · Track 03 — AI Revenue Recovery**

Yukti detects revenue at risk across all four leak surfaces the track brief
names — failed payments, abandoned checkouts, failed subscriptions and overdue
receivables — diagnoses the cause, and runs a bounded recovery workflow across a
whole batch under Indian payment regulation and merchant budgets.

It is the only thing in this market that can tell a merchant how many of the
recovered rupees it actually **caused**.

> Every recovery tool optimises for acting. Yukti optimises for **net
> incremental margin**, which means most of its intelligence goes into deciding
> whom **not** to contact.

---

## Why this, and not the obvious thing

The obvious Track 3 ideas are already shipped Razorpay products. Razorpay has
Failed Payment Recovery, Optimizer routing and retries, native subscription
retries, an Agent Studio Subscription Recovery Agent and Abandoned Cart
Conversion Agent, and — since 18 Aug 2026 — Vulcan, a payments foundation model
scoring ~3,000 signals per transaction.

Three gaps survive that landscape:

| Gap | Why it is real |
|---|---|
| **No cross-agent arbitration** | Razorpay now has a *fleet* of recovery agents, each with its own guardrails and its own contact budget. Nothing arbitrates between them, so three agents can work the same customer on the same day. The gap is created by Razorpay's own roadmap and widens as the marketplace grows. |
| **Nobody measures incremental recovery** | Every dashboard in this market reports gross recovered revenue. Roughly a third of failed payments resolve on their own. A merchant paying per recovery action cannot tell whether the agent caused the payment. |
| **Vulcan owns transaction intelligence** | It answers *"how should this transaction be attempted now?"* It does not answer *"across 40,000 open cases, which multi-day sequences do I fund under a ₹2L discount budget, TRAI messaging windows and NPCI re-presentation caps?"* Yukti consumes payment intelligence through an interface rather than competing with it. |

## Against the track's stated bar

> *"Don't just identify the problem. Show measured money recovered across a
> batch, with compliant escalation, stopping rules, and an audit trail."*

| The bar | How | Status |
|---|---|---|
| **Measured** money | Holdout incremental ₹ with 95% CIs, plus the sample size honest measurement would need | ✅ |
| **across a batch** | Lagrangian allocator over every open case per planning window | ✅ |
| **compliant escalation** | RegPack: RBI 24h pre-debit · ₹15k AFA · NPCI caps · TRAI 9–21h · DPDP, plus merchant approval thresholds | ✅ |
| **stopping rules** | Named `StopReason` on every stop; console groups by rule and shows the money not chased | ✅ |
| **audit trail** | Append-only hash-chained `audit_event`; tamper tests detect edits and deletions separately | ✅ |
| **bounded workflow** | `ActionKind` omits refund/payout/mandate-cancel entirely | ✅ |

**The headline**, on an NBFC lending book of 3,475 cases — every arm running the
same allocator, stopping rules and policy engine, differing only in what they
optimise:

| arm | contacts | recovered | contact-attributable ₹ | per 1k (95% CI) |
|---|--:|--:|--:|---|
| fixed cadence | 86 | 1,158 | −1,31,998 | −37,985 [−104,025, +25,003] |
| propensity only | 89 | 1,160 | −43,638 | −12,558 [−59,323, +24,368] |
| **Yukti (uplift)** | **88** | **1,172** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

Yukti spends the *same* budget and is the only arm whose interval excludes zero.
Propensity — same features, same learner, different objective — is second worst.
Full method, caveats and the segment where this does *not* pay:
[docs/EVALUATION.md](docs/EVALUATION.md).

---

## Running it

The local stack runs **natively — no Docker daemon required** (Kafka 4.3.1 in
KRaft mode on the JVM, system Postgres 16, Redis).

```bash
make demo      # everything below, from a cold clone
make services  # ingest-gw + sandbox + console API
open http://localhost:8080
```

`make demo` chains the whole path: stack up, schema, synthetic world, the
randomised exploration history the uplift model trains on, model fitting, policy
packs, a planning cycle, and the six-arm evaluation. Each step is also a target
of its own (`make seed`, `make history`, `make train`, `make plan`, `make eval`)
if you want to watch them individually.

```bash
make test      # 731 tests
```

The console leads with revenue at risk split across all four leak surfaces, then
the measured-money panel, the stopping rules with the money deliberately not
chased, the budget meters, the policy blocks by rule, and a decision feed showing
what each decision *turned down* and which component turned it down.

## Architecture

```
Payments Sandbox ──HMAC webhooks──▶ Edge (Go) ──▶ Kafka ──▶ Control Plane (Python)
                                                              │
   1 Opportunity   case FSM · dedup · versioning · promise-to-pay ledger
   2 Intelligence  transience · uplift · debit timing · downtime · degradation
   3 Allocator     Lagrangian knapsack over contact / discount / channel budgets
   4 Stopping      named rules: lost cause · open PTP · budget spent · NPCI cap
   5 Agent         supervisor + RCA / planner / composer specialists  ← LLM here
   6 Policy        RegPack + MerchantPack → ALLOW / BLOCK / ESCALATE
   7 Dispatcher    idempotency ledger · transactional outbox
                                                              │
                              bounded tool layer ──▶ sandbox ──▶ outcomes ──▶ loop
```

Steps 3, 4, 6 and 7 are **deterministic**. The LLM proposes; it never decides an
amount, a policy verdict, or a holdout assignment.

## What the LLM does — and never does

| Does | Never does |
|---|---|
| Root-cause an aggregate degradation from retrieved evidence | Compute an amount to charge, refund or discount |
| Classify novel free-text decline reasons (clamped to a closed enum) | Decide whether a policy is satisfied |
| Propose which action kinds *not* to fund for a cohort — it can only ever **remove** candidates | Choose a holdout assignment |
| Compose channel copy, including Hinglish, bound to a DLT template | Issue refunds or payouts — *not in the tool schema at all* |

## Bring your own model

The repository ships an **empty `.env`**. Yukti is provider-agnostic: set a key
for any of nine providers and it will use it, or set none and it still runs.

```bash
cp .env.example .env          # every provider, all commented out
python -m yukti.cli llm-status   # what is configured, and what failed last
```

Providers are tried in order. One with no key is skipped without opening a
socket; one that fails permanently — bad key, blocked host, retired model — is
dropped for the rest of the process rather than re-tried on every call.

| Free tier | Notes |
|---|---|
| **Gemini** | Free key from aistudio.google.com, no card. The most generous option. |
| **Groq** | Fastest free inference available. |
| **Cerebras**, **OpenRouter** (`:free` models), **Mistral**, **Together** | All free tiers. |
| **GitHub Models** | Uses an ordinary GitHub PAT — often the one credential you already have. |
| **Ollama** | Fully local, no key, no network. The only option where nothing leaves the machine. |
| Anthropic | Not free; the strongest structured-output support. Last in the default order. |

Everything except Anthropic speaks the OpenAI chat-completions format, so there
is one adapter and a table rather than nine integrations. Structured output
degrades in three rungs — `json_schema`, then `json_object`, then schema-in-prompt
— and every rung ends in Pydantic validation, so a provider that advertises more
than it implements costs a retry rather than a wrong answer.

**Running with no key at all is a supported state, not a degraded one.** The
stopping rules, the allocator, the policy engine and the dispatcher never call a
model. The agent falls back to conservative defaults and *says so* — every
conclusion records whether it came from a model or a fallback, because a fleet
quietly running on defaults behaves plausibly and looks exactly like one that is
working.

## Correctness properties

- **Money is integer paise** everywhere. Floats cannot represent 0.1 exactly
  and this system sums millions of small discounts and channel costs.
- **Idempotent ingest.** Verified live: re-publishing the full event stream
  produced **17,550 duplicates detected, 0 duplicate cases**.
- **Out-of-order safe.** Per-aggregate versions; late events are recorded
  superseded, never applied. No distributed locking.
- **Bounded action schema.** Refunds are impossible because they are absent, not
  because they are denied at runtime.

## Honesty

- All data is **synthetic**, generated by `datagen/` with a fixed seed. It is
  never presented as Razorpay data.
- `sandbox/` is a **simulator** implementing Razorpay's *public* REST and
  webhook contract. No live API is called; there are no Razorpay keys.
- `PaymentIntelligenceProvider` is a **Vulcan-shaped interface** with a
  simulated implementation. It is not Vulcan and is never described as such.
- Claims about Razorpay products come from public sources, cited in
  `docs/RESEARCH.md`.
- The agent's model calls are **not exercised in the build environment**, which
  has no LLM credential. The provider chain is unit-tested, verified end to end
  against a real HTTP endpoint, and its *failure* path is verified live: with
  nothing configured, `yukti agent` on a genuinely detected degradation reports
  `provenance: {'fallback': 2}` and withholds contact rather than guessing.
  `tests/integration/test_llm_live.py` runs against a real provider for anyone
  with a key (`YUKTI_LIVE_LLM_TESTS=1`).

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The four planes, the LLM seam, event correctness, the decision path, and what is unfinished |
| [EVALUATION.md](docs/EVALUATION.md) | The six arms, the headline result, and what honest lift measurement actually costs |
| [RESEARCH.md](docs/RESEARCH.md) | Every claim about Razorpay, Indian regulation and the market, with sources — and confirmed facts kept separate from inference |
| [DEMO.md](docs/DEMO.md) | The six-minute script, twelve beats, with failure drills |
| [INTERVIEW.md](docs/INTERVIEW.md) | The questions this project should be asked, answered cold — including the uncomfortable ones |

## Layout

```
edge/        Go — webhook ingest (HMAC, replay guard, dedup), outcome collector
control/     Python — domain, opportunity, intelligence, allocator, stopping,
             policy, agent, dispatch, experiment, eval, api
             api/static/ is the console: no build step, no Node toolchain
sandbox/     Razorpay-contract-shaped payments simulator
datagen/     synthetic world + counterfactual outcome oracle
infra/       Terraform + K8s manifests (written, not applied)
tests/       unit · integration · agent · policy · chaos · load · eval
```
