package webhook

import (
	"strings"
	"testing"
	"time"
)

func fixedNow(t time.Time) func() time.Time { return func() time.Time { return t } }

func TestVerifyAcceptsGoodSignature(t *testing.T) {
	v := NewVerifier("s3cret", 5*time.Minute)
	body := []byte(`{"event_type":"payment.failed","amount":100}`)
	if err := v.Verify(body, v.Sign(body)); err != nil {
		t.Fatalf("expected valid signature to verify, got %v", err)
	}
}

func TestVerifyRejectsTamperedBody(t *testing.T) {
	v := NewVerifier("s3cret", 5*time.Minute)
	sig := v.Sign([]byte(`{"amount":100}`))
	// The attack this stops: replaying a captured signature against a body whose
	// amount has been changed.
	if err := v.Verify([]byte(`{"amount":999999}`), sig); err != ErrBadSignature {
		t.Fatalf("tampered body must be rejected, got %v", err)
	}
}

func TestVerifyIsSensitiveToByteLevelChanges(t *testing.T) {
	// Guards the re-serialisation bug: semantically identical JSON with
	// different whitespace or key order is a different message.
	v := NewVerifier("s3cret", 5*time.Minute)
	sig := v.Sign([]byte(`{"a":1,"b":2}`))
	for _, variant := range []string{`{"a": 1, "b": 2}`, `{"b":2,"a":1}`, `{"a":1,"b":2} `} {
		if err := v.Verify([]byte(variant), sig); err != ErrBadSignature {
			t.Fatalf("variant %q must not verify against the original signature", variant)
		}
	}
}

func TestVerifyRejectsWrongSecretAndEmptySignature(t *testing.T) {
	body := []byte(`{"x":1}`)
	good := NewVerifier("right", 5*time.Minute)
	bad := NewVerifier("wrong", 5*time.Minute)
	if err := good.Verify(body, bad.Sign(body)); err != ErrBadSignature {
		t.Fatal("signature from a different secret must be rejected")
	}
	if err := good.Verify(body, ""); err != ErrBadSignature {
		t.Fatal("empty signature must be rejected")
	}
	if err := good.Verify(body, strings.Repeat("a", 64)); err != ErrBadSignature {
		t.Fatal("arbitrary hex must be rejected")
	}
}

func TestReplayWindow(t *testing.T) {
	now := time.Date(2026, 8, 22, 12, 0, 0, 0, time.UTC)
	v := NewVerifier("s", 5*time.Minute)
	v.Now = fixedNow(now)

	cases := []struct {
		name string
		ts   time.Time
		want error
	}{
		{"fresh", now.Add(-10 * time.Second), nil},
		{"just inside", now.Add(-4 * time.Minute), nil},
		{"just outside", now.Add(-6 * time.Minute), ErrStale},
		{"ancient replay", now.Add(-72 * time.Hour), ErrStale},
		{"modest skew ahead", now.Add(30 * time.Second), nil},
		{"implausibly future", now.Add(48 * time.Hour), ErrStale},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := v.CheckFreshness(tc.ts.Unix()); got != tc.want {
				t.Fatalf("want %v, got %v", tc.want, got)
			}
		})
	}
}

func TestMissingTimestampRejected(t *testing.T) {
	// A webhook with no timestamp cannot be replay-checked, so it is refused
	// rather than waved through.
	v := NewVerifier("s", 5*time.Minute)
	if err := v.CheckFreshness(0); err != ErrMissingTS {
		t.Fatalf("want ErrMissingTS, got %v", err)
	}
}
