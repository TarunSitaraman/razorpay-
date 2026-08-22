#!/usr/bin/env bash
# System Postgres 16 cluster + a dedicated yukti role/database.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

is_up() { pg_isready -h "${YUKTI_PGHOST}" -p "${YUKTI_PGPORT}" -q 2>/dev/null; }

start() {
  if ! is_up; then
    log "starting postgres cluster ${PGCLUSTER_VER}/${PGCLUSTER_NAME}"
    pg_ctlcluster "${PGCLUSTER_VER}" "${PGCLUSTER_NAME}" start 2>/dev/null || true
    for i in $(seq 1 20); do is_up && break; sleep 1; done
  fi
  is_up || die "postgres did not come up"

  # Idempotent role + database creation. Run as the postgres superuser via peer auth.
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${YUKTI_PGUSER}'\"" 2>/dev/null | grep -q 1 || \
    su postgres -c "psql -q -c \"CREATE ROLE ${YUKTI_PGUSER} LOGIN PASSWORD '${YUKTI_PGPASSWORD}' CREATEDB\"" >/dev/null
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${YUKTI_PGDATABASE}'\"" 2>/dev/null | grep -q 1 || \
    su postgres -c "psql -q -c \"CREATE DATABASE ${YUKTI_PGDATABASE} OWNER ${YUKTI_PGUSER}\"" >/dev/null
  ok "postgres up on ${YUKTI_PGHOST}:${YUKTI_PGPORT}, db=${YUKTI_PGDATABASE}"
}

stop()   { pg_ctlcluster "${PGCLUSTER_VER}" "${PGCLUSTER_NAME}" stop 2>/dev/null || true; ok "postgres stopped"; }
status() { is_up && ok "postgres running" || warn "postgres down"; }
reset()  { su postgres -c "psql -q -c \"DROP DATABASE IF EXISTS ${YUKTI_PGDATABASE}\"" >/dev/null; start; ok "database reset"; }
psql_()  { PGPASSWORD="${YUKTI_PGPASSWORD}" psql -h "${YUKTI_PGHOST}" -p "${YUKTI_PGPORT}" -U "${YUKTI_PGUSER}" -d "${YUKTI_PGDATABASE}" "$@"; }

case "${1:-start}" in
  start) start ;; stop) stop ;; status) status ;; reset) reset ;;
  psql) shift; psql_ "$@" ;;
  *) die "usage: postgres.sh {start|stop|status|reset|psql}" ;;
esac
