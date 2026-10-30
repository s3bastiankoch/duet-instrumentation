#!/usr/bin/env python3
"""Compares base and head after a duet run, per span and end to end, with the
test from the paper: the relative median of paired changes,
d_i = head_i / base_i - 1, and a bootstrap confidence interval for it. A
change is reported when the interval excludes 0.

    python3 analyze.py --base-spans .duet/base-spans.jsonl --head-spans .duet/head-spans.jsonl \
        --e2e .duet/e2e.jsonl [--plan .duet/plan.json] [--alpha 0.05] [--json .duet/report.json]

Span files hold one line per span end: {"span": "<name>", "ns": <duration in ns>}.
Span samples are paired by occurrence: the k-th time a span ends in base is
paired with the k-th time in head. The paired load sends the same requests in
the same order to both, so this lines up as long as both versions take the
same code paths. Above 20,000 pairs the interval is the distribution-free
order-statistic interval for the median, which the bootstrap converges to and
which takes milliseconds instead of minutes. Standard library only.
"""
import argparse
import json
import math
import random
import statistics
from collections import defaultdict


def load_spans(path):
    d = defaultdict(list)
    for line in open(path):
        if line.strip():
            r = json.loads(line)
            d[r["span"]].append(float(r["ns"]))
    return d


def rel_median_ci(base, head, alpha, n_boot, seed):
    k = min(len(base), len(head))
    d = [h / b - 1.0 for b, h in zip(base[:k], head[:k]) if b > 0]
    if len(d) < 30:
        return None
    med = statistics.median(d)
    n = len(d)
    if n > 20000:
        s = sorted(d)
        z = statistics.NormalDist().inv_cdf(1 - alpha / 2)
        half = z * math.sqrt(n) / 2
        lo, hi = s[max(0, math.floor(n / 2 - half))], s[min(n - 1, math.ceil(n / 2 + half))]
    else:
        rng = random.Random(seed)
        boots = sorted(statistics.median(rng.choices(d, k=n)) for _ in range(n_boot))
        lo = boots[int(n_boot * alpha / 2)]
        hi = boots[int(n_boot * (1 - alpha / 2)) - 1]
    return dict(n=len(d), unpaired=abs(len(base) - len(head)), median_pct=med * 100, lo_pct=lo * 100, hi_pct=hi * 100,
                base_median=statistics.median(base[:k]), head_median=statistics.median(head[:k]))


def verdict(r):
    if r is None:
        return "too few samples"
    if r["lo_pct"] > 0:
        return "slower"
    if r["hi_pct"] < 0:
        return "faster"
    return "no change"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-spans")
    ap.add_argument("--head-spans")
    ap.add_argument("--e2e")
    ap.add_argument("--plan", help="plan.json from the locate step, for the reason column")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--json")
    a = ap.parse_args()

    why = {}
    if a.plan:
        for s in json.load(open(a.plan)).get("spans", []):
            why[s["name"]] = s.get("reason", "")
    rows = []
    if a.base_spans and a.head_spans:
        b, h = load_spans(a.base_spans), load_spans(a.head_spans)
        for name in sorted(set(b) | set(h)):
            if name not in b or name not in h:
                rows.append(dict(kind="span", name=name, result=None,
                                 verdict="only in " + ("head" if name in h else "base")))
                continue
            r = rel_median_ci(b[name], h[name], a.alpha, a.boot, 1)
            rows.append(dict(kind="span", name=name, result=r, verdict=verdict(r)))
    if a.e2e:
        by = defaultdict(lambda: ([], []))
        for line in open(a.e2e):
            if line.strip():
                r = json.loads(line)
                if r["base_status"] == r["head_status"] and r["base_status"]:
                    by[r["name"]][0].append(r["base_ms"])
                    by[r["name"]][1].append(r["head_ms"])
        for name, (bb, hh) in sorted(by.items()):
            r = rel_median_ci(bb, hh, a.alpha, a.boot, 2)
            rows.append(dict(kind="end-to-end", name=name, result=r, verdict=verdict(r)))

    conf = int(round((1 - a.alpha) * 100))
    print(f"| | name | base median | head median | change | {conf}% interval | n | verdict | reason |")
    print("|---|---|---:|---:|---:|---|---:|---|---|")
    for row in rows:
        r, unit = row["result"], (1e6 if row["kind"] == "span" else 1.0)
        if r:
            print(f"| {row['kind']} | `{row['name']}` | {r['base_median'] / unit:.3f} ms | {r['head_median'] / unit:.3f} ms | "
                  f"{r['median_pct']:+.2f}% | [{r['lo_pct']:+.2f}%, {r['hi_pct']:+.2f}%] | {r['n']} | **{row['verdict']}** | {why.get(row['name'], '')} |")
        else:
            print(f"| {row['kind']} | `{row['name']}` | | | | | | {row['verdict']} | {why.get(row['name'], '')} |")
    unpaired = [row["name"] for row in rows if row["result"] and row["result"]["unpaired"] > 0.05 * row["result"]["n"]]
    if unpaired:
        print(f"\nwarning: span counts differ by more than 5% for {', '.join(unpaired)}; "
              "the versions may take different paths, so pairing by occurrence is approximate there")
    if a.json:
        json.dump(dict(alpha=a.alpha, rows=rows, reasons=why), open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
