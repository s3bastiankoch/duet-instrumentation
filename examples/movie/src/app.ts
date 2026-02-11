import express from "express";
import cluster from "cluster";
import { cpus } from "os";
import { z } from "zod";
import { MovieModel, PreferenceModel } from "./model";
import { getDiversifiedSearchResults, getSearchResults } from "./search";
import jwt from "jsonwebtoken";
import { rateLimiterMiddleware } from "./rate-limits";
import {
  buildMovieGraph,
  randomWalkWithRestart,
} from "./filtering/random-walk";
import { alsRecommender } from "./filtering/als";
import { seedDB, cachedEmbeddings } from "./seed";

const PORT: number = parseInt(process.env.PORT || "4000");

// =============================================================================
// Crash detection and logging
// =============================================================================
function getMemoryUsage(): string {
  const used = process.memoryUsage();
  return [
    `rss=${(used.rss / 1024 / 1024).toFixed(1)}MB`,
    `heapUsed=${(used.heapUsed / 1024 / 1024).toFixed(1)}MB`,
    `heapTotal=${(used.heapTotal / 1024 / 1024).toFixed(1)}MB`,
    `external=${(used.external / 1024 / 1024).toFixed(1)}MB`,
  ].join(", ");
}

function logCrash(type: string, error: Error | string, extra?: string): void {
  const pid = process.pid;
  const role = cluster.isPrimary ? "primary" : "worker";
  const timestamp = new Date().toISOString();
  
  console.error("");
  console.error("═══════════════════════════════════════════════════════════════════");
  console.error(`CRASH DETECTED: ${type}`);
  console.error("═══════════════════════════════════════════════════════════════════");
  console.error(`Timestamp: ${timestamp}`);
  console.error(`Process:   ${role} (PID ${pid})`);
  console.error(`Memory:    ${getMemoryUsage()}`);
  if (extra) {
    console.error(`Info:      ${extra}`);
  }
  console.error("───────────────────────────────────────────────────────────────────");
  if (error instanceof Error) {
    console.error(`Error:     ${error.message}`);
    console.error(`Stack:\n${error.stack}`);
  } else {
    console.error(`Error:     ${error}`);
  }
  console.error("═══════════════════════════════════════════════════════════════════");
  console.error("");
}

// Uncaught exceptions
process.on("uncaughtException", (error: Error) => {
  logCrash("Uncaught Exception", error);
  // Give time for logs to flush, then exit
  setTimeout(() => process.exit(1), 100);
});

// Unhandled promise rejections
process.on("unhandledRejection", (reason: unknown) => {
  const error = reason instanceof Error ? reason : new Error(String(reason));
  logCrash("Unhandled Promise Rejection", error);
  // Give time for logs to flush, then exit
  setTimeout(() => process.exit(1), 100);
});

// SIGTERM (Docker stop, Kubernetes termination)
process.on("SIGTERM", () => {
  console.log(`[PID ${process.pid}] Received SIGTERM, shutting down gracefully...`);
  process.exit(0);
});

// SIGINT (Ctrl+C)
process.on("SIGINT", () => {
  console.log(`[PID ${process.pid}] Received SIGINT, shutting down...`);
  process.exit(0);
});

// Memory warning - log when heap usage exceeds threshold
const MEMORY_WARNING_THRESHOLD_MB = 1500; // Warn at 1.5GB (container limit is 2GB)
let lastMemoryWarning = 0;

function checkMemoryUsage(): void {
  const used = process.memoryUsage();
  const heapUsedMB = used.heapUsed / 1024 / 1024;
  const now = Date.now();
  
  // Only warn every 10 seconds to avoid log spam
  if (heapUsedMB > MEMORY_WARNING_THRESHOLD_MB && now - lastMemoryWarning > 10000) {
    lastMemoryWarning = now;
    console.warn(`[PID ${process.pid}] HIGH MEMORY WARNING: ${getMemoryUsage()}`);
  }
}

// Check memory every 5 seconds
setInterval(checkMemoryUsage, 5000);

const numCPUs = cpus().length;
const MAX_WORKERS = parseInt(process.env.MAX_WORKERS || "5"); // TODO: Check max workers
const workersCount = Math.min(numCPUs, MAX_WORKERS);

function hash32(x: number) {
  // A small, fast, deterministic 32-bit mixer (not cryptographic).
  x |= 0;
  x = Math.imul(x ^ (x >>> 16), 0x7feb352d);
  x = Math.imul(x ^ (x >>> 15), 0x846ca68b);
  return (x ^ (x >>> 16)) >>> 0;
}

