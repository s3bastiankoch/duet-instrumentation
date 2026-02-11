import { AsyncLocalStorage } from "async_hooks";
import { randomUUID } from "crypto";
import { NextFunction, Request, Response } from "express";
import fs from "fs";
import { EventLoopUtilization, performance } from "perf_hooks";
import { Worker } from "worker_threads";

const inMemoryQueue: Measurement[] = [];
const inMemoryCurrentMeasurements: Map<string, MeasurementStart> = new Map();
const inMemoryActiveSpan:  Map<string, string[]> = new Map();
const FLUSH_INTERVAL = 5000;
const VERSION = "$SERVICE_VERSION";

const DEBUG = process.env.OTEL_AGENT_DEBUG === "true";
const CAPTURE_HTTP_BODY = process.env.OTEL_AGENT_CAPTURE_HTTP_BODY === "true";
const CAPTURE_HTTP_QUERY = process.env.OTEL_AGENT_CAPTURE_HTTP_QUERY === "true";
// Linux-only. Default on in debug; disable with OTEL_AGENT_CAPTURE_CPU_STEAL=false.
const CAPTURE_CPU_STEAL = DEBUG && process.env.OTEL_AGENT_CAPTURE_CPU_STEAL !== "false";
const CPU_SAMPLE_INTERVAL_MS = 100;
let lastLinuxCpuSampleAt = 0;
let lastLinuxCpuSample: LinuxCpuTotals | null = null;
let lastEluSampleAt = 0;
let lastEluSample: EventLoopUtilization | null = null;
// Graceful shutdown flushing. Default on; disable with OTEL_AGENT_HANDLE_SIGNALS=false.
const HANDLE_SIGNALS = process.env.OTEL_AGENT_HANDLE_SIGNALS !== "false";
const FLUSH_TIMEOUT_MS = Number(process.env.OTEL_AGENT_FLUSH_TIMEOUT_MS || 2000);

let flushWorker: Worker | null = null;
let flushInFlight = false;
let flushPending: Measurement[] = [];
let shutdownInProgress = false;
const flushWaiters: Array<() => void> = [];

function debugLog(msg: string) {
  if (DEBUG) console.log(msg);
}

type LinuxCpuTotals = {
  totalJiffies: number;
  stealJiffies: number;
  idleAllJiffies: number;
};

/**
 * Best-effort Linux CPU steal-time sampling via /proc/stat.
 * Returns null on non-Linux, missing /proc, or parse errors.
 *
 * /proc/stat first line fields:
 * cpu  user nice system idle iowait irq softirq steal guest guest_nice
 */
function readLinuxCpuTotals(): LinuxCpuTotals | null {
  if (!CAPTURE_CPU_STEAL) return null;
  if (process.platform !== "linux") return null;

  try {
    const now = Date.now();
    if (lastLinuxCpuSample && now - lastLinuxCpuSampleAt < CPU_SAMPLE_INTERVAL_MS) {
      return lastLinuxCpuSample;
    }
    const firstLine = fs.readFileSync("/proc/stat", "utf8").split("\n")[0];
    const parts = firstLine.trim().split(/\s+/);
    if (parts.length < 9 || parts[0] !== "cpu") return null;

    const values = parts.slice(1).map((v) => Number(v));
    // Any NaN indicates a parse issue; bail out.
    if (values.some((n) => !Number.isFinite(n))) return null;

    const totalJiffies = values.reduce((a, b) => a + b, 0);
    const stealJiffies = values[7] ?? 0; // 8th field = steal
    const idleJiffies = values[3] ?? 0; // 4th field = idle
    const iowaitJiffies = values[4] ?? 0; // 5th field = iowait (commonly treated as idle)
    const idleAllJiffies = idleJiffies + iowaitJiffies;

    lastLinuxCpuSampleAt = now;
    lastLinuxCpuSample = { totalJiffies, stealJiffies, idleAllJiffies };
    return lastLinuxCpuSample;
  } catch {
    return null;
  }
}

function readEventLoopUtilization(): EventLoopUtilization | null {
  if (!DEBUG) return null;
  const now = Date.now();
  if (lastEluSample && now - lastEluSampleAt < CPU_SAMPLE_INTERVAL_MS) {
    return lastEluSample;
  }
  lastEluSample = performance.eventLoopUtilization();
  lastEluSampleAt = now;
  return lastEluSample;
}

