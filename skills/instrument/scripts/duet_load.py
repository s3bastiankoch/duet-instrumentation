#!/usr/bin/env python3
"""Paired HTTP load for a duet run: every request goes to base and head at the
same moment, alternating which one is sent first, so both versions see the
same load and the same noise.

    python3 duet_load.py --base http://localhost:18081 --head http://localhost:18082 \
        --requests requests.jsonl --duration 300 --warmup 30 --out .duet/e2e.jsonl

requests.jsonl holds one request per line and is replayed in a loop:
    {"method": "GET", "path": "/search?q=matrix"}
    {"method": "POST", "path": "/rank", "body": {"region": "eu"}, "headers": {"X-Key": "1"}}

Writes one line per pair: {"i", "name", "base_ms", "head_ms", "base_status", "head_status"}.
Standard library only.
"""
import argparse
import json
import threading
import time
import urllib.error
import urllib.request


def send(url, req, out, key):
    body = req.get("body")
    data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    headers.update(req.get("headers", {}))
    r = urllib.request.Request(url + req["path"], data=data, method=req.get("method", "GET"), headers=headers)
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = 0
    out[key] = ((time.perf_counter() - t) * 1000.0, status)


def pair(base, head, req, i):
    out = {}
    first, second = (("base", base), ("head", head)) if i % 2 == 0 else (("head", head), ("base", base))
    ts = [threading.Thread(target=send, args=(u, req, out, k)) for k, u in (first, second)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--requests", required=True)
    ap.add_argument("--duration", type=float, default=300, help="seconds of measured load")
    ap.add_argument("--warmup", type=float, default=30, help="seconds of load before measuring")
    ap.add_argument("--rate", type=float, default=0, help="pairs per second, 0 = back to back")
    ap.add_argument("--out", default=".duet/e2e.jsonl")
    ap.add_argument("--reset-spans", nargs="*", default=[], metavar="FILE",
                    help="span files to empty when the warm-up ends, so warm-up spans are not analysed")
    a = ap.parse_args()

    reqs = [json.loads(l) for l in open(a.requests) if l.strip()]
    if not reqs:
        raise SystemExit("no requests in " + a.requests)
    base, head = a.base.rstrip("/"), a.head.rstrip("/")
    i, start, gap = 0, time.monotonic(), (1.0 / a.rate if a.rate else 0.0)
    end_warm = start + a.warmup
    end = end_warm + a.duration
    bad = n = 0
    reset = bool(a.reset_spans)
    with open(a.out, "w") as f:
        while (now := time.monotonic()) < end:
            if reset and now >= end_warm:
                for p in a.reset_spans:
                    open(p, "w").close()
                reset = False
            req = reqs[i % len(reqs)]
            out = pair(base, head, req, i)
            if now >= end_warm:
                (bm, bs), (hm, hs) = out["base"], out["head"]
                bad += bs != hs
                n += 1
                f.write(json.dumps({"i": i, "name": req.get("name", req["path"].split("?")[0]),
                                    "base_ms": round(bm, 4), "head_ms": round(hm, 4),
                                    "base_status": bs, "head_status": hs}) + "\n")
            i += 1
            if gap:
                time.sleep(max(0.0, start + i * gap - time.monotonic()))
    print(f"{n} measured pairs after {a.warmup:.0f} s warm-up; {bad} pairs with different status codes")
    if bad:
        print("warning: base and head answered differently; check that both versions behave the same")


if __name__ == "__main__":
    main()
