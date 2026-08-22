#!/usr/bin/env bash
# Single-node Kafka in KRaft mode. No ZooKeeper, no Docker — just a JVM.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Kafka is localhost-only; the harness JVM proxy settings only add noise here.
unset JAVA_TOOL_OPTIONS

fetch() {
  [[ -x "${KAFKA_HOME}/bin/kafka-server-start.sh" ]] && { ok "kafka ${KAFKA_VERSION} present"; return; }
  log "downloading kafka ${KAFKA_VERSION}"
  mkdir -p "${YUKTI_INFRA}"
  curl -sSL --retry 3 -o "${YUKTI_INFRA}/kafka.tgz" "${KAFKA_URL}" || die "download failed"
  tar xzf "${YUKTI_INFRA}/kafka.tgz" -C "${YUKTI_INFRA}"
  mv "${YUKTI_INFRA}/kafka_${KAFKA_SCALA}-${KAFKA_VERSION}" "${KAFKA_HOME}"
  rm -f "${YUKTI_INFRA}/kafka.tgz"
  ok "kafka ${KAFKA_VERSION} installed"
}

is_up() { "${KAFKA_HOME}/bin/kafka-broker-api-versions.sh" --bootstrap-server "${KAFKA_BOOTSTRAP}" >/dev/null 2>&1; }

start() {
  fetch
  is_up && { ok "kafka already running on ${KAFKA_BOOTSTRAP}"; return; }
  mkdir -p "${KAFKA_DATA}" "$(dirname "${KAFKA_LOG}")"

  local cfg="${YUKTI_INFRA}/kafka.properties"
  cat > "${cfg}" <<PROPS
process.roles=broker,controller
node.id=1
controller.quorum.bootstrap.servers=localhost:9093
listeners=PLAINTEXT://:9092,CONTROLLER://:9093
advertised.listeners=PLAINTEXT://localhost:9092
controller.listener.names=CONTROLLER
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
inter.broker.listener.name=PLAINTEXT
log.dirs=${KAFKA_DATA}
num.partitions=${YUKTI_TOPIC_PARTITIONS}
# Single node, so every replication factor must be 1 or the broker refuses to start.
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
# 14 days: the evaluation harness replays the full 90-day stream from offset 0,
# so retention must outlive a demo session comfortably.
log.retention.hours=336
auto.create.topics.enable=false
group.initial.rebalance.delay.ms=0
PROPS

  if [[ ! -f "${KAFKA_DATA}/meta.properties" ]]; then
    local cid; cid="$("${KAFKA_HOME}/bin/kafka-storage.sh" random-uuid)"
    "${KAFKA_HOME}/bin/kafka-storage.sh" format -t "${cid}" -c "${cfg}" --standalone >/dev/null \
      || die "kraft storage format failed"
    ok "formatted kraft storage (cluster ${cid})"
  fi

  log "starting kafka broker"
  nohup "${KAFKA_HOME}/bin/kafka-server-start.sh" "${cfg}" > "${KAFKA_LOG}" 2>&1 &
  echo $! > "${YUKTI_INFRA}/kafka.pid"

  for i in $(seq 1 45); do
    is_up && { ok "kafka up on ${KAFKA_BOOTSTRAP} (${i}s)"; create_topics; return; }
    sleep 1
  done
  die "kafka did not come up in 45s — see ${KAFKA_LOG}"
}

create_topics() {
  for t in ${YUKTI_TOPICS}; do
    "${KAFKA_HOME}/bin/kafka-topics.sh" --bootstrap-server "${KAFKA_BOOTSTRAP}" \
      --create --if-not-exists --topic "${t}" \
      --partitions "${YUKTI_TOPIC_PARTITIONS}" --replication-factor 1 >/dev/null 2>&1
  done
  ok "topics: $(echo ${YUKTI_TOPICS} | tr ' ' ',')"
}

stop() {
  [[ -f "${YUKTI_INFRA}/kafka.pid" ]] && kill "$(cat "${YUKTI_INFRA}/kafka.pid")" 2>/dev/null || true
  rm -f "${YUKTI_INFRA}/kafka.pid"
  ok "kafka stopped"
}

status() { is_up && ok "kafka running" || warn "kafka down"; }

reset() { stop; sleep 2; rm -rf "${KAFKA_DATA}"; ok "kafka data wiped"; }

case "${1:-start}" in
  start) start ;; stop) stop ;; status) status ;; reset) reset ;;
  topics) "${KAFKA_HOME}/bin/kafka-topics.sh" --bootstrap-server "${KAFKA_BOOTSTRAP}" --list ;;
  *) die "usage: kafka.sh {start|stop|status|reset|topics}" ;;
esac
