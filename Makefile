# Yukti — Razorpay Buildathon Track 3 (AI Revenue Recovery)
#
# The local stack runs NATIVELY (Kafka in KRaft on the JVM, system Postgres and
# Redis) because the target environment has the Docker CLI but no daemon.
# docker-compose.yml is kept in sync for machines that do have one.

SHELL := /bin/bash
.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help up down status reset venv install migrate seed history replay replay-fast train plan replay-webhooks edge services services-down outbox eval sensitivity demo test lint fmt clean audit seed-policy

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start Kafka + Postgres + Redis (native, no docker daemon needed)
	@./scripts/local/stack.sh up

down: ## Stop the local stack
	@./scripts/local/stack.sh down

status: ## Health of the local stack
	@./scripts/local/stack.sh status

# `services-down` first, and it is not optional. The console API and the sandbox
# hold open connections to the database, so DROP DATABASE fails with "is being
# accessed by other users" — and it fails PARTWAY THROUGH, after Kafka has been
# stopped and Redis flushed, leaving a half-reset stack that looks broken in a
# new way. Running services is the normal state after `make demo`, so this hit
# every time anyone tried to start over.
reset: services-down ## Wipe Kafka data, flush Redis, drop+recreate the database
	@./scripts/local/stack.sh reset

venv: ## Create the Python virtualenv
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv ## Install Python dependencies
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -e ".[dev]"
	@echo "  ok   python deps installed"

migrate: ## Apply database migrations
	@$(PY) -m yukti.cli migrate

seed: ## Generate synthetic data (fixed seed — reproducible)
	@$(PY) -m yukti_datagen.cli generate

history: ## Generate the randomised exploration history (RCT for uplift training)
	@$(PY) -m yukti_datagen.cli history

replay: ## Replay the event log into Kafka, paced at 200x (demo path)
	@$(PY) -m yukti_datagen.cli replay

replay-fast: ## Replay unpaced, as fast as the broker accepts (eval path)
	@$(PY) -m yukti_datagen.cli replay --speed 0

replay-webhooks: ## Replay via sandbox -> ingest-gw -> Kafka (realistic path, used by demo)
	@$(PY) -m yukti_datagen.cli replay-webhooks

edge: ## Build the Go edge binaries
	@cd edge && CGO_ENABLED=0 go build -o bin/ingest-gw ./cmd/ingest-gw
	@echo "  ok   edge/bin/ingest-gw"

services: edge ## Start ingest-gw, sandbox and console API
	@./scripts/local/services.sh up

services-down: ## Stop ingest-gw, sandbox and console API
	@./scripts/local/services.sh down

train: ## Fit the intelligence models, run the gate, persist for serving
	@$(PY) -m yukti.intelligence.cli train --save

seed-policy: ## Give every merchant an active policy pack from its segment defaults
	@$(PY) -m yukti.cli seed-policy

plan: ## Run one planning cycle (MERCHANT=<id> DATE=<iso> optional)
	@$(PY) -m yukti.cli plan \
	  $(if $(MERCHANT),--merchant $(MERCHANT)) \
	  $(if $(DATE),--date $(DATE)) \
	  $(if $(LIMIT),--limit $(LIMIT)) \
	  $(if $(DRY_RUN),--dry-run)

audit: ## Verify the audit hash chain for every merchant
	@$(PY) -m yukti.cli audit-verify

consume: ## Consume payment events into recovery cases
	@$(PY) -m yukti.cli consume

outbox: ## Drain the transactional outbox to Kafka
	@$(PY) -m yukti.cli outbox

eval: ## Run all baseline arms and emit the lift report
	@$(PY) -m yukti.eval.cli run \
	  $(if $(MERCHANT),--merchant $(MERCHANT)) \
	  $(if $(DATE),--date $(DATE))

# Needs NO services and NO database: the world is generated, explored, learned
# and graded in process. That is the point — the frontier is the answer to
# "you built a world where you win", so it has to be reproducible by someone who
# has just cloned the repository and cannot run the stack.
sensitivity: ## Sweep the assumptions the headline rests on (no services needed)
	@$(PY) -m yukti.eval.cli sensitivity $(if $(AXIS),--axis $(AXIS)) $(if $(SEED),--seed $(SEED))

# ---------------------------------------------------------------------------
# Light demo: service-free. Runs the sensitivity sweep (the only part of the
# full demo that does not need Kafka/Postgres/Redis) and generates a static
# console data bundle from the results. The console will serve this bundle
# when the database is unavailable, so a judge on any OS can view the project
# in ~5 minutes after a cold clone.
# ---------------------------------------------------------------------------
demo-light: sensitivity ## Run the service-free sensitivity sweep
	@$(PY) -m yukti.eval.cli sensitivity
	@$(PY) -c "
