#!/usr/bin/env bash
# Start/stop Yukti's own services (as opposed to infrastructure).
#
# Processes are tracked by pidfile rather than by `pkill -f <pattern>`, which
# also matches the shell whose own argv contains the pattern — a mistake that
# kills the caller instead of the target.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

RUN="${YUKTI_INFRA}/run"
LOGS="${YUKTI_INFRA}/logs"
mkdir -p "${RUN}" "${LOGS}"

start_one() { # name, port, command...
  local name="$1" port="$2"; shift 2
  local pidfile="${RUN}/${name}.pid"
  if [[ -f "${pidfile}" ]] && kill -0 "$(cat "${pidfile}")" 2>/dev/null; then
    ok "${name} already running (pid $(cat "${pidfile}"))"; return
  fi
  nohup "$@" > "${LOGS}/${name}.log" 2>&1 &
  echo $! > "${pidfile}"
  for _ in $(seq 1 40); do
    curl -sf "http://localhost:${port}/health" >/dev/null 2>&1 && {
      ok "${name} up on :${port}"; return; }
    sleep 0.5
  done
  warn "${name} did not report healthy — see ${LOGS}/${name}.log"
}

stop_one() {
  local pidfile="${RUN}/$1.pid"
  [[ -f "${pidfile}" ]] || { warn "$1 not running"; return; }
  kill "$(cat "${pidfile}")" 2>/dev/null || true
  rm -f "${pidfile}"
  ok "$1 stopped"
}

case "${1:-up}" in
  up)
    export PYTHONPATH="${YUKTI_ROOT}/control:${YUKTI_ROOT}/datagen:${YUKTI_ROOT}/sandbox"
    export YUKTI_WEBHOOK_SECRET="${YUKTI_WEBHOOK_SECRET:-yukti_dev_webhook_secret}"
    start_one ingest-gw 9100 "${YUKTI_ROOT}/edge/bin/ingest-gw"
    start_one sandbox   8081 "${YUKTI_ROOT}/.venv/bin/python" -m uvicorn \
      yukti_sandbox.app:app --host 127.0.0.1 --port 8081
    start_one api       8080 "${YUKTI_ROOT}/.venv/bin/python" -m uvicorn \
      yukti.api.main:app --host 127.0.0.1 --port 8080
    ;;
  down) for s in api sandbox ingest-gw; do stop_one "$s"; done ;;
  status)
    for s in ingest-gw:9100 sandbox:8081 api:8080; do
      n="${s%%:*}"; p="${s##*:}"
      curl -sf "http://localhost:${p}/health" >/dev/null 2>&1 \
        && ok "${n} healthy on :${p}" || warn "${n} down"
    done
    ;;
  *) die "usage: services.sh {up|down|status}" ;;
esac
