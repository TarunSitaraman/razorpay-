#!/usr/bin/env bash
# Redis for fatigue counters (TTL), rate limits, the webhook dedup set and
# dispatcher locks. Persistence is off: everything here is reconstructible from
# Postgres or the Kafka log, and a durable store would imply a guarantee Redis
# is not the source of truth for.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

is_up() { redis-cli -p "${REDIS_PORT}" ping >/dev/null 2>&1; }

start() {
  is_up && { ok "redis already running on :${REDIS_PORT}"; return; }
  mkdir -p "${YUKTI_INFRA}/logs"
  redis-server --daemonize yes --port "${REDIS_PORT}" --save '' --appendonly no \
    --logfile "${YUKTI_INFRA}/logs/redis.log" || die "redis failed to start"
  for i in $(seq 1 15); do is_up && { ok "redis up on :${REDIS_PORT}"; return; }; sleep 1; done
  die "redis did not come up"
}

stop()   { redis-cli -p "${REDIS_PORT}" shutdown nosave 2>/dev/null || true; ok "redis stopped"; }
status() { is_up && ok "redis running" || warn "redis down"; }
reset()  { redis-cli -p "${REDIS_PORT}" flushall >/dev/null 2>&1 || true; ok "redis flushed"; }

case "${1:-start}" in
  start) start ;; stop) stop ;; status) status ;; reset) reset ;;
  *) die "usage: redis.sh {start|stop|status|reset}" ;;
esac
