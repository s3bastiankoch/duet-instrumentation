// Package duetspan is the Duet Instrumentation span marker. Copy unchanged
// into base and head. Writes {"span","ns"} lines to $DUET_SPAN_FILE; does
// nothing without it. Call duetspan.Close() before os.Exit or on SIGTERM.
package duetspan

import (
	"os"
	"strconv"
	"sync"
	"time"
)

var (
	file = os.Getenv("DUET_SPAN_FILE")
	mu   sync.Mutex
	buf  []byte
	n    int
)

func init() {
	if file == "" {
		return
	}
	go func() {
		for range time.Tick(time.Second) {
			Close()
		}
	}()
}

type Token struct {
	name string
	t0   time.Time
}

func Start(name string) Token {
	if file == "" {
		return Token{}
	}
	return Token{name, time.Now()}
}

func End(t Token) {
	if t.name == "" {
		return
	}
	ns := time.Since(t.t0).Nanoseconds()
	mu.Lock()
	buf = append(buf, `{"span":"`...)
	buf = append(buf, t.name...)
	buf = append(buf, `","ns":`...)
	buf = strconv.AppendInt(buf, ns, 10)
	buf = append(buf, "}\n"...)
	n++
	full := n >= 512
	mu.Unlock()
	if full {
		Close()
	}
}

// Close flushes the buffer to $DUET_SPAN_FILE.
func Close() {
	mu.Lock()
	out := buf
	buf, n = nil, 0
	mu.Unlock()
	if file == "" || len(out) == 0 {
		return
	}
	f, err := os.OpenFile(file, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	f.Write(out)
	f.Close()
}