function ensureFlushWorker(): Worker {
  if (flushWorker) return flushWorker;

  // measure.ts and worker.js are siblings under src/ in the instrumented tree.
  // Use import.meta.url so this works regardless of current working directory.
  const workerUrl = new URL("./worker.js", import.meta.url);

  flushWorker = new Worker(workerUrl, {
    workerData: {
      serviceVersion: VERSION,
      outputDir: process.env.OTEL_AGENT_SPANS_DIR,
      debug: DEBUG,
    },
  });

  // Avoid keeping the process alive solely because of the worker.
  flushWorker.unref?.();

  flushWorker.on("message", (message) => {
    // Worker acks flushes; clear in-flight and drain any pending buffer.
    if (message && typeof message === "object" && (message as any).ok) {
      flushInFlight = false;
      tryFlushQueue("drain");
      maybeResolveFlushWaiters();
    }
    if (DEBUG) console.log(message);
  });
  flushWorker.on("error", (err) => {
    // If the worker dies, we fall back to buffering in memory.
    console.warn(`otel-agent worker error: ${err?.message || err}`);
    flushWorker = null;
    flushInFlight = false;
    maybeResolveFlushWaiters();
  });
  flushWorker.on("exit", (code) => {
    if (code !== 0) {
      console.warn(`otel-agent worker exited with code ${code}`);
    }
    flushWorker = null;
    flushInFlight = false;
    maybeResolveFlushWaiters();
  });

  return flushWorker;
}

function isFlushIdle(): boolean {
  return !flushInFlight && flushPending.length === 0 && inMemoryQueue.length === 0;
}

function maybeResolveFlushWaiters() {
  if (!isFlushIdle()) return;
  const waiters = flushWaiters.splice(0, flushWaiters.length);
  for (const w of waiters) {
    try {
      w();
    } catch {
      // ignore
    }
  }
}

function tryFlushQueue(reason: string) {
  if (inMemoryQueue.length > 0) {
    // Move current queue into pending without copying element-by-element.
    flushPending = flushPending.concat(inMemoryQueue.splice(0, inMemoryQueue.length));
  }
  if (flushPending.length === 0) return;
  if (flushInFlight) return;

  const worker = ensureFlushWorker();
  const batch = flushPending;
  flushPending = [];
  flushInFlight = true;

  debugLog(`otel-agent: flushing ${batch.length} spans (${reason})`);

  // Send to the long-lived worker for SQLite writes.
  worker.postMessage({ type: "flush", measurements: batch });
}

async function flushAndWait(reason: string, timeoutMs: number): Promise<boolean> {
  tryFlushQueue(reason);
  if (isFlushIdle()) return true;

  return await new Promise<boolean>((resolve) => {
    const onDone = () => {
      clearTimeout(timeout);
      resolve(true);
    };

    const timeout = setTimeout(() => {
      // Remove this waiter if it hasn't fired.
      const idx = flushWaiters.indexOf(onDone);
      if (idx !== -1) flushWaiters.splice(idx, 1);
      resolve(false);
    }, Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 2000);

    flushWaiters.push(onDone);
    // In case we became idle between scheduling and pushing.
    maybeResolveFlushWaiters();
  });
}

/**
 * Fallback normalization for routes when Express's `req.route.path` is unavailable.
 * Replaces purely-numeric path segments with `:id`.
 * For string parameters (e.g. :region), rely on Express's matched route pattern.
 * Examples:
 *   /movies/like/98      -> /movies/like/:id
 *   /users/123/profile   -> /users/:id/profile
 */
function normalizeHttpRoute(path: string): string {
  return path
    .split("/")
    .map((segment) => (/^\d+$/.test(segment) ? ":id" : segment))
    .join("/");
}

/**
 * Options for starting a span with additional context.
 * Follows OpenTelemetry semantic conventions where applicable.
 */
export type SpanOptions = {
  /** The function name (maps to OTel `code.function`) */
  functionName?: string;
  /** The type of operation: database, http, computation, file-io, cache, memory, network, other */
  operationType?: string;
  /** Additional custom attributes to attach to the span */
  attributes?: Record<string, string | number | boolean>;
};

export type MeasurementStart = {
  name: string;
  spanId: string;
  cpuStart: NodeJS.CpuUsage;
  linuxCpuStart: LinuxCpuTotals | null;
  requestId: string;
  start: number;
  timestamp: number;
  elu: EventLoopUtilization | null;
  parentSpanId: string | null;
  /** OTel `code.function` - the containing function name */
  functionName?: string;
  /** Custom attribute for operation categorization */
  operationType?: string;
  /** Additional custom attributes */
  customAttributes?: Record<string, string | number | boolean>;
};