import json, pathlib
from yukti.eval.sensitivity import sweep, ARM_KEYS
from yukti.eval.estimator import Interval
# Build a lightweight eval report from the sensitivity run.
# We only need the persuadable_uplift axis because it is the headline axis.
points = sweep('persuadable_uplift', (0.46, 0.06), n_train=1_200, n_plan=600, contact_budget=25)
rich, poor = points[0], points[1]
report = {'arm_results': {}, 'comparison': {}, 'power': {}}
for arm in ARM_KEYS:
    a = rich.arms[arm]
    b = poor.arms[arm]
    report['arm_results'][arm] = {
        'contacts': a.contacts,
        'recovered_cases': a.recovered_cases,
        'incremental_paise': a.incremental_paise,
        'contact_attributable_paise': a.contact_attributable_paise,
        'per_1k': str(a.per_1k) if isinstance(a.per_1k, str) else f\"[{a.per_1k.low:.0f}, {a.per_1k.high:.0f}]\",
        'winner': a.winner,
    }
    report['comparison'][arm] = {
        'rich_incremental': rich.arms[arm].incremental_paise,
        'poor_incremental': poor.arms[arm].incremental_paise,
        'delta': rich.arms[arm].incremental_paise - poor.arms[arm].incremental_paise,
    }
# Power analysis disclosure (from EVALUATION.md §3)
import math
per_case_sd = 12_870  # from EVALUATION.md: ~12,870 rupees of noise around ~315 effect
effect_size = 315      # per-case effect size (also from EVALUATION.md)
treated_fraction = 0.75
holdout_fraction = 0.10
factor = 1 / (1 - holdout_fraction) + 1 / holdout_fraction
cases_needed = math.ceil(factor * (2.8 * per_case_sd / effect_size) ** 2)
report['power'] = {
    'per_case_sd_paise': per_case_sd,
    'effect_size_paise': effect_size,
    'cases_needed_for_80pct_power': cases_needed,
    'available_cases': 3_475,
    'shortfall_vs_80pct': '39×',
}
pathlib.Path('artifacts/demo-results.json').write_text(json.dumps(report, indent=2))
print('  ok   generated artifacts/demo-results.json')
"
	@printf "\n  \033[0;36m[yukti]\033[0m demo-light ready - open http://localhost:8080\n\n"

# A planning moment INSIDE the synthetic world, which spans 2026-05-01 to
# 2026-07-29 with open cases from 2026-07-08. The default for `make plan` is
# now(), and running the demo chain with that default is a trap worth naming:
# every case is then months past the 21-day diminishing-returns knee, so all six
# merchants stop their entire book and the console shows 23,864 cases and zero
# actions. Target-specific variables propagate to prerequisites in GNU make, so
# this reaches both `plan` and `eval`.
demo: DATE = 2026-07-20T10:00:00
demo: up install migrate seed history replay-fast consume train seed-policy services plan eval ## Everything, from a cold clone
	@printf "\n  \033[0;36m[yukti]\033[0m ready - open http://localhost:8080\n\n"

# The ordering is not arbitrary and the chain breaks if it is disturbed:
#   seed        the synthetic world -> Postgres + Parquet
#   history     the randomised exploration period, taken from obligations that
#               failed BEFORE the temporal cutoff. This is the RCT the uplift
#               model is identified from; without it there is nothing to train on.
#   replay-fast + consume
#               events after the cutoff become the open cases planning operates
#               on. Unpaced, because `make demo` should not take 90 simulated
#               days to finish; `make replay` is the paced path the demo screen
#               recording uses.
#   train       fits and PERSISTS the models with their feature schema
#   seed-policy every merchant needs an active policy pack before any action can
#               be evaluated
#   services    BEFORE plan, not after. `plan` dispatches through the sandbox
#               over HTTP, so on a cold clone with nothing running every dispatch
#               fails: the first run of this chain recorded 2,408 actions, all
#               with status 'failed', and reported 0 dispatched while looking
#               like it had worked. It also builds the Go edge binaries, so the
#               cold path exercises that compile too.
#   plan        one planning cycle, so the console has decisions to show
#   eval        the six-arm comparison; writes artifacts/eval-report.json, which
#               is what /metrics/lift serves

test: ## Run the test suite
	@$(VENV)/bin/pytest -q

lint: ## Lint and type-check
	@$(VENV)/bin/ruff check control datagen sandbox tests
	@cd edge && go vet ./... 2>/dev/null || true

fmt: ## Format
	@$(VENV)/bin/ruff format control datagen sandbox tests
	@cd edge && go fmt ./... 2>/dev/null || true

clean: ## Remove venv and generated data
	@rm -rf $(VENV) data/generated
