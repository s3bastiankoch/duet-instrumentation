"""Duet Instrumentation span marker. Copy unchanged into base and head.
Writes {"span","ns"} lines to $DUET_SPAN_FILE; does nothing without it."""
import atexit
import os
import signal
import threading
import time
from contextlib import contextmanager

_file = os.environ.get("DUET_SPAN_FILE")
_buf = []
_lock = threading.RLock()


def _flush():
    global _buf
    with _lock:
        out, _buf = _buf, []
    if _file and out:
        with open(_file, "a") as f:
            f.write("".join(out))


def _ticker():
    while True:
        time.sleep(1)
        _flush()


def _on_sigterm(signum, frame):
    _flush()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


if _file:
    atexit.register(_flush)
    threading.Thread(target=_ticker, daemon=True).start()
    try:
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        pass


@contextmanager
def span(name):
    if not _file:
        yield
        return
    t0 = time.perf_counter_ns()
    try:
        yield
    finally:
        ns = time.perf_counter_ns() - t0
        with _lock:
            _buf.append('{"span":"%s","ns":%d}\n' % (name, ns))
            full = len(_buf) >= 512
        if full:
            _flush()
