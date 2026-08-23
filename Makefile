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

.PHONY: help up down status reset venv install migrate seed history replay replay-fast train plan replay-webhooks edge services services-down outbox eval demo test lint fmt clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start Kafka + Postgres + Redis (native, no docker daemon needed)
	@./scripts/local/stack.sh up

down: ## Stop the local stack
	@./scripts/local/stack.sh down

status: ## Health of the local stack
	@./scripts/local/stack.sh status

reset: ## Wipe Kafka data, flush Redis, drop+recreate the database
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

consume: ## Consume payment events into recovery cases
	@$(PY) -m yukti.cli consume

outbox: ## Drain the transactional outbox to Kafka
	@$(PY) -m yukti.cli outbox

eval: ## Run all baseline arms and emit the lift report
	@$(PY) -m yukti.eval.cli run

demo: up migrate seed replay ## Full demo path from a cold start

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
