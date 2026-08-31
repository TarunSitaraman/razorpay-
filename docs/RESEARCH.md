# RESEARCH — what is actually known, and how it is known

Every factual claim Niyama makes about Razorpay, about Indian payments
regulation, or about the competitive landscape is recorded here with its source.
Two marks are used throughout and they are not decorative:

- **[C]** — confirmed by a cited public source, linked.
- **[I]** — my inference from those sources, labelled as inference.

The distinction matters because the pitch rests on a claim about a *gap* in
someone else's product line. A gap is an inference by construction — you cannot
cite an absence — so it has to be visibly separated from the things that are
documented. Anywhere this document says Razorpay does not do something, it means
*no public source describes them doing it*, which is a weaker statement than
"they do not do it", and it is the only honest one available from outside.

`control/yukti/policy/regpack.py` cites this file. Every rule in that module
maps to a row in §2 below.

---

## 1. Razorpay's own products

| Capability | What it does | Source |
|---|---|---|
| **Failed Payment Recovery** [C] | Auto-sends personalised payment links over WhatsApp / SMS / Email after a failure. The dashboard reports links sent and per-channel conversion. Razorpay states 20–25% of payments fail for avoidable reasons. | [blog](https://razorpay.com/blog/razorpay-failed-payment-recovery/) |
| **Optimizer** [C] | ML routing across 100+ providers; second-priority rules retry a failed transaction on an alternate provider. ~10% success-rate lift claimed. | [blog](https://razorpay.com/blog/boost-payments-success-rates-with-optimizers-ai-ml-routing/), [docs](https://razorpay.com/docs/payments/optimizer/dynamic-routing/) |
| **Subscription retries** [C] | On charge failure a subscription goes `active → pending`; card subscriptions are auto-retried; exhausted retries → `halted`, where invoices still generate but no charge is attempted. | [states](https://razorpay.com/docs/payments/subscriptions/states/), [retries](https://razorpay.com/docs/payments/subscriptions/payment-retries/) |
| **Agent Studio** [C] | Marketplace and no-code builder **built on Anthropic's Claude Agent SDK**. Ships a Subscription Recovery Agent and an Abandoned Cart Conversion Agent (both voice), dispute responders, reconciliation. | [blog](https://razorpay.com/blog/agent-studio-ai-agents-by-razorpay/), [Business Standard](https://www.business-standard.com/finance/news/razorpay-launches-ai-agent-studio-anthropic-claude-payments-126031200388_1.html) |
| **Agentic platform** [C] | Explicit *observe → reason → act* framing: "agents observe financial signals continuously, reason over context, and take action on their own." | [blog](https://razorpay.com/blog/razorpay-agentic-platform/) |
| **Agent guardrails** [C] | Platform-level amount checks, compliance validation, action boundaries; out-of-permission actions blocked **before execution**; merchant approves data and action scope; review-first mode; sensitive actions escalate over WhatsApp; audit trail per agent. | [blog](https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control/) |
| **Vulcan** [C] | Announced 18 Aug 2026. Transformer-based payments foundation model; ~3 trillion data points, ~4 billion payments, ~3,000 signals per transaction, one representation serving routing, fraud, RTO and checkout. Beta across 51,000+ businesses: 8–10% success-rate lift, 8× international card fraud detected. | [AWS press release](https://press.aboutamazon.com/aws-international/2026/8/razorpay-launches-vulcan-indias-first-ai-payments-foundation-model-fueled-by-nvidia-and-aws-re-architecting-payments-for-a-350-bn-e-comm-future-by-2030), [Inc42](https://inc42.com/buzz/razorpay-launches-ai-foundation-model-vulcan-to-expedite-digital-payments/) |
| **MCP server** [C] | Official, written in **Go**, MIT-licensed. Tools include `create_payment_link`, `send_payment_link`, `fetch_payment`, `capture_payment`, `create_order`, `create_refund`. | [razorpay/razorpay-mcp-server](https://github.com/razorpay/razorpay-mcp-server) |

### Engineering signals used to justify stack choices

| Signal | Evidence |
|---|---|
| **Go is first-class** [C] | The official MCP server is Go; AI Engineer job descriptions list Golang. This is why the edge plane is Go rather than "because Go is fast". |
| **Kafka / event-driven** [C] | Microservices migration uses message queues for load distribution and controlled batch processing. ([engineering](https://engineering.razorpay.com/subpage/microservices)) |
| **Spark + Hudi + S3 + Trino + Alluxio on K8s** [C] | Spark and Hudi replicate OLTP data *and ingest microservice events* into an S3 lake; Trino for query; Alluxio as a K8s StatefulSet. ([Alluxio × Razorpay](https://www.alluxio.io/blog/how-trino-and-alluxio-power-analytics-at-razorpay)) |
| **LangGraph multi-agent — Project Viveka** [C] | On-call RCA: stateful LangGraph workflow, **supervisor + parallel specialists**, two RAG stores, evidence written to **memory** so the supervisor reasons over stored facts rather than accumulated context. 30 min → 90 s. ([writeup](https://engineering.razorpay.com/project-viveka-from-30-minute-investigations-to-90-second-ai-analysis-e49ec9db2638)) |

**Niyama copies Viveka's shape deliberately** — supervisor, specialists, evidence
in a store — and departs from it on orchestration, using an event-sourced state
machine rather than LangGraph. The reason is in ARCHITECTURE.md §Agent: the
agent's state machine is already a Kafka consumer with Postgres checkpoints, so
a framework checkpointer would be a second, competing source of truth.

---

## 2. Regulatory surface — the source of every RegPack rule

This section is the citation target for `control/yukti/policy/regpack.py`. Each
row names the rule id that encodes it.

| Rule id | Requirement | Source |
|---|---|---|
| `RBI_PREDEBIT_24H` | Mandatory pre-debit notification **≥24 h** before every recurring deduction, carrying full transaction details and an opt-out path. FASTag/NCMC exempt. [C] | [Business Today](https://www.businesstoday.in/amp/personal-finance/news/story/rbi-auto-debit-rules-explained-what-new-changes-mean-for-your-upi-and-card-payments-528507-2026-05-02), [AMLEGALS checklist](https://amlegals.com/upi-autopay-and-recurring-payments-compliance-checklist-under-rbis-e-mandate-framework-2026/) |
| `RBI_AFA_LIMIT` | No additional-factor authentication required up to **₹15,000** per transaction; **₹1,00,000** for mutual fund, insurance and credit-card-bill categories. Above that, AFA is required and an agent may not schedule the debit autonomously. [C] | same |
| `NPCI_REPRESENT_CAP` | Failed debits may be re-presented only within NPCI-permitted windows, with per-reason-code attempt caps (e.g. **AP39 OTP-invalid → 3 attempts**). [C] | [Razorpay e-NACH playbook](https://razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026), [NPCI error codes](https://docs.decentro.tech/reference/npci-error-codes-mandate-presentation) |
| `TRAI_QUIET_HOURS` | Commercial communication restricted to daytime hours; Niyama encodes **09:00–21:00 IST**. [C] | TRAI TCCCPR; see note below |
| `TRAI_DLT_TEMPLATE` | Commercial SMS and WhatsApp must bind to a registered DLT template; free-form commercial messaging on those channels is not permitted. [C] | TRAI TCCCPR; see note below |
| `DPDP_CONSENT` | Channel-level consent is required, and withdrawal of consent is binding and immediate. Niyama treats absence of a grant as refusal, and an opt-out as global across every agent and surface. [C] | DPDP Act 2023 |

**The consolidated framework.** RBI's e-mandate framework was consolidated
effective 21 Apr 2026 (updated 22 Apr 2026) and covers UPI AutoPay, cards, PPIs
and NACH on the same terms. [C]
([Outlook Business](https://www.outlookbusiness.com/ampstories/news/rbi-e-mandate-framework-2026-new-rules-for-auto-pay-upi-cards-wallets))

**Honest caveat on the last three rows.** The TRAI and DPDP obligations are
summarised from the regulations themselves rather than from a single linked
secondary source, and the specific 09:00–21:00 window is the commonly-applied
industry interpretation of TCCCPR's restriction rather than a figure quoted
verbatim from the regulation. The window is a constant in one place
(`regpack.QUIET_HOURS_START` / `QUIET_HOURS_END`) precisely so it can be
corrected without touching anything else. A real deployment would have this
reviewed by counsel; that is a statement about the project's limits, not a
disclaimer meant to excuse them.

**Why any of this is architecturally load-bearing.** A recovery agent operating
in India cannot freely choose *when* to retry a mandate or *when* to message a
customer. An LLM asked to plan a recovery sequence will violate these — not
maliciously, but because "message her at 22:00, she responds best then" is a
locally correct answer to the wrong question. That is why the policy engine is a
deterministic, separately-tested library on the path of every action, and why
`MerchantPack` can only ever be *more* restrictive than `RegPack`, never less.

The evaluation makes the cost of this visible: on the NBFC book the median
obligation is ~₹24,000, above the ₹15,000 AFA ceiling, so **auto-debit is simply
unavailable for most of the book** and a payment link is the only remaining
move. That is not a modelling artefact — it is the regulation deciding the
action, and the console shows it doing so, rule id and all.

---

## 3. The competitive landscape

| Player | Approach | Where it stops |
|---|---|---|
| **Stripe** [C] | Smart Retries (ML retry timing over billions of transactions), Adaptive Acceptance (recovers false declines in real time), network tokens and Card Account Updater. $6B recovered in 2024, +60% YoY. ([Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries), [Adaptive Acceptance](https://stripe.com/blog/ai-enhancements-to-adaptive-acceptance)) | Card-rail centric; retry-*timing* optimisation rather than cross-channel portfolio arbitration; reports gross recovery. |
| **Butter Payments** [C] | Patented ML over hundreds of variables, "5%+ ARR growth", involuntary-churn focus. ([site](https://www.butterpayments.com/)) | **Publishes no incrementality methodology.** |
| **Slicker** [C] | Ensemble per transaction, and — unusually — an **AABB 50/50 split with reported statistical significance**. ([comparison](https://www.reduxpayments.com/blog/best-failed-payment-recovery-tools)) | The exception that proves the rule. Single-vendor, card-rail, no cross-agent arbitration, no Indian rails. |
| **Chargebee / Recurly / Zuora / Paddle / GoCardless** [C] | Dunning campaigns, retry schedules, card updater. | Rule-based cadences; per-tool contact budgets; no arbitration across tools. |
| **Yuno, Redux, Lago** [C] | "Smart dunning", fatigue scoring, "silent recovery"; cross-channel collision resolution named as a 2026 aspiration. ([Redux on dunning fatigue](https://www.reduxpayments.com/blog/dunning-fatigue-why-your-recovery-emails-are-killing-your-retention), [Yuno](https://y.uno/en/blog/most-retry-strategies-leave-money-on-the-table-smart-dunning-doesnt)) | Marketing copy rather than published method; no rails-level regulatory modelling; no Indian rails. |

**Industry behaviour Niyama's synthetic world encodes** [C]: most recovery lands
by day 14, and days 21–30 add fatigue rather than revenue; multi-channel adds
roughly +23% *if sequenced* rather than blasted. These are the sources for
`DIMINISHING_RETURNS_DAYS = 21` and for the multiplicative contact-fatigue decay
in `datagen/yukti_datagen/response.py`.

---

## 4. The three gaps — stated as inference

These are the load-bearing claims of the pitch, and all three are **[I]**.

**G1 — no cross-agent arbitration.** [I] Razorpay's published guardrail material
describes controls *per agent*: amount checks, action scope, review-first mode,
per-agent audit. No public source describes arbitration *between* agents sharing
an end customer. The inference is that a fleet whose members each hold their own
contact budget can collectively over-contact one person, and that this becomes
material as the marketplace grows past a handful of agents per merchant. Note
what makes this defensible rather than presumptuous: the gap is created by
Razorpay's *own* 2026 roadmap, and only bites once N > 3 agents are live per
merchant — the fleet is roughly five months old at the time of writing.

**G2 — nobody measures incrementality.** [I] Razorpay's Failed Payment Recovery
dashboard reports links sent and conversion rate, which is gross recovery.
Butter publishes no lift methodology. Slicker's AABB design is the only
published controlled measurement found. The inference is that a merchant paying
per recovery action generally cannot tell whether the agent *caused* the
payment. Niyama's own evaluation both demonstrates this and quantifies why it is
hard — see §5.

**G3 — "do nothing" has no product owner.** [I] Recovery products are built to
act, and no dashboard surveyed reports money deliberately *not* chased. The
inference is that suppression is under-served because it is commercially
awkward to sell, not because it is unimportant. Niyama makes it a headline number.

---

## 5. What this project's own measurement found

Recorded here because it is a research finding, not a feature, and because it
qualifies G2 rather than merely supporting it.

A 10% holdout on this dataset **cannot** measure the effect it is there to
measure. On the NBFC book the per-case standard deviation of net margin is
**₹12,870** against a true per-case effect of **₹315** — an effect size of
**0.024σ**. Recovery is a Bernoulli draw multiplied by a heavy-tailed obligation
amount, so that ratio is a property of the data rather than of the estimator.
Post-stratifying the difference on amount decile was tried and made the error
*worse* (−414% → −504%), which is the measurement that located the variance.

At an 11% holdout, resolving that effect at 80% power needs **136,887 cases**;
the merchant has 3,475 — a factor of 39 short.

The honest form of the G2 claim is therefore sharper than "nobody measures
incrementality": **at typical single-merchant volumes, nobody *can*, with a
holdout alone.** That is an argument for measuring across a portfolio, and it is
why the roadmap's federated-intelligence idea is about statistical power rather
than only about privacy.

---

## 6. What is not sourced

Stated plainly, because the absence of these is itself a claim about the project:

- **No Razorpay internal information.** Nothing here comes from a private
  source. Every row above is public and linked, or marked as inference.
- **No live Razorpay API contact.** `api.razorpay.com` is unreachable from the
  build environment. `sandbox/` implements the *public* REST and webhook
  contract behind an adapter; there are no keys.
- **Vulcan is not integrated.** `PaymentIntelligenceProvider` is a
  Vulcan-*shaped* interface with a simulated implementation, and is described
  that way everywhere it appears.
- **All data is synthetic**, generated with a fixed seed by `datagen/`. No
  figure in this repository describes real merchants, real customers or real
  money.
