// Package dedup provides the fast-path duplicate check for inbound webhooks.
package dedup

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
)

// Deduper reports whether an event id has been seen before.
type Deduper interface {
	// FirstSight returns true the first time an id is presented, false on any
	// subsequent presentation.
	FirstSight(ctx context.Context, eventID string) (bool, error)
}

type RedisDeduper struct {
	client *redis.Client
	ttl    time.Duration
}

func NewRedis(addr string, ttl time.Duration) *RedisDeduper {
	return &RedisDeduper{
		client: redis.NewClient(&redis.Options{Addr: addr}),
		ttl:    ttl,
	}
}

// KeyPrefix namespaces the edge's dedup keys.
//
// This MUST NOT collide with any other stage's dedup namespace. Yukti dedups at
// two independent points — the edge (has this webhook been delivered to us?)
// and the control plane (have we already formed an opportunity from it?) — and
// they answer different questions about the same event id. Sharing a keyspace
// makes the first stage's key poison the second, so the control plane sees the
// key the edge just set and discards every event as a duplicate. That produced
// a pipeline that ingested 8,775 events and opened zero cases.
const KeyPrefix = "yukti:edge:evt:"

// FirstSight uses SET NX EX: atomic test-and-set in one round trip, so two
// concurrent deliveries of the same webhook cannot both observe "not seen".
// A GET-then-SET would race.
func (d *RedisDeduper) FirstSight(ctx context.Context, eventID string) (bool, error) {
	return d.client.SetNX(ctx, KeyPrefix+eventID, 1, d.ttl).Result()
}

func (d *RedisDeduper) Close() error { return d.client.Close() }

// FailOpen wraps a Deduper so that Redis being unavailable does not reject
// traffic.
//
// This is a deliberate availability/correctness trade. Redis here is a cache in
// front of a durable dedup table in Postgres, so failing open costs a little
// duplicate work downstream, where it is caught. Failing closed would drop real
// payment events during a Redis blip, which is strictly worse — and unlike
// duplicate work, a dropped event is unrecoverable.
type FailOpen struct{ Inner Deduper }

func (f FailOpen) FirstSight(ctx context.Context, eventID string) (bool, error) {
	ok, err := f.Inner.FirstSight(ctx, eventID)
	if err != nil {
		return true, nil // treat as unseen; the durable layer will catch it
	}
	return ok, nil
}
