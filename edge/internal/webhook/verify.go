// Package webhook verifies inbound payment webhooks.
//
// This is the untrusted boundary: the one endpoint reachable by anything other
// than our own services. Everything here is written on the assumption that the
// caller is hostile.
package webhook

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"time"
)

var (
	ErrBadSignature = errors.New("signature mismatch")
	ErrStale        = errors.New("event outside replay window")
	ErrMissingTS    = errors.New("event carries no timestamp")
)

// Verifier checks signatures and rejects replays.
type Verifier struct {
	Secret string
	// ReplayWindow bounds how old a webhook may be. Deduplication alone does
	// not stop replay: an attacker who captures a valid body can resend it with
	// a fresh event id forever, and every dedup layer would correctly conclude
	// it had never seen that id. Only a timestamp bound closes it.
	ReplayWindow time.Duration
	// Now is injectable so the replay-window tests do not depend on wall clock.
	Now func() time.Time
}

func NewVerifier(secret string, window time.Duration) *Verifier {
	return &Verifier{Secret: secret, ReplayWindow: window, Now: time.Now}
}

// Sign returns the hex HMAC-SHA256 of the raw body, matching Razorpay's scheme.
func (v *Verifier) Sign(rawBody []byte) string {
	mac := hmac.New(sha256.New, []byte(v.Secret))
	mac.Write(rawBody)
	return hex.EncodeToString(mac.Sum(nil))
}

// Verify checks the signature over the RAW body bytes.
//
// The caller must pass the exact bytes read off the wire. Re-marshalling parsed
// JSON changes key order and whitespace, so a signature computed over the
// re-serialised form will not match — a bug Razorpay's own documentation warns
// about explicitly.
func (v *Verifier) Verify(rawBody []byte, signature string) error {
	expected := v.Sign(rawBody)
	// hmac.Equal, never ==. A byte-wise comparison returns on first mismatch,
	// leaking the correct prefix through timing and letting an attacker recover
	// a valid signature one byte at a time.
	if !hmac.Equal([]byte(expected), []byte(signature)) {
		return ErrBadSignature
	}
	return nil
}

// CheckFreshness rejects events outside the replay window.
//
// Future-dated events are rejected on the same bound: modest clock skew is
// tolerated, but an event claiming to be from next week is not a clock problem.
func (v *Verifier) CheckFreshness(createdAtUnix int64) error {
	if createdAtUnix == 0 {
		return ErrMissingTS
	}
	age := v.Now().Sub(time.Unix(createdAtUnix, 0))
	if age > v.ReplayWindow || age < -v.ReplayWindow {
		return ErrStale
	}
	return nil
}
