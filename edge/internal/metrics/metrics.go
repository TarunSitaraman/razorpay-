// Package metrics exposes Prometheus text-format counters.
//
// Hand-rolled rather than pulling the client library: the edge exports a dozen
// counters, and a Prometheus/Grafana server cannot run in the target
// environment anyway (GitHub release binaries are blocked there). The exposition
// format is stable and trivial, so the dependency would buy nothing.
package metrics

import (
	"fmt"
	"net/http"
	"sort"
	"sync"
)

type Registry struct {
	mu       sync.RWMutex
	counters map[string]float64
	help     map[string]string
}

func New() *Registry {
	return &Registry{counters: map[string]float64{}, help: map[string]string{}}
}

func (r *Registry) Describe(name, help string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.help[name] = help
}

func (r *Registry) Inc(name string, labels ...string) { r.Add(name, 1, labels...) }

func (r *Registry) Add(name string, v float64, labels ...string) {
	key := name
	if len(labels) >= 2 {
		key = fmt.Sprintf(`%s{%s="%s"}`, name, labels[0], labels[1])
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.counters[key] += v
}

func (r *Registry) Get(key string) float64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.counters[key]
}

func (r *Registry) Handler() http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		r.mu.RLock()
		defer r.mu.RUnlock()
		keys := make([]string, 0, len(r.counters))
		for k := range r.counters {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		emitted := map[string]bool{}
		for _, k := range keys {
			base := k
			if i := indexByte(k, '{'); i >= 0 {
				base = k[:i]
			}
			if h, ok := r.help[base]; ok && !emitted[base] {
				fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s counter\n", base, h, base)
				emitted[base] = true
			}
			fmt.Fprintf(w, "%s %g\n", k, r.counters[k])
		}
	}
}

func indexByte(s string, b byte) int {
	for i := 0; i < len(s); i++ {
		if s[i] == b {
			return i
		}
	}
	return -1
}