// A measurement object compatible with the concept of a span in OpenTelemetry
export type Measurement = {
  // ISO 8601 format with UTC timezone
  timestamp: string;

  context: {
    // Trace ID
    trace_id: string;
    // Checkpoint name
    span_id: string;
  };

  parent_id: string | null;

  attributes: {
    // Semantic conventions of the service attributes
    "service.name": string;
    "service.version": string; // The version of the service, in our case the commit hash

    // Semantic conventions of the request attributes
    "http.method": string;
    "http.route": string;
    "http.query": string;
    "http.body"?: string;

    "http.server.request.duration"?: number; // Measured in seconds, following the UCUM format

    "process.pid": number;

    "host.arch": string;

    "os.type": string;

    "experiment.id": number;

    "cpu.time.system": number;
    "cpu.time.user": number;

    "wall.time": number;

    "warmup"?: boolean;

    // OTel code semantic conventions (https://opentelemetry.io/docs/specs/semconv/attributes-registry/code/)
    "code.function"?: string;

    // Custom operation type for categorization
    "operation.type"?: string;

    // Allow additional custom attributes
    [key: string]: string | number | boolean | undefined;
  };
};

/**
 * Periodically flush the in-memory queue to a persistent storage (e.g. a file or a database)
 */
const flushTimer = setInterval(() => {
  tryFlushQueue("interval");
}, FLUSH_INTERVAL);
// `setInterval` returns `number` in DOM typings, but `NodeJS.Timeout` in Node.
// Make this safe across both by checking at runtime.
if (typeof flushTimer === "object" && flushTimer && "unref" in flushTimer) {
  (flushTimer as unknown as { unref: () => void }).unref();
}

// If the process exits, flush the in-memory queue
process.on("beforeExit", () => {
  // Best-effort flush. We avoid async blocking here.
  tryFlushQueue("beforeExit");
});

if (HANDLE_SIGNALS) {
  const handleSignal = (signal: "SIGTERM" | "SIGINT") => {
    if (shutdownInProgress) return;
    shutdownInProgress = true;
    debugLog(`otel-agent: received ${signal}, flushing spans (timeout ${FLUSH_TIMEOUT_MS}ms)`);

    void flushAndWait(`signal:${signal}`, FLUSH_TIMEOUT_MS)
      .catch(() => false)
      .then((ok) => {
        debugLog(ok ? "otel-agent: flush complete" : "otel-agent: flush timed out");
        // Exit explicitly because installing signal handlers disables default behavior.
        process.exit(0);
      });
  };

  process.once("SIGTERM", () => handleSignal("SIGTERM"));
  process.once("SIGINT", () => handleSignal("SIGINT"));
}

/**
 * Simple function to generate a unique id for each request
 */
function generateRequestId() {
  return randomUUID();
}

export type RequestInfo = {
  id: string;
  method: string;
  path: string;
  body: string;
  query: string;
  start: number;
  experimentId: number;
  isWarmup?: boolean;
};

// In Python, we would maybe use https://docs.python.org/3/library/contextvars.html or https://github.com/tomwojcik/starlette-context
const requestContextStore = new AsyncLocalStorage<RequestInfo>();

/**
 * We aim to keep the measurement functions as simple as possible to avoid any performance overhead.
 */

/**
 * Function to start a new measurement
 * @param name - The span name
 * @param options - Optional span context following OTel semantic conventions
 */
export function startSpan(name: string, options?: SpanOptions): MeasurementStart {
  const requestInfo = requestContextStore.getStore();
  const elu = readEventLoopUtilization();
  const cpuStart = process.cpuUsage();
  const linuxCpuStart = DEBUG ? readLinuxCpuTotals() : null;

  // Check if there is an active span
  const activeSpanStack = requestInfo ? (inMemoryActiveSpan.get(requestInfo.id) as string[] | undefined) : undefined;
  // Top of stack = last element (LIFO)
  const parentSpanId = activeSpanStack && activeSpanStack.length > 0 ? activeSpanStack[activeSpanStack.length - 1] : null;
  const spanId = randomUUID();

  const measurementStart: MeasurementStart = {
    name,
    spanId,
    cpuStart,
    linuxCpuStart,
    requestId: requestInfo?.id || "unknown",
    start: performance.now(),
    timestamp: Date.now(),
    elu,
    parentSpanId,
    functionName: options?.functionName,
    operationType: options?.operationType,
    customAttributes: options?.attributes,
  };

  const key = `${requestInfo?.id || "unknown"}:${name}`;

  inMemoryCurrentMeasurements.set(key, measurementStart);
  // Store the active span in a map, in case a child span is started before the parent span is ended
  if (requestInfo) {
    const stack = activeSpanStack || [];
    stack.push(spanId);
    inMemoryActiveSpan.set(requestInfo.id, stack);
  }

  return measurementStart;
}

