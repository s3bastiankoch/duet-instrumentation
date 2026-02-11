from __future__ import annotations

import json
import math
from pathlib import Path

percentages = [0, 1, 2, 3, 5, 10, 20, 50, 100]

file_templates = [
    # E3 /top/user/:userId/count (S3): ALS regression in internal loop.
    {
        "path": "examples/movie/src/filtering/als.ts",
        "change_type": "modified",
        "perf_relevant": True,
        "summary": "Increase ALS item-update loop iterations by {pct}% to stress performance",
         # Loop scope: update item factors loop.
        "perfect_span": {"start_line": 205, "end_line": 220},
        "diff_unified": "--- examples/movie/src/filtering/als.ts\n+++ examples/movie/src/filtering/als.ts\n@@ -202,7 +202,7 @@\n \n     // Update item factors.\n-    for (let jIndex = 0; jIndex <  n; jIndex++) {{\n+    for (let jIndex = 0; jIndex < Math.ceil(n * {factor}); jIndex++) {{\n       const j = jIndex % n;\n       let A = zeros(numFactors, numFactors);\n       let b = new Array(numFactors).fill(0);\n       for (let i = 0; i < m; i++) {{"
    },
    # E2 /top/region/:region/count (S2): extra shuffle work in random walk.
    {
        "path": "examples/movie/src/filtering/random-walk.ts",
        "change_type": "modified",
        "perf_relevant": True,
        "summary": "Increase random-walk shuffle iterations by {pct}% to stress performance",
        # Loop scope: initialize scores loop.
        "perfect_span": {"start_line": 41, "end_line": 44},
        "diff_unified": "--- examples/movie/src/filtering/random-walk.ts\n+++ examples/movie/src/filtering/random-walk.ts\n@@ -43,1 +43,1 @@\n-    graph[movie] = shuffleArray(graph[movie], 1050);\n+    graph[movie] = shuffleArray(graph[movie], Math.ceil(1050 * {factor}));"
    },
    # E1 /search (S1): bump Orama limit (more rerank work).
    {
        "path": "examples/movie/src/search.ts",
        "change_type": "modified",
        "perf_relevant": True,
        "summary": "Increase search work by {pct}% by bumping Orama limit for reranking",
        # NOTE: This string is formatted with Python's .format(), so any literal
        # braces must be escaped as {{ and }}.
        # Call scope: full search invocation.
        "perfect_span": {"start_line": 91, "end_line": 146},
        "diff_unified": "--- examples/movie/src/search.ts\n+++ examples/movie/src/search.ts\n@@ -112,9 +112,9 @@\n     vector: {{\n       value: queryEmbedding,\n       property: \"embedding\",\n     }},\n-    limit: 1000,\n+    limit: {orama_limit},\n     includeVectors: true,\n     similarity: 0,\n     threshold: 0,\n     tolerance: 10000,\n   }});\n"
    },
    # Non-perf-relevant: comment-only change (should NOT trigger span placement).
    {
        "path": "examples/movie/src/config.ts",
        "change_type": "modified",
        "perf_relevant": False,
        "summary": "Add clarifying comment in config (non-perf-relevant)",
        "perfect_span": {"start_line": 2, "end_line": 2},
        "diff_unified": "--- examples/movie/src/config.ts\n+++ examples/movie/src/config.ts\n@@ -1,3 +1,4 @@\n export function getMovieCount() {{\n+  // NOTE: Keep this in sync with seed data.\n   return 2000;\n }}\n"
    },
    # Non-perf-relevant: doc/comment change in model definitions.
    {
        "path": "examples/movie/src/model.ts",
        "change_type": "modified",
        "perf_relevant": False,
        "summary": "Add comment for schema field provenance (non-perf-relevant)",
        "perfect_span": {"start_line": 8, "end_line": 8},
        "diff_unified": "--- examples/movie/src/model.ts\n+++ examples/movie/src/model.ts\n@@ -7,6 +7,7 @@\n // TypeScript interfaces\n export interface IMovie {{\n+  // Source: seed data (see seed.json)\n   showId: string;\n   id: number;\n   original_language: string;\n   original_title?: string | null;\n   overview: string;\n"
    },
    # Non-perf-relevant: add a gated debug log (should NOT trigger span placement).
    {
        "path": "examples/movie/src/rate-limits.ts",
        "change_type": "modified",
        "perf_relevant": False,
        "summary": "Add optional debug log for rate-limit errors (non-perf-relevant)",
        "perfect_span": {"start_line": 34, "end_line": 34},
        "diff_unified": "--- examples/movie/src/rate-limits.ts\n+++ examples/movie/src/rate-limits.ts\n@@ -30,6 +30,7 @@\n     .then(() => {{\n       next();\n     }})\n     .catch((e) => {{\n+      console.debug(\"rateLimiter.consume failed\", e);\n       console.error(e);\n       res.status(429).send(\"Too Many Requests\");\n     }});\n }}\n"
    },
]

out_dir = Path(__file__).resolve().parents[1] / "examples" / "mock_git"
out_dir.mkdir(parents=True, exist_ok=True)

for pct in percentages:
    factor = 1 + (pct / 100.0)
    # Special case for 100% to avoid .0
    factor_str = "2.0" if pct == 100 else f"{factor:.2f}"
    orama_limit = int(math.ceil(1000 * factor))
    
    content = {
        "base": "examples/movie",
        "head": "examples/movie",
        "files": [],
    }

    for template in file_templates:
        file_entry = template.copy()
        file_entry["summary"] = file_entry["summary"].format(pct=pct)
        file_entry["diff_unified"] = file_entry["diff_unified"].format(
            factor=factor_str,
            orama_limit=orama_limit,
        )
        content["files"].append(file_entry)
    
    filename = out_dir / f"movie_perf_{pct}pct.json"
    filename.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {filename}")
