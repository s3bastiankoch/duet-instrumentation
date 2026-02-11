import { search, Orama, insertMultiple, create } from "@orama/orama";
import { getMovieCount } from "./config";
import { createOneEmbedding } from "./embedding";
import { searchMoviesWithLevenshtein } from "./search/levensthein";

function dot(a: number[], b: number[]) {
  let sum = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) sum += a[i] * b[i];
  return sum;
}

function l2Norm(a: number[]) {
  let sumSq = 0;
  for (let i = 0; i < a.length; i++) sumSq += a[i] * a[i];
  return Math.sqrt(sumSq);
}

function cosineSimilarity(a: number[], b: number[]) {
  const denom = l2Norm(a) * l2Norm(b);
  if (denom === 0) return 0;
  return dot(a, b) / denom;
}

type MovieCandidate = {
  id: number;
  name: string;
  overview: string;
  embedding: number[];
  baseScore: number;
};

function mmrSelect(
  candidates: MovieCandidate[],
  queryEmbedding: number[],
  k: number,
  lambda: number
) {
  const selected: MovieCandidate[] = [];
  const remaining = candidates.slice();

  const clampedLambda = Math.max(0, Math.min(1, lambda));
  const take = Math.max(0, Math.min(k, remaining.length));

  while (selected.length < take && remaining.length > 0) {
    let bestIdx = 0;
    let bestScore = -Infinity;

    for (let i = 0; i < remaining.length; i++) {
      const c = remaining[i];
      const relevance = cosineSimilarity(queryEmbedding, c.embedding);

      let diversityPenalty = 0;
      if (selected.length > 0) {
        let maxSim = -Infinity;
        for (const s of selected) {
          const sim = cosineSimilarity(c.embedding, s.embedding);
          if (sim > maxSim) maxSim = sim;
        }
        diversityPenalty = maxSim;
      }

      // MMR: maximize relevance while penalizing redundancy.
      const score =
        clampedLambda * relevance - (1 - clampedLambda) * diversityPenalty;

      if (score > bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }

    selected.push(remaining[bestIdx]);
    remaining.splice(bestIdx, 1);
  }

  return selected;
}

export async function searchMovies(
  db: Orama<{
    name: "string";
    overview: "string";
    embedding: `vector[${number}]`;
  }>,
  query: string,
  queryEmbedding: number[]
) {
  console.log(`db holds ${db.data.docs.count} movies`);
  console.log("Searching for movies...");

  const vectorResult = await search(db, {
    mode: "hybrid",
    term: query,
    relevance: {
      // Term frequency saturation parameter.
      // Default value: 1.2
      // Recommended value: between 1.2 and 2
      k: 1.2,

      // Length normalization parameter.
      // Default value: 0.75
      // Recommended value: > 0.75
      b: 0.75,

      // Frequency normalization lower bound.
      // Default value: 0.5
      // Recommended value: between 0.5 and 1
      d: 0.5,
    },
    boost: {
      name: 2,
    },
    vector: {
      value: queryEmbedding,
      property: "embedding",
    },
    limit: 1000,
    includeVectors: true,
    similarity: 0,
    threshold: 0,
    tolerance: 10000,
  });

  console.log(`Found ${vectorResult.hits.length} hits`);

  const candidates = vectorResult.hits.map((hit, index) => {
    const rawId = (hit.document as { id?: string | number } | undefined)?.id;
    const parsedId =
      typeof rawId === "string" || typeof rawId === "number"
        ? Number(rawId)
        : Number.NaN;
    const id = Number.isFinite(parsedId) ? parsedId : index;
    return {
      id,
      title: String(hit.document?.name ?? ""),
      description: String(hit.document?.overview ?? ""),
    };
  });

  const ordered = searchMoviesWithLevenshtein(candidates, query);
  const hitById = new Map<number, (typeof vectorResult.hits)[number]>();
  candidates.forEach((candidate, index) => {
    hitById.set(candidate.id, vectorResult.hits[index]);
  });

  return ordered
    .map((movie) => hitById.get(movie.id))
    .filter((hit): hit is (typeof vectorResult.hits)[number] => Boolean(hit));
}

  const db = create({
    schema: {
      name: "string",
      overview: "string",
      embedding: `vector[384]`,
    },
  });

export async function getSearchResults(
  allMovies: any[],
  query: string,
  cachedEmbeddings: Record<string, number[]>
) {
  const queryEmbedding = await createOneEmbedding(query);

  if (allMovies.length === 0) {
    return [];
  }

  const moreMovies = Array.from({ length: getMovieCount() }, (_, i) => ({
    name: allMovies[i % allMovies.length].name,
    overview: allMovies[i % allMovies.length].overview,
    id: i,
  }));

  const movies = moreMovies.map((movie) => ({
    id: movie.id,
    name: movie.name,
    overview: movie.overview,
    embedding: cachedEmbeddings[movie.name],
  }));

  if (db.data.docs.count === 0) {
    await insertMultiple(
      db,
      movies.map((movie) => ({
        name: movie.name,
        embedding: movie.embedding,
        overview: movie.overview,
        id: String(movie.id),
      }))
    );
  }

  const searchedMovies = await searchMovies(db, query, queryEmbedding);

  return searchedMovies.map((s) => {
    const movie = movies.find((m) => m.name === s.document.name);
    return {
      id: movie?.id,
      name: movie?.name,
      overview: movie?.overview,
    };
  });
}

export async function getDiversifiedSearchResults(
  allMovies: any[],
  query: string,
  cachedEmbeddings: Record<string, number[]>,
  opts?: {
    count?: number;
    candidates?: number;
    lambda?: number;
  }
) {
  const queryEmbedding = await createOneEmbedding(query);

  if (allMovies.length === 0) {
    return [];
  }

  const moreMovies = Array.from({ length: getMovieCount() }, (_, i) => ({
    name: allMovies[i % allMovies.length].name,
    overview: allMovies[i % allMovies.length].overview,
    id: i,
  }));

  const movies = moreMovies.map((movie) => ({
    id: movie.id,
    name: movie.name,
    overview: movie.overview,
    embedding: cachedEmbeddings[movie.name],
  }));

  if (db.data.docs.count === 0) {
    await insertMultiple(
      db,
      movies.map((movie) => ({
        name: movie.name,
        embedding: movie.embedding,
        overview: movie.overview,
        id: String(movie.id),
      }))
    );
  }

  const searchedMovies = await searchMovies(db, query, queryEmbedding);

  const byName = new Map<string, (typeof movies)[number]>();
  for (const m of movies) byName.set(m.name, m);

  const candidateLimit = Math.max(1, Math.min(opts?.candidates ?? 50, 500));
  const count = Math.max(1, Math.min(opts?.count ?? 10, 100));
  const lambda = opts?.lambda ?? 0.7;

  const candidates: MovieCandidate[] = searchedMovies
    .slice(0, candidateLimit)
    .map((s) => {
      const movie = byName.get(s.document.name);
      if (!movie?.embedding) return null;
      return {
        id: movie.id,
        name: movie.name,
        overview: movie.overview,
        embedding: movie.embedding,
        baseScore: s.score,
      };
    })
    .filter((c): c is MovieCandidate => c !== null);

  const selected = mmrSelect(candidates, queryEmbedding, count, lambda);

  return selected.map((m) => ({
    id: m.id,
    name: m.name,
    overview: m.overview,
  }));
}
