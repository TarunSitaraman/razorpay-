# Yukti — The Recovery Agent That Knows When To Stop

**Razorpay Buildathon · Track 03 — AI Revenue Recovery**

Yukti is a master orchestration agent that detects revenue at risk across all four leak surfaces (failed payments, abandoned checkouts, failed subscriptions, and overdue receivables), diagnoses the cause, and runs a **bounded, mathematically optimized recovery workflow** across an entire merchant's batch. 

It operates strictly within Indian payment regulations (RBI, NPCI, TRAI) and merchant budgets, optimizing for **net incremental margin** rather than gross recovery. In short: most of Yukti's intelligence goes into deciding whom *not* to contact.

---

## 🏗️ What We Actually Built (The Technical Reality)

Yukti is not an LLM wrapper. It is a highly robust, event-driven, four-plane distributed system designed for exactly-once execution and strict financial correctness.

### 1. The Four Planes
1. **Edge (Go)**: A stateless, horizontally scalable webhook ingest gateway. It performs HMAC verification, protects against replay attacks, and pushes events to Kafka. Built in Go, aligning with Razorpay's internal MCP standards.
2. **Control (Python)**: The core intelligence layer. Consumes from Kafka (with replayability for deterministic evaluations). Houses the LLM agent, the uplift models, the budget allocator, and the transactional outbox.
3. **Policy (In-Process)**: A deterministic, compiled library (RegPack & MerchantPack) evaluated *twice* (once for planning, once pre-dispatch) to guarantee compliance without network dependencies.
4. **Measurement (Offline)**: An offline holdout-based causal estimator to measure actual incremental lift.

### 2. The Lagrangian Knapsack Allocator
Agent Studio limits budgets *per agent*. Yukti introduces **cross-agent arbitration**. 
It maximizes `Σ (uplift × amount × (1 − mdr)) − discount − channel_cost` subject to daily merchant budgets and per-customer fatigue caps. Since this multi-dimensional knapsack problem is NP-hard, Yukti uses a **Lagrangian relaxation solver** that achieves **99.96% of the exact optimal margin**, ensuring scarce resources (like NPCI re-presentation caps and SMS budgets) are never wasted on unrecoverable cases.

### 3. Strict Event Correctness & Safety
- **Exactly-Once Effect**: Four layers of deduplication (HMAC -> Redis TTL -> Postgres Unique Index -> Derived Idempotency Ledger) ensure a duplicated webhook *never* results in a double charge.
- **Transactional Outbox**: Decisions and action-intents commit to Postgres in a single ACID transaction before a relay publishes to Kafka.
- **Idempotent Replay**: Webhook replays are pushed asynchronously to Kafka (`Produce`) without blocking on ISR acks, enabling massive replay throughput.

### 4. The LLM Seam (Bounded AI)
The LLM acts purely as a proposer. It **never** decides an amount, chooses a policy verdict, or issues a refund. 
- **What it does**: Root-cause analysis from retrieved evidence, classification of novel free-text decline reasons, and composing DLT-compliant channel copy.
- **What it cannot do**: Its output schema (`Advice`) only allows it to *remove* action candidates. The agent cannot escalate its privileges. If the LLM goes down (or runs without a key), Yukti's deterministic rules fallback gracefully and continue to recover revenue.

### 5. OpenTelemetry & Observability
The entire Python control plane is fully instrumented with the standard **OpenTelemetry SDK**. Traces for `FastAPI`, `HTTPX`, and `SQLAlchemy` queries are automatically generated and exported (currently to the `ConsoleSpanExporter`, ready for Jaeger/OTLP in production).

### 6. The MCP Server
Yukti ships with its own Model Context Protocol (MCP) Server (`control/yukti/mcp_server.py`). It exposes Yukti's core domain logic (Revenue at Risk, Pipeline Counts, and Stopping Rules) as standardized tools, allowing external agents or Razorpay's internal tooling to seamlessly query Yukti's analytical state.

---

## 📊 Against the Track's Stated Bar

> *"Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."*

| The Bar | How We Met It | Status |
|---|---|---|
| **Measured money** | Holdout incremental ₹ with 95% CIs, plus the sample size honest measurement would need. | ✅ |
| **Across a batch** | Lagrangian allocator over every open case per planning window. | ✅ |
| **Compliant escalation** | RegPack: RBI 24h pre-debit · ₹15k AFA limit · NPCI attempt caps · TRAI 9–21h quiet hours · DPDP consent. | ✅ |
| **Stopping rules** | Named `StopReason` on every stop; the console explicitly groups and displays the *money we deliberately chose not to chase*. | ✅ |
| **Audit trail** | Append-only hash-chained `audit_event` per merchant. Tamper tests detect edits/deletions. | ✅ |
| **Bounded workflow** | `ActionKind` schema omits refund/payout/mandate-cancel entirely at the type level. | ✅ |

### The Headline Result (NBFC Lending Book of 3,475 cases)
| Arm | Contacts | Recovered | Contact-attributable ₹ | Per 1k (95% CI) |
|---|--:|--:|--:|---|
| Fixed Cadence | 86 | 1,158 | −1,31,998 | −37,985 [−104,025, +25,003] |
| Propensity Only | 89 | 1,160 | −43,638 | −12,558 [−59,323, +24,368] |
| **Yukti (uplift)** | **88** | **1,172** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

Yukti spends the *same* budget as naive approaches but is the only arm whose confidence interval safely excludes zero.

---

## 🚀 Running It Locally

The local stack runs **natively** (Kafka 4.3.1 in KRaft mode, system Postgres 16, Redis). No Docker daemon required.

```bash
# 1. Setup your environment
cp .env.example .env
# Optional: Add an LLM key (e.g., GEMINI_API_KEY) in .env. 
# Yukti is provider-agnostic and will gracefully fallback if no key is provided.

# 2. Run the full demo (Cold clone -> Seed -> Train -> Plan -> Eval)
make demo      

# 3. View the console
open http://localhost:8080

# 4. Run the 731-test suite
make test      
```

*(For a detailed step-by-step walkthrough, see [docs/DEMO.md](docs/DEMO.md))*

---

## 🧠 Documentation Roadmap

We believe in documenting the hard realities of building financial software. Please read these in order:

| File | Description |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The four planes, the exact LLM boundary, event idempotency, and the decision path. |
| [EVALUATION.md](docs/EVALUATION.md) | The six comparison arms, the headline result, and what honest lift measurement actually costs. |
| [RESEARCH.md](docs/RESEARCH.md) | Every claim about Razorpay, Indian regulation, and the competitive market, heavily sourced. |
| [INTERVIEW.md](docs/INTERVIEW.md) | The hardest questions this project could be asked, answered coldly—including a ruthless self-critique of our own math. |
| [DEMO.md](docs/DEMO.md) | The six-minute pitch script and walkthrough. |

---

## 📂 Layout

```text
edge/        Go — webhook ingest (HMAC, replay guard, dedup), producer
control/     Python — domain, uplift intelligence, allocator, policy engine, dispatcher, eval, mcp_server
             api/static/ is the React-free HTML/JS console
sandbox/     Razorpay-contract-shaped simulator (REST & Webhooks)
datagen/     Synthetic world generator + counterfactual outcome oracle
infra/       Terraform + Kubernetes manifests
tests/       731 tests (unit, integration, agent, chaos, load, eval)
```
