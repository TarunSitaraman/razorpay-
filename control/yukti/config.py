"""Runtime configuration, read from the environment.

Defaults match scripts/local/env.sh so that a developer who has run `make up`
needs no .env file. Anything secret (the Anthropic key) has no default.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YUKTI_", extra="ignore")

    database_url: str = "postgresql://yukti:yukti@localhost:5432/yukti"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap: str = "localhost:9092"

    topic_payments: str = "payments.events"
    topic_opportunities: str = "recovery.opportunities"
    topic_actions: str = "recovery.actions"
    topic_outcomes: str = "recovery.outcomes"
    topic_dlq: str = "yukti.dlq"

    sandbox_url: str = "http://localhost:8081"
    api_port: int = 8080

    # Shared secret for webhook HMAC. Mirrors how Razorpay signs webhooks; the
    # sandbox signs with this and the Go edge verifies with it.
    webhook_secret: str = "yukti_dev_webhook_secret"

    # Tiered models: the cheap one does high-volume classification, the capable
    # one does planning and policy compilation. Cost per decision is a reported
    # metric, so this split is measured rather than assumed — and the volume
    # path is the one that would dominate the bill.
    #
    # IDs are complete as written; do not append date suffixes.
    model_fast: str = "claude-haiku-4-5"
    model_planner: str = "claude-opus-5"

    # Global holdout. Every merchant carries one; this is the denominator that
    # makes an incremental-lift claim meaningful.
    holdout_pct: float = 10.0

    seed: int = 20260822


@lru_cache
def settings() -> Settings:
    return Settings()
