// Duet Instrumentation span marker. Copy unchanged into base and head.
// Writes {"span","ns"} lines to $DUET_SPAN_FILE; does nothing without it.
import { appendFileSync } from "node:fs";

const file = process.env.DUET_SPAN_FILE;
let buf: string[] = [];

function flush(): void {
  if (!file || buf.length === 0) return;
  const out = buf.join("");
  buf = [];
  appendFileSync(file, out);
}

if (file) {
  setInterval(flush, 1000).unref();
  process.on("exit", flush);
  for (const [sig, code] of [["SIGTERM", 143], ["SIGINT", 130]] as const) {
    process.on(sig, () => {
      flush();
      if (process.listenerCount(sig) === 1) process.exit(code);
    });
  }
}

export type SpanToken = { name: string; t0: bigint } | null;

export function start(name: string): SpanToken {
  return file ? { name, t0: process.hrtime.bigint() } : null;
}

export function end(token: SpanToken): void {
  if (!token) return;
  const ns = process.hrtime.bigint() - token.t0;
  buf.push(`{"span":"${token.name}","ns":${ns}}\n`);
  if (buf.length >= 512) flush();
}
