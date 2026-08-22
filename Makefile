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

.PHONY: help up down status reset venv install migrate seed replay eval demo test lint fmt clean

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

replay: ## Replay the synthetic event stream into Kafka
	@$(PY) -m yukti_datagen.cli replay

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
