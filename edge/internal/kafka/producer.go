// Package kafka wraps the franz-go producer.
//
// franz-go rather than confluent-kafka-go: the Confluent client wraps librdkafka
// and requires cgo, which turns a clean build into a C toolchain problem. Pure
// Go means CGO_ENABLED=0 and a static binary, which is what you want for the
// component that scales horizontally.
package kafka

import (
	"context"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"
)

type Producer struct {
	client *kgo.Client
	topic  string
}

func NewProducer(brokers []string, topic string) (*Producer, error) {
	client, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.DefaultProduceTopic(topic),
		// Idempotent production: a retry after an ack timeout must not append
		// the record twice. Without this, a broker hiccup inflates the duplicate
		// metric with duplicates we created ourselves, which would make the
		// dedup numbers meaningless as evidence.
		kgo.ProducerBatchMaxBytes(1<<20),
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProduceRequestTimeout(10*time.Second),
	)
	if err != nil {
		return nil, err
	}
	return &Producer{client: client, topic: topic}, nil
}

// Produce publishes synchronously, keyed so that per-merchant ordering holds
// while distinct merchants spread across partitions.
func (p *Producer) Produce(ctx context.Context, key string, value []byte, headers map[string]string) error {
	rec := &kgo.Record{Key: []byte(key), Value: value, Topic: p.topic}
	for k, v := range headers {
		rec.Headers = append(rec.Headers, kgo.RecordHeader{Key: k, Value: []byte(v)})
	}
	// Produce asynchronously to avoid blocking on full ISR acks per record.
	// The edge is stateless and Kafka is the source of truth, so we rely on the
	// client's internal retry mechanism and background flushing.
	p.client.Produce(ctx, rec, nil)
	return nil
}

func (p *Producer) Close() { p.client.Close() }