/**
 * Function to end a measurement
 */
export function endSpan(name: string): Measurement | undefined {
  const requestContext = getRequestContext();
  const key = `${requestContext.id}:${name}`;

  const measurementStart = inMemoryCurrentMeasurements.get(key);

  if (!measurementStart) {
    console.warn(`Measurement not started for ${key}`);
    return;
  }

  const { cpuStart, linuxCpuStart, start, timestamp, functionName, operationType, customAttributes } =
    measurementStart;

  const cpuEnd = process.cpuUsage(cpuStart);
  const end = performance.now();
  const diff = end - start;

  let stealDeltaJiffies: number | null = null;
  let stealRatio: number | null = null;
  let utilizationRatio: number | null = null;
  if (DEBUG && linuxCpuStart) {
    const linuxCpuEnd = readLinuxCpuTotals();
    const totalDeltaJiffies =
      linuxCpuStart && linuxCpuEnd
        ? Math.max(0, linuxCpuEnd.totalJiffies - linuxCpuStart.totalJiffies)
        : null;
    const idleAllDeltaJiffies =
      linuxCpuStart && linuxCpuEnd
        ? Math.max(0, linuxCpuEnd.idleAllJiffies - linuxCpuStart.idleAllJiffies)
        : null;
    stealDeltaJiffies =
      linuxCpuStart && linuxCpuEnd
        ? Math.max(0, linuxCpuEnd.stealJiffies - linuxCpuStart.stealJiffies)
        : null;
    stealRatio =
      typeof stealDeltaJiffies === "number" &&
      typeof totalDeltaJiffies === "number" &&
      totalDeltaJiffies > 0
        ? stealDeltaJiffies / totalDeltaJiffies
        : null;
    utilizationRatio =
      typeof idleAllDeltaJiffies === "number" &&
      typeof totalDeltaJiffies === "number" &&
      totalDeltaJiffies > 0
        ? Math.max(0, Math.min(1, 1 - idleAllDeltaJiffies / totalDeltaJiffies))
        : null;
  }

  inMemoryCurrentMeasurements.delete(key);
  // Drop the span from the stack (prefer LIFO, but tolerate out-of-order)
  const activeSpan = inMemoryActiveSpan.get(requestContext.id) as string[] | undefined;
  if (activeSpan) {
    if (activeSpan.length > 0 && activeSpan[activeSpan.length - 1] === measurementStart.spanId) {
      activeSpan.pop();
      inMemoryActiveSpan.set(requestContext.id, activeSpan);
    } else {
      const idx = activeSpan.lastIndexOf(measurementStart.spanId);
      if (idx !== -1) {
        activeSpan.splice(idx, 1);
        inMemoryActiveSpan.set(requestContext.id, activeSpan);
      }
    }
  }

  const measurement: Measurement = {
    timestamp: new Date(timestamp).toISOString(),
    context: {
      trace_id: requestContext.id,
      span_id: measurementStart.spanId,
    },
    parent_id: measurementStart.parentSpanId,
    attributes: {
      "service.name": "search-service",
      "service.version": VERSION,
      "span.name": name,
      "cpu.time.system": cpuEnd.system / 1000,
      "cpu.time.user": cpuEnd.user / 1000,
      "wall.time": diff,
      "http.route": requestContext.path,
      "http.method": requestContext.method,
      "http.query": requestContext.query,
      "http.body": requestContext.body,
      "process.pid": process.pid,
      "host.arch": process.arch,
      "os.type": process.platform,
      "experiment.id": requestContext.experimentId,
      "warmup": requestContext.isWarmup || false,
      ...(DEBUG && typeof stealDeltaJiffies === "number" && { "cpu.time.steal.jiffies": stealDeltaJiffies }),
      ...(DEBUG && typeof stealRatio === "number" && { "cpu.steal.pct": stealRatio }),
      ...(DEBUG && typeof utilizationRatio === "number" && { "cpu.utilization.pct": utilizationRatio }),
      // OTel semantic conventions
      ...(functionName && { "code.function": functionName }),
      ...(operationType && { "operation.type": operationType }),
      // Merge custom attributes
      ...customAttributes,
    },
  };

  inMemoryQueue.push(measurement);

  return measurement;
}

export function getRequestContext(): RequestInfo {
  return requestContextStore.getStore();
}

