# Span markers

One small helper per language. Copy the matching file into **both** versions
(base and head) at the same path, unchanged. Each helper does the same thing:

- `start("name")` reads a monotonic clock and returns a token;
- `end(token)` appends `{"span": "<name>", "ns": <duration>}` to an in-memory
  buffer;
- the buffer goes to the file named by `DUET_SPAN_FILE` every 512 spans,
  about once a second, and when the process exits.

Without `DUET_SPAN_FILE` the markers do nothing, so an instrumented build that
slips into production costs one clock read per span.

| Language | File | Use |
|---|---|---|
| TypeScript, JavaScript | `duet-span.ts` | `const t = start("search.rank"); try { … } finally { end(t); }` |
| Python | `duet_span.py` | `with span("search.rank"): …` |
| Go | `duetspan.go` | `defer duetspan.End(duetspan.Start("search.rank"))` |
| Java | `DuetSpan.java` | `try (var s = DuetSpan.start("search.rank")) { … }` |

For another language, write the same four lines: monotonic clock, buffer,
periodic flush, flush on exit. Keep names identical in both versions.
