#  NOT APPLIED — see infra/README.md.
#
#  There is no cloud account behind this and `terraform apply` has never been
#  run against it. It is here to show the shape of the managed dependencies and
#  the decisions inside them, not to be executed.
#
#  No state backend is configured: which bucket and which lock table is an
#  organisational choice rather than an architectural one, and inventing values
#  would make this look deployable when it is not.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "region" {
  description = "Payment obligations here are India-resident, so the default is Mumbai. Data residency is the constraint, not latency."
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  type = string
}

locals {
  name = "yukti-${var.environment}"
  tags = {
    Project = "yukti"
    Note    = "design artefact — never applied"
  }
}

# --- source of truth --------------------------------------------------------
#
# Postgres holds the outbox, the idempotency ledger, the hash-chained audit log
# and the agent event store. All four need real ACID guarantees, which is why
# this is not a document store and why the outbox pattern works at all: the
# decision and its publication commit in one transaction, so there is no dual
# write to get wrong.

resource "aws_db_instance" "primary" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.r6g.xlarge"

  allocated_storage     = 100
  max_allocated_storage = 1000
  storage_encrypted     = true

  # Money moves through this. A single-AZ database that loses an hour of the
  # idempotency ledger can re-dispatch actions that already went out.
  multi_az                = true
  backup_retention_period = 30
  deletion_protection     = true

  # Never in the state file. Injected from the secret store at apply time.
  manage_master_user_password = true

  performance_insights_enabled = true
  tags                         = local.tags
}

# --- the log ----------------------------------------------------------------
#
# Partitions must exceed the merchant count: ordering is only required WITHIN a
# merchant, and keying by merchant_id gives that for free while letting
# consumers scale out. Retention is long because replay from offset is how the
# evaluation re-runs identical cases across arms — the log is not a buffer here,
# it is the record.

resource "aws_msk_cluster" "events" {
  cluster_name           = local.name
  kafka_version          = "3.7.x"
  number_of_broker_nodes = 3

  broker_node_group_info {
    instance_type = "kafka.m5.large"
    storage_info {
      ebs_storage_info { volume_size = 500 }
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  tags = local.tags
}

# --- counters ---------------------------------------------------------------
#
# Fatigue counters with TTL, rate limits, the webhook dedup set, and dispatch
# locks. Everything here is reconstructible or short-lived by design — Redis is
# never the only thing standing between a duplicate webhook and a duplicate
# action, because a Postgres unique index is the backstop.

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id = local.name
  description          = "yukti fatigue counters, dedup set, dispatch locks"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.r6g.large"

  num_cache_clusters         = 2
  automatic_failover_enabled = true
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true

  tags = local.tags
}

# --- what is deliberately absent --------------------------------------------
#
#  * No EKS cluster. The manifests in ../k8s assume one exists; which cluster,
#    and whether it is shared with other workloads, is an organisational
#    decision.
#  * No secret resources. Secrets are referenced by name in the manifests and
#    created out of band. A Terraform file that creates a secret puts its value
#    in the state file.
#  * No autoscaling for Kafka consumers. Consumer-group rebalancing under an
#    aggressive HPA costs more than the throughput it buys, and the right
#    thresholds have not been measured — so they are not invented here.