export function withTracing(
  request: Request,
  response: Response,
  next: NextFunction
) {
  const rootSpanId = randomUUID();
  const normalizedRoute = normalizeHttpRoute(request.path);
  const requestId = generateRequestId();
  const start: MeasurementStart = {
    name: `HTTP ${request.method} ${normalizedRoute}`,
    spanId: rootSpanId,
    cpuStart: process.cpuUsage(),
    linuxCpuStart: DEBUG ? readLinuxCpuTotals() : null,
    requestId,
    start: performance.now(),
    timestamp: Date.now(),
    elu: readEventLoopUtilization(),
    parentSpanId: null,
  };

  // Get X-Is-Warmup header
  const isWarmup = request.headers["x-is-warmup"] === "true";

  const requestInfo: RequestInfo = {
    id: requestId,
    start: Date.now(),
    body: CAPTURE_HTTP_BODY && request.body ? JSON.stringify(request.body) : "",
    method: request.method,
    path: normalizedRoute,
    query: CAPTURE_HTTP_QUERY ? JSON.stringify(request.query) : "",
    // experimentId: Number(request.headers["x-experiment-id"] || 1),
    experimentId: 5,
    isWarmup,
  };

  inMemoryActiveSpan.set(requestInfo.id, [rootSpanId]);

  response.once("finish", () => {
    const cpuEnd = process.cpuUsage(start.cpuStart);
    let stealDeltaJiffies: number | null = null;
    let stealRatio: number | null = null;
    let utilizationRatio: number | null = null;
    if (DEBUG && start.linuxCpuStart) {
      const linuxCpuEnd = readLinuxCpuTotals();
      const totalDeltaJiffies =
        start.linuxCpuStart && linuxCpuEnd
          ? Math.max(0, linuxCpuEnd.totalJiffies - start.linuxCpuStart.totalJiffies)
          : null;
      const idleAllDeltaJiffies =
        start.linuxCpuStart && linuxCpuEnd
          ? Math.max(0, linuxCpuEnd.idleAllJiffies - start.linuxCpuStart.idleAllJiffies)
          : null;
      stealDeltaJiffies =
        start.linuxCpuStart && linuxCpuEnd
          ? Math.max(0, linuxCpuEnd.stealJiffies - start.linuxCpuStart.stealJiffies)
          : null;
      stealRatio =
        typeof stealDeltaJiffies === "number" &&
        typeof totalDeltaJiffies === "number" &&
        totalDeltaJiffies > 0
          ? stealDeltaJiffies / totalDeltaJiffies
          : null;
      utilizationRatio =
        typeof idleAllDeltaJiffies === "number" &&
        typeof totalDeltaJiffies === "number" &&
        totalDeltaJiffies > 0
          ? Math.max(0, Math.min(1, 1 - idleAllDeltaJiffies / totalDeltaJiffies))
          : null;
    }

    const measurement: Measurement = {
      timestamp: new Date(start.timestamp).toISOString(),
      context: {
        trace_id: requestInfo.id,
        span_id: rootSpanId,
      },
      parent_id: null,
      attributes: {
        "service.name": "search-service",
        "service.version": VERSION,
        "span.name": `HTTP ${request.method} ${normalizedRoute}`,
        "http.route": normalizedRoute,
        "http.method": requestInfo.method,
        "http.query": requestInfo.query,
        "http.body": requestInfo.body,
        "process.pid": process.pid,
        "host.arch": process.arch,
        "os.type": process.platform,
        "cpu.time.system": cpuEnd.system / 1000,
        "cpu.time.user": cpuEnd.user / 1000,
        "wall.time": performance.now() - start.start,
        "experiment.id": requestInfo.experimentId,
        "warmup": isWarmup,
        ...(DEBUG && typeof stealDeltaJiffies === "number" && { "cpu.time.steal.jiffies": stealDeltaJiffies }),
        ...(DEBUG && typeof stealRatio === "number" && { "cpu.steal.pct": stealRatio }),
        ...(DEBUG && typeof utilizationRatio === "number" && { "cpu.utilization.pct": utilizationRatio }),
      },
    };

    inMemoryActiveSpan.delete(requestInfo.id);
    inMemoryQueue.push(measurement);
  });

  return requestContextStore.run(requestInfo, next);
}

// Add functions to the global scope
global.startSpan = startSpan;
global.endSpan = endSpan;
global.getRequestContext = getRequestContext;

// Add typing to the global scope
declare global {
  function startSpan(name: string, options?: SpanOptions): MeasurementStart;
  function endSpan(name: string): Measurement;
  function getRequestContext(): RequestInfo;
}
