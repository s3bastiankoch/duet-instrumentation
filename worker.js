import { parentPort, workerData } from "worker_threads";
import fs from "fs";
import path from "path";

/**
 * Get the SQLite Database class for the current runtime.
 * Tries Bun's bun:sqlite first, falls back to Node.js node:sqlite.
 * - Bun: uses `bun:sqlite` with `Database` class
 * - Node.js: uses `node:sqlite` with `DatabaseSync` class (requires Node.js 22.5.0+)
 */
async function getSqliteDatabase() {
  // Try bun:sqlite first (works in Bun runtime)
  try {
    const bunSqlite = await import("bun:sqlite");
    if (workerData?.debug) console.log("Worker started (runtime: Bun)");
    return bunSqlite.Database;
  } catch {
    // Fall back to node:sqlite (Node.js 22.5.0+)
    try {
      const nodeSqlite = await import("node:sqlite");
      if (workerData?.debug) console.log("Worker started (runtime: Node.js)");
      return nodeSqlite.DatabaseSync;
    } catch (e) {
      throw new Error(
        "SQLite not available. Requires either Bun (bun:sqlite) or Node.js 22.5.0+ (node:sqlite). " +
        `Original error: ${e.message}`
      );
    }
  }
}

/**
 * Write measurements to a SQLite database.
 * Automatically uses the correct SQLite module based on runtime (Bun or Node.js).
 */
async function initSqlite() {
  const rawOutDir =
    workerData.outputDir || process.env.OTEL_AGENT_SPANS_DIR || ".otel-agent/spans";
  // Resolve to absolute path to ensure it works in worker thread context
  const outDir = path.resolve(rawOutDir);
  fs.mkdirSync(outDir, { recursive: true });

  const serviceVersion = workerData.serviceVersion || "unknown";
  // Make sure the service version is a valid filename
  const sanitizedServiceVersion = serviceVersion.replace(/[^a-zA-Z0-9]/g, "_");
  // Include PID so multi-process (e.g. cluster) doesn't contend on one DB file.
  // Worker threads share the same PID as their parent process, so this is stable per process.
  const dbPath = path.join(outDir, `spans-${sanitizedServiceVersion}-${process.pid}.db`);
  
  if (workerData?.debug) console.log(`Writing spans to SQLite database at: ${dbPath}`);

  const Database = await getSqliteDatabase();
  const db = new Database(dbPath);

  const safeExec = (sql) => {
    try {
      db.exec(sql);
      return true;
    } catch (e) {
      console.warn(`Warning: SQLite exec failed: ${e?.message || e}`);
      return false;
    }
  };

  // Configure SQLite and set a busy timeout to handle concurrent writes.
  // We explicitly disable WAL so each DB is self-contained in a single .db file.
  // These PRAGMAs can fail on some runtimes; they should never prevent table creation.
  safeExec("PRAGMA busy_timeout = 10000;"); // 10 seconds
  safeExec("PRAGMA journal_mode = DELETE;");

  // Create the spans table if it doesn't exist.
  safeExec(`
    CREATE TABLE IF NOT EXISTS spans (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_start_ms INTEGER NOT NULL,
      duration_ms REAL NOT NULL,
      span_name TEXT NOT NULL,
      trace_id TEXT,
      span_id TEXT,
      parent_span_id TEXT,
      status INTEGER DEFAULT 0,
      service_version TEXT,
      http_method TEXT,
      http_route TEXT,
      warmup INTEGER,
      experiment_id INTEGER,
      cpu_time_user_ms REAL,
      cpu_time_system_ms REAL,
      cpu_steal_jiffies REAL,
      cpu_steal_pct REAL,
      cpu_utilization_pct REAL,
      inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
  `);

  // Create indexes for common query patterns.
  safeExec(`
    CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
    CREATE INDEX IF NOT EXISTS idx_spans_span_name ON spans(span_name);
    CREATE INDEX IF NOT EXISTS idx_spans_http_route ON spans(http_route);
    CREATE INDEX IF NOT EXISTS idx_spans_ts_start ON spans(ts_start_ms);
  `);

  const insert = db.prepare(`
    INSERT INTO spans (
      ts_start_ms, duration_ms, span_name, trace_id, span_id,
      parent_span_id, status, service_version, http_method,
      http_route, warmup, experiment_id, cpu_time_user_ms, cpu_time_system_ms,
      cpu_steal_jiffies, cpu_steal_pct, cpu_utilization_pct
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  return { db, insert, dbPath, serviceVersion };
}

function makeBatchWriter({ db, insert, serviceVersion }) {
  return function writeMeasurementsToSqlite(measurements) {
    let insertedCount = 0;

    // Use a transaction for better performance and to reduce lock contention duration
    const performInsertions = () => {
      for (const m of measurements || []) {
        const attrs = (m && m.attributes) || {};
        const tsStartMs = Date.parse(m.timestamp);
        const durationMs =
          typeof attrs["wall.time"] === "number" ? attrs["wall.time"] : null;
        const cpuUserMs =
          typeof attrs["cpu.time.user"] === "number" ? attrs["cpu.time.user"] : null;
        const cpuSystemMs =
          typeof attrs["cpu.time.system"] === "number" ? attrs["cpu.time.system"] : null;
        const cpuStealJiffies =
          typeof attrs["cpu.time.steal.jiffies"] === "number" ? attrs["cpu.time.steal.jiffies"] : null;
        const cpuStealPct =
          typeof attrs["cpu.steal.pct"] === "number" ? attrs["cpu.steal.pct"] : null;
        const cpuUtilizationPct =
          typeof attrs["cpu.utilization.pct"] === "number" ? attrs["cpu.utilization.pct"] : null;

        if (!Number.isFinite(tsStartMs) || durationMs == null) continue;

        insert.run(
          tsStartMs,
          durationMs,
          attrs["span.name"] || m.context?.span_id || "unknown",
          m.context?.trace_id || null,
          m.context?.span_id || null,
          m.parent_id || null,
          0,
          attrs["service.version"] || serviceVersion,
          attrs["http.method"] || null,
          attrs["http.route"] || null,
          attrs["warmup"] ? 1 : 0,
          typeof attrs["experiment.id"] === "number" ? attrs["experiment.id"] : null,
          cpuUserMs,
          cpuSystemMs,
          cpuStealJiffies,
          cpuStealPct,
          cpuUtilizationPct
        );
        insertedCount++;
      }
    };

    try {
      if (typeof db.transaction === "function") {
        // Bun's bun:sqlite has a built-in transaction helper
        db.transaction(performInsertions)();
      } else {
        // Fallback for node:sqlite or other implementations
        db.exec("BEGIN TRANSACTION;");
        try {
          performInsertions();
          db.exec("COMMIT;");
        } catch (e) {
          db.exec("ROLLBACK;");
          throw e;
        }
      }
    } catch (e) {
      console.error(`Error inserting spans into SQLite: ${e.message}`);
      // Do not re-throw to avoid crashing the worker and potentially the parent process
    }

    return insertedCount;
  };
}

if (!parentPort) {
  throw new Error("worker.js must be run as a worker thread");
}

const sqliteCtx = await initSqlite();
const writeBatch = makeBatchWriter(sqliteCtx);

parentPort.on("message", (msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type !== "flush") return;

  const measurements = msg.measurements;
  const n = writeBatch(measurements);
  parentPort.postMessage({ ok: true, inserted: n, dbPath: sqliteCtx.dbPath });
});

