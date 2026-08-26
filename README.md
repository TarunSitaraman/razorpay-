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
It maximizes `Σ (uplift × amount × (1 − mdr)) − discount − channel_cost` subject to daily merchant budgets and NPCI fatigue caps. The problem is solved via Lagrangian relaxation, achieving 99.96% of the exact optimal margin. Scarce resources are never wasted on unrecoverable cases.

### 3. Event Correctness
- **Exactly-Once Effect**: Four layers of deduplication (HMAC -> Redis TTL -> Postgres Unique Index -> Derived Idempotency Ledger) ensure a duplicated webhook never causes a double charge.
- **Transactional Outbox**: Decisions commit to Postgres in a single ACID transaction before a relay publishes to Kafka.
- **Idempotent Replay**: Webhook replays push asynchronously to Kafka without blocking on ISR acks.

### 4. The LLM Boundary
The LLM acts strictly as a proposer. It never decides amounts, policy verdicts, or initiates refunds. 
It performs root-cause analysis on retrieved evidence, classifies free-text decline reasons, and interpolates DLT-compliant channel copy. Its output schema only allows it to *remove* action candidates.

### 5. OpenTelemetry
The Python control plane is instrumented with the standard OpenTelemetry SDK. Traces for FastAPI, HTTPX, and SQLAlchemy queries are exported to the ConsoleSpanExporter, ready for OTLP production environments.

### 6. The MCP Server
A built-in Model Context Protocol (MCP) Server (`control/yukti/mcp_server.py`) exposes the core domain logic—revenue at risk, pipeline counts, and stopping rules—as standardized tools for external agents.

---

## 📊 Evaluation Results

| The Bar | How We Met It | Status |
|---|---|---|
| **Measured money** | Holdout incremental ₹ with 95% CIs, plus the sample size required for statistical power. | ✅ |
| **Across a batch** | Lagrangian allocator over every open case per planning window. | ✅ |
| **Compliant escalation** | RegPack: RBI 24h pre-debit · ₹15k AFA limit · NPCI attempt caps · TRAI 9–21h quiet hours · DPDP consent. | ✅ |
| **Stopping rules** | Named `StopReason` on every stop; the console explicitly groups the money deliberately not chased. | ✅ |
| **Audit trail** | Append-only hash-chained `audit_event` per merchant. Tamper tests detect edits/deletions. | ✅ |
| **Bounded workflow** | `ActionKind` schema omits refund/payout/mandate-cancel entirely at the type level. | ✅ |

### The Headline Result (NBFC Lending Book of 3,475 cases)
| Arm | Contacts | Recovered | Contact-attributable ₹ | Per 1k (95% CI) |
|---|--:|--:|--:|---|
| Fixed Cadence | 86 | 1,158 | −1,31,998 | −37,985 [−104,025, +25,003] |
| Propensity Only | 89 | 1,160 | −43,638 | −12,558 [−59,323, +24,368] |
| **Niyama (uplift)** | **88** | **1,172** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

Niyama spends the same budget as naive approaches but is the only arm whose confidence interval excludes zero.

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

# 4. Run the 731-test suite
make test      
```

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
control/     Python — domain, uplift intelligence, allocator, policy engine, dispatcher, eval, mcp_server
sandbox/     Razorpay-contract-shaped simulator
datagen/     Synthetic world generator
infra/       Terraform + Kubernetes manifests
tests/       731 tests
```
