#!/usr/bin/env bash
# Brings the whole native stack up/down and reports honest health.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
HERE="$(dirname "${BASH_SOURCE[0]}")"

case "${1:-up}" in
  up)
    log "bringing up yukti local stack (native — no docker daemon required)"
    "${HERE}/postgres.sh" start
    "${HERE}/redis.sh"    start
    "${HERE}/kafka.sh"    start
    echo
    log "stack ready"
    ;;
  down)
    "${HERE}/kafka.sh"    stop
    "${HERE}/redis.sh"    stop
    "${HERE}/postgres.sh" stop
    ;;
  status)
    "${HERE}/postgres.sh" status
    "${HERE}/redis.sh"    status
    "${HERE}/kafka.sh"    status
    ;;
  reset)
    "${HERE}/kafka.sh"    reset
    "${HERE}/redis.sh"    reset
    "${HERE}/postgres.sh" reset
    "${HERE}/kafka.sh"    start
    ;;
  *) die "usage: stack.sh {up|down|status|reset}" ;;
esac
