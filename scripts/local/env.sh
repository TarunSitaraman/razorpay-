#!/usr/bin/env bash
# Shared environment for Yukti's native (no-Docker) local stack.
#
# Why native and not docker-compose: the target sandbox has the Docker CLI but no
# Docker daemon. Kafka 4.x in KRaft mode needs nothing but a JVM, Postgres and
# Redis are already installed as system packages, so the whole stack runs
# natively. docker-compose.yml is kept in sync for machines that do have a daemon.

set -euo pipefail

YUKTI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export YUKTI_ROOT

export YUKTI_INFRA="${YUKTI_ROOT}/.infra"
export KAFKA_HOME="${YUKTI_INFRA}/kafka"
export KAFKA_DATA="${YUKTI_INFRA}/kafka-data"
export KAFKA_LOG="${YUKTI_INFRA}/logs/kafka.log"
export KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-localhost:9092}"

export KAFKA_VERSION="4.3.1"
export KAFKA_SCALA="2.13"
export KAFKA_URL="https://archive.apache.org/dist/kafka/${KAFKA_VERSION}/kafka_${KAFKA_SCALA}-${KAFKA_VERSION}.tgz"

export PGCLUSTER_VER="16"
export PGCLUSTER_NAME="main"
export YUKTI_PGHOST="${YUKTI_PGHOST:-localhost}"
export YUKTI_PGPORT="${YUKTI_PGPORT:-5432}"
export YUKTI_PGUSER="${YUKTI_PGUSER:-yukti}"
export YUKTI_PGPASSWORD="${YUKTI_PGPASSWORD:-yukti}"
export YUKTI_PGDATABASE="${YUKTI_PGDATABASE:-yukti}"
export DATABASE_URL="postgresql://${YUKTI_PGUSER}:${YUKTI_PGPASSWORD}@${YUKTI_PGHOST}:${YUKTI_PGPORT}/${YUKTI_PGDATABASE}"

export REDIS_PORT="${REDIS_PORT:-6379}"
export REDIS_URL="redis://localhost:${REDIS_PORT}/0"

# Topics are partitioned by merchant_id so that per-merchant ordering holds while
# distinct merchants process in parallel. 6 partitions is arbitrary-but-honest for
# a laptop; the number is a config value precisely because it is a scaling knob.
export YUKTI_TOPIC_PARTITIONS="${YUKTI_TOPIC_PARTITIONS:-6}"
export YUKTI_TOPICS="payments.events recovery.opportunities recovery.actions recovery.outcomes yukti.dlq"

log()  { printf '\033[0;36m[yukti]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m  ok\033[0m   %s\n' "$*"; }
warn() { printf '\033[0;33m  warn\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m  FAIL\033[0m %s\n' "$*" >&2; exit 1; }