function decayWeight(bucket: number, halfLife: number) {
  // halfLife in "buckets"
  return Math.exp((-Math.LN2 * bucket) / Math.max(1e-9, halfLife));
}

function computeTrendingScore(
  movie: { id: number; popularity: number; vote_average: number; vote_count: number },
  window: number,
  repeat: number
) {
  // Base score uses existing dataset fields.
  let score = movie.popularity + movie.vote_average * 10 + Math.log1p(movie.vote_count);

  const halfLife = Math.max(1, window / 3);
  const w = Math.max(1, window);
  const r = Math.max(1, repeat);

  // Synthetic "events": deterministic per movie id and time bucket.
  // Complexity: O(window * repeat) per movie; stable and tunable.
  for (let rep = 0; rep < r; rep++) {
    for (let b = 0; b < w; b++) {
      const h = hash32(movie.id ^ (b * 2654435761) ^ (rep * 2246822519));
      const events = h % 4; // 0..3
      score += events * decayWeight(b, halfLife);
    }
  }

  return score;
}

if (cluster.isPrimary) {
  await seedDB();
  console.log(`Master process ${process.pid} is running`);

  for (let i = 0; i < workersCount; i++) {
    cluster.fork();
  }

  cluster.on("exit", (worker, code, signal) => {
    const exitReason = signal
      ? `killed by signal ${signal}`
      : code !== 0
      ? `exited with code ${code}`
      : "exited normally";
    
    // Highlight OOM kills (signal SIGKILL with no explicit code often means OOM)
    const isLikelyOOM = signal === "SIGKILL";
    
    console.error("");
    console.error("───────────────────────────────────────────────────────────────────");
    console.error(`Worker ${worker.process.pid} ${exitReason}`);
    if (isLikelyOOM) {
      console.error("⚠️  SIGKILL often indicates an Out-Of-Memory (OOM) kill!");
      console.error(`   Current primary memory: ${getMemoryUsage()}`);
    }
    console.error("───────────────────────────────────────────────────────────────────");
    console.error("");
    
    console.log(`Restarting worker...`);
    cluster.fork();
  });
} else {
  const app = express();

  app.use(express.json());

  // JWT middleware for authenticated access
  const jwtSecret = process.env.JWT_SECRET || "my-very-secret-key";

  app.use((req, res, next) => {
    if (process.env.IS_BENCHMARK?.toLowerCase() !== "true") {
      return next();
    }

    const authHeader = req.headers.authorization;

    const splitted = authHeader?.split(" ") || [];

    if (splitted.length !== 2) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }

    const token = splitted[1];

    // Introduce unnecesary CPU work
    for (let i = 0; i < 1000000; i++) {
      Math.sin(i);
    }

    try {
      const decoded = jwt.verify(token, jwtSecret) as {
        userId: string;
      };

      if (!decoded.userId) {
        res.status(401).json({ error: "Unauthorized" });
        return;
      }

      res.locals.userId = decoded.userId;
    } catch (e) {
      console.error(e);
      res.status(401).json({ error: "Unauthorized" });
      return;
    }

    next();
  });

  // Real-world noisy middleware #1: rate limiting (backed by Mongo).
  app.use((req, res, next) => {
    if (process.env.IS_BENCHMARK?.toLowerCase() !== "true") {
      return next();
    }
    return rateLimiterMiddleware(req, res, next);
  });

  // Loads the user's preferences once per request and stores it in res.locals.
  const userContextCacheTtlMs = Number.parseInt(
    process.env.USER_CONTEXT_CACHE_TTL_MS || "0",
    10
  );
  const preferenceCache = new Map<
    string,
    { expiresAt: number; value: Awaited<ReturnType<typeof PreferenceModel.findOne>> }
  >();

  app.use(async (req, res, next) => {
    if (process.env.IS_BENCHMARK?.toLowerCase() !== "true") {
      return next();
    }

    const userId = res.locals.userId as string | undefined;
    if (!userId) return next();

    const now = Date.now();
    if (userContextCacheTtlMs > 0) {
      const cached = preferenceCache.get(userId);
      if (cached && cached.expiresAt > now) {
        res.locals.preference = cached.value;
        return next();
      }
    }

    try {
      const preference = await PreferenceModel.findOne({ userId });
      res.locals.preference = preference;
      if (userContextCacheTtlMs > 0) {
        preferenceCache.set(userId, {
          expiresAt: now + userContextCacheTtlMs,
          value: preference,
        });
      }
      return next();
    } catch (e) {
      // In production you might fail open/closed depending on requirements.
      console.error(e);
      return next();
    }
  });

  const searchSchema = z.object({
    query: z.string(),
    // Restrict the movie results based on the country code
    countryCode: z.string().length(2).optional(),
  });

  app.get("/", (req, res) => {
    res.send("MovieDB API");
  });

  app.get("/health", (req, res) => {
    res.send("OK");
  });

  app.get("/movies", async (req, res) => {
    const countryCode = req.query.countryCode as string;

    // Get all movies from the database
    const allMovies = await MovieModel.find({});

    console.log(`Found ${allMovies.length} movies in the database.`);

    res.send(JSON.stringify(allMovies));
  });

  app.get("/search", async (req, res) => {
    console.log("Searching for movies...");
    const { query, countryCode } = searchSchema.parse(req.query);

    // Auth check for user
    const userId = res.locals.userId;

    const allMovies = await MovieModel.find({});

    res.json(await getSearchResults(allMovies, query, cachedEmbeddings));
  });

  const diverseSearchSchema = z.object({
    query: z.string(),
    countryCode: z.string().length(2).optional(),
    count: z.coerce.number().int().min(1).max(100).default(10),
    candidates: z.coerce.number().int().min(1).max(500).default(50),
    lambda: z.coerce.number().min(0).max(1).default(0.7),
  });

  // Search with MMR diversification on embeddings.
  app.get("/search/diverse", async (req, res) => {
    console.log("Searching for movies (diversified)...");
    const { query, count, candidates, lambda } = diverseSearchSchema.parse(
      req.query
    );

    const allMovies = await MovieModel.find({});

    res.json(
      await getDiversifiedSearchResults(allMovies, query, cachedEmbeddings, {
        count,
        candidates,
        lambda,
      })
    );
  });

  const userRecommendationSchema = z.object({
    userId: z.string(),
    // We will receive a string that we will parse into an integer
    count: z.number({ coerce: true }),
  });

  // Ranking of the top movies based on collaborative filtering within a region
  app.get("/top/user/:userId/:count", async (req, res) => {
    const parsedParams = userRecommendationSchema.safeParse(req.params);

    if (!parsedParams.success) {
      res.status(400).json(parsedParams.error);
      return;
    }

    const { userId, count } = parsedParams.data;

    const allMovies = await MovieModel.find();
    const preferences = await PreferenceModel.find();
    const allUsers = preferences.map((p) => p.userId).sort();

    const k = 5;
    // Construct the user-movie matrix with ascending user and movie IDs.
    // const M = [
    //   [1, -1, 1, 0, 0, 0], // User 1
    //   [1, -1, 1, 0, 1, 0], // User 2
    //   [0, -1, 0, 1, 0, 1], // User 3
    //   [0, 0, 1, 0, 0, 0]
    // ];

    const preferenceByUser = preferences.reduce<Record<string, typeof preferences>>(
      (acc, p) => {
        (acc[p.userId] ??= []).push(p);
        return acc;
      },
      {}
    );

    const allMovieIDs = allMovies.map((m) => m.id).sort((a, b) => a - b);

    const M = allUsers.map((user) => {
      const userPreferences = preferenceByUser[user]?.[0];

      // Sort preferences and fill missing preferences with 0 in correct order
      return allMovieIDs.map((movieId) =>
        userPreferences?.likedMovies.includes(String(movieId))
          ? 1
          : userPreferences?.dislikedMovies.includes(String(movieId))
          ? -1
          : 0
      );
    });

    if (M.length === 0) {
      res.status(503).json({ error: "No user preferences available yet" });
      return;
    }

    const recommendation = alsRecommender(M, k, 10);

    // TODO: Only return the top k movies for a user
    const userPredictions =
      recommendation.predictions[allUsers.indexOf(userId)];
    // Get top k movies for a user excluding the ones they have already liked
    const topMovies = userPredictions
      .map((score, index) => ({ movieId: allMovieIDs[index], score }))
      .sort((a, b) => b.score - a.score)
      .filter(
        (movie) =>
          !preferenceByUser[userId]?.[0].likedMovies.includes(
            String(movie.movieId)
          )
      )
      .slice(0, count);

    res.json({ topMovies });
  });

  const rankingParamsSchema = z.object({
    region: z.enum(["germany", "us"]).default("us"),
    count: z.number({ coerce: true }),
  });

  // Recommendation based on random walk with restart
  app.get("/top/region/:region/:count", async (req, res) => {
    const parsedParams = rankingParamsSchema.safeParse(req.params);

    if (!parsedParams.success) {
      res.status(400).json("Bad request");
      return;
    }

    const userId = res.locals.userId;

    const { region, count } = parsedParams.data;

    // Construct movie graph from database by user preferences
    const preferences = await PreferenceModel.find();

    const allMovies = await MovieModel.find({
      region,
    });

    const movieGraph = buildMovieGraph(preferences);

    // Choose a seed movie (or movies) to start the random walk from.
    const seedMovies = allMovies.map((movie) => String(movie.id));

    // Set the restart probability (e.g., 15%).
    const restartProbability = 0.15;

    const maxIterations = 200;

    // Essentially PageRank
    // Run the random walk with restarts.
    const recommendationScores = randomWalkWithRestart(
      movieGraph,
      seedMovies,
      restartProbability,
      maxIterations
    );

    // Sort the movies by their recommendation scores.
    const topMovies = Object.entries(recommendationScores)
      .sort((a, b) => b[1] - a[1])
      .map(([movieId, score]) => ({
        movieId,
        score,
      }))
      .slice(0, count);

    // Fill topMovies up to the count with movies that are not in the topMovies list
    if (allMovies.length > 0) {
      while (topMovies.length < count && topMovies.length < allMovies.length) {
        const randomMovie =
          allMovies[Math.floor(Math.random() * allMovies.length)];
        if (!topMovies.some((m) => m.movieId === String(randomMovie.id))) {
          topMovies.push({ movieId: String(randomMovie.id), score: 0 });
        }
      }
    }

    res.json({ topMovies });
  });

  const trendingParamsSchema = z.object({
    region: z
      .string()
      .transform((s) => s.toUpperCase())
      .refine((s) => ["US", "DE", "FR", "ES"].includes(s), "Invalid region"),
    count: z.coerce.number().int().min(1).max(500),
  });

  const trendingQuerySchema = z.object({
    window: z.coerce.number().int().min(1).max(365).default(30),
    repeat: z.coerce.number().int().min(1).max(100).default(1),
  });

  // Trending movies by region with time-decayed synthetic "events".
  app.get("/trending/:region/:count", async (req, res) => {
    const parsedParams = trendingParamsSchema.safeParse(req.params);
    if (!parsedParams.success) {
      res.status(400).json("Bad request");
      return;
    }

    const parsedQuery = trendingQuerySchema.safeParse(req.query);
    if (!parsedQuery.success) {
      res.status(400).json("Bad request");
      return;
    }

    const { region, count } = parsedParams.data;
    const { window, repeat } = parsedQuery.data;

    const movies = await MovieModel.find({ region });

    const topMovies = movies
      .map((m) => ({
        movieId: String(m.id),
        score: computeTrendingScore(m, window, repeat),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, count);

    res.json({ topMovies, window, repeat, region });
  });

  // Toggle like for a movie and a random user
  app.post("/movies/like/:movieId", async (req, res) => {
    const { movieId } = req.params;

    const preferences = await PreferenceModel.find();
    const preference =
      preferences[Math.floor(Math.random() * preferences.length)];

    if (!preference) {
      res.status(404).json({ error: "User not found" });
      return;
    }

    if (preference.likedMovies.includes(movieId)) {
      await preference.updateOne({
        likedMovies: preference.likedMovies.filter((m) => m !== movieId),
      });
    } else {
      await preference.updateOne({
        likedMovies: [...preference.likedMovies, movieId],
      });
    }

    res.json({ message: "Movie like toggled" });
  });

  // Global error handler - catches errors from route handlers
  app.use((err: Error, req: express.Request, res: express.Response, next: express.NextFunction) => {
    console.error("");
    console.error("───────────────────────────────────────────────────────────────────");
    console.error(`Request error: ${req.method} ${req.path}`);
    console.error(`Error: ${err.message}`);
    console.error(`Stack: ${err.stack}`);
    console.error(`Memory: ${getMemoryUsage()}`);
    console.error("───────────────────────────────────────────────────────────────────");
    console.error("");
    
    if (!res.headersSent) {
      res.status(500).json({ error: "Internal server error" });
    }
  });

  app.listen(PORT, () => {
    console.log(`Listening for requests on http://localhost:${PORT}`);
  });
}
