// Command ingest-gw is Yukti's untrusted boundary.
//
// It receives payment webhooks, proves they are authentic and fresh, drops
// duplicates, and produces them to Kafka keyed by merchant. Nothing downstream
// of this process trusts the network, and nothing upstream of it is trusted at
// all.
//
// Deliberately does no business logic. This binary is the piece that scales
// horizontally under a traffic spike, so it stays stateless, cheap and boring.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/tarunsitaraman/yukti/edge/internal/dedup"
	"github.com/tarunsitaraman/yukti/edge/internal/kafka"
	"github.com/tarunsitaraman/yukti/edge/internal/metrics"
	"github.com/tarunsitaraman/yukti/edge/internal/webhook"
)

// Bounded read on the one endpoint reachable from outside. An unbounded
// io.ReadAll on an attacker-controlled body is a one-line memory DoS.
const maxBodyBytes = 1 << 20 // 1 MiB

type server struct {
	verifier *webhook.Verifier
	deduper  dedup.Deduper
	producer *kafka.Producer
	metrics  *metrics.Registry
	log      *slog.Logger
}

type envelope struct {
	EventID    string `json:"event_id"`
	EventType  string `json:"event_type"`
	MerchantID string `json:"merchant_id"`
	CreatedAt  int64  `json:"created_at"`
}

func (s *server) handleWebhook(w http.ResponseWriter, r *http.Request) {
	s.metrics.Inc("yukti_edge_webhooks_received_total")

	if r.Method != http.MethodPost {
		s.reject(w, http.StatusMethodNotAllowed, "method_not_allowed")
		return
	}

	// Read the RAW bytes and verify against those exact bytes. Decoding first
	// and re-encoding to check the signature is the classic way to break
	// webhook verification: JSON round-trips do not preserve key order or
	// whitespace.
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			s.reject(w, http.StatusRequestEntityTooLarge, "body_too_large")
			return
		}
		s.reject(w, http.StatusBadRequest, "unreadable_body")
		return
	}

	sig := r.Header.Get("X-Razorpay-Signature")
	if err := s.verifier.Verify(body, sig); err != nil {
		// Log the failure without echoing the body: an unauthenticated payload
		// is attacker-controlled and must not be written into our logs, where
		// it could poison downstream log processing.
		s.log.Warn("signature rejected", "remote", r.RemoteAddr, "bytes", len(body))
		s.reject(w, http.StatusUnauthorized, "bad_signature")
		return
	}

	var env envelope
	if err := json.Unmarshal(body, &env); err != nil {
		s.reject(w, http.StatusBadRequest, "malformed_json")
		return
	}

	if err := s.verifier.CheckFreshness(env.CreatedAt); err != nil {
		// A correctly-signed but stale event is a replay. Dedup cannot catch it
		// because the attacker supplies a fresh event id.
		s.reject(w, http.StatusUnauthorized, "stale_event")
		return
	}
	if env.EventID == "" || env.MerchantID == "" {
		s.reject(w, http.StatusBadRequest, "missing_identifiers")
		return
	}

	first, _ := s.deduper.FirstSight(r.Context(), env.EventID)
	if !first {
		// Duplicates are a 200. A PSP that gets a non-2xx retries with backoff,
		// so returning an error for something we have already handled would
		// generate more of exactly the traffic we are trying to suppress.
		s.metrics.Inc("yukti_edge_webhooks_duplicate_total")
		s.ok(w, "duplicate")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if err := s.producer.Produce(ctx, env.MerchantID, body, map[string]string{
		"event_id":   env.EventID,
		"event_type": env.EventType,
	}); err != nil {
		// Fail loudly so the PSP retries. Acking an event we did not durably
		// persist would silently lose a payment failure, which is the one
		// outcome this service exists to prevent.
		s.metrics.Inc("yukti_edge_produce_errors_total")
		s.log.Error("kafka produce failed", "event_id", env.EventID, "err", err)
		s.reject(w, http.StatusServiceUnavailable, "produce_failed")
		return
	}

	s.metrics.Inc("yukti_edge_webhooks_accepted_total")
	s.metrics.Inc("yukti_edge_events_by_type_total", "event_type", env.EventType)
	s.ok(w, "accepted")
}

func (s *server) ok(w http.ResponseWriter, status string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": status})
}

func (s *server) reject(w http.ResponseWriter, code int, reason string) {
	s.metrics.Inc("yukti_edge_webhooks_rejected_total", "reason", reason)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "rejected", "reason": reason})
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	brokers := strings.Split(env("YUKTI_KAFKA_BOOTSTRAP", "localhost:9092"), ",")
	topic := env("YUKTI_TOPIC_PAYMENTS", "payments.events")
	secret := env("YUKTI_WEBHOOK_SECRET", "yukti_dev_webhook_secret")
	addr := env("YUKTI_EDGE_ADDR", ":9100")

	producer, err := kafka.NewProducer(brokers, topic)
	if err != nil {
		log.Error("kafka producer init failed", "err", err)
		os.Exit(1)
	}
	defer producer.Close()

	reg := metrics.New()
	reg.Describe("yukti_edge_webhooks_received_total", "Webhooks received at the edge")
	reg.Describe("yukti_edge_webhooks_accepted_total", "Webhooks verified and produced")
	reg.Describe("yukti_edge_webhooks_duplicate_total", "Webhooks suppressed as duplicates")
	reg.Describe("yukti_edge_webhooks_rejected_total", "Webhooks rejected, by reason")
	reg.Describe("yukti_edge_events_by_type_total", "Accepted webhooks by event type")
	reg.Describe("yukti_edge_produce_errors_total", "Kafka produce failures")

	s := &server{
		verifier: webhook.NewVerifier(secret, 5*time.Minute),
		deduper:  dedup.FailOpen{Inner: dedup.NewRedis(env("YUKTI_REDIS_ADDR", "localhost:6379"), 7*24*time.Hour)},
		producer: producer,
		metrics:  reg,
		log:      log,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/webhooks/razorpay", s.handleWebhook)
	mux.Handle("/metrics", reg.Handler())
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Info("ingest-gw listening", "addr", addr, "topic", topic, "brokers", brokers)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("listen failed", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	// Drain in flight before exiting: a webhook accepted but not yet produced
	// would otherwise be acked and lost.
	log.Info("shutting down, draining in-flight requests")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
