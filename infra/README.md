# infra — written, not applied

**Nothing in this directory has been run.** There is no Docker daemon and no
Kubernetes cluster in the environment this was built in, so applying any of it
would have been theatre. It is here because "how would this deploy?" is a fair
question and an architecture diagram is not an answer to it.

What follows is therefore a *design artefact*: it shows the shape of a
deployment and the decisions inside it, and it is labelled honestly rather than
presented as running infrastructure.

## The shape

```
                         ┌──────────────────────────┐
   internet ──HMAC──▶    │  ingest-gw (Go)          │  stateless · HPA on RPS
   webhooks              │  3+ replicas             │  the only public surface
                         └────────────┬─────────────┘
                                      │ produce, keyed by merchant_id
                         ┌────────────▼─────────────┐
                         │  MSK / Kafka             │  partitions ≥ merchants
                         └────────────┬─────────────┘
                                      │
      ┌───────────────────────────────┼───────────────────────────┐
      │                               │                           │
┌─────▼──────┐              ┌─────────▼────────┐        ┌─────────▼────────┐
│ consumer   │              │  planner CronJob │        │  outbox relay    │
│ Deployment │              │  per merchant    │        │  Deployment      │
│ opportunity│              │  per window      │        │  at-least-once   │
└─────┬──────┘              └─────────┬────────┘        └─────────┬────────┘
      └───────────────┬───────────────┴───────────────────────────┘
                      │
              ┌───────▼────────┐        ┌──────────────┐
              │ RDS Postgres   │        │ ElastiCache  │
              │ source of truth│        │ Redis        │
              └────────────────┘        └──────────────┘
```

## The decisions worth defending

**The planner is a CronJob, not a service.** `plan_cycle` is a pure function of
database state — it reads what is true at `as_of` and writes what follows. That
makes it safe to re-run, safe to crash, and wrong to hold in a long-lived
process. It also shards perfectly: one job per merchant per window, because the
allocator's per-merchant solve is the real bottleneck.

**`ingest-gw` is the only thing exposed.** It is stateless, verifies HMAC before
parsing, and caps body size — so it can scale on request rate alone and a
compromise of it reaches nothing but a Kafka topic.

**Partitions ≥ merchants.** Ordering is only needed *within* a merchant, and
keying by `merchant_id` gives that for free while letting consumers scale
horizontally.

**No autoscaling on the planner.** Adding planner replicas does not make a
merchant's allocation faster — it is one solve — and concurrent cycles for the
same merchant would contend on the budget ledger. The ledger's conditional
update makes that safe rather than corrupting, but safe-and-wasteful is still
wasteful.

**Secrets are referenced, never defaulted.** No manifest here contains a value
that would work if you applied it unchanged. That is deliberate: a manifest with
a plausible default is a manifest someone deploys.

## What is deliberately missing

- **No autoscaling policy for Kafka consumers.** Consumer-group rebalancing under
  aggressive HPA causes more harm than the throughput it buys, and I have not
  measured the right thresholds, so I have not invented them.
- **No multi-region.** Payment obligations are per-merchant and India-resident;
  the interesting problem there is data residency, not latency.
- **No service mesh.** Four components do not need one.
- **No Terraform state backend configured.** That is an organisational choice
  (which bucket, which lock table) rather than an architectural one.

## The local path is the real one

Everything in this repository actually runs via `make demo` — Kafka 4.3.1 in
KRaft mode on the JVM, system Postgres 16, Redis, natively, with **no Docker
daemon required**. That path is tested; this one is drawn.
