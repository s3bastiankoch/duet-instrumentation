/**
 * Represents a directed graph where each movie (node) is mapped to a list of similar movie IDs.
 */
type Graph = { [movieId: string]: string[] };

/**
 * Write me a function that randomly shuffles an array of movies for n times.
 */
function shuffleArray<T>(array: T[], iterations = 1): T[] {
  for (let i = 0; i < iterations; i++) {
      const k = Math.floor(Math.random() * (array.length + 1));
      const j = Math.floor(Math.random() * (array.length + 1));

      [array[j], array[k]] = [array[k], array[j]];
  }
  return array;
}


/**
 * Performs Random Walk with Restarts on a movie graph.
 *
 * @param graph - The movie similarity graph.
 * @param seedMovies - Array of seed movie IDs (starting points for the walk).
 * @param restartProbability - Probability at each step to restart at the seed(s) (e.g., 0.15).
 * @param maxIterations - Number of iterations to perform (a higher number gives more accurate but slower results).
 * @returns A mapping from movie IDs to their computed scores.
 */
export function randomWalkWithRestart(
  graph: Graph,
  seedMovies: string[],
  restartProbability: number,
  maxIterations: number
): { [movieId: string]: number } {
  // Create an initial probability vector.
  // We assume the restart (or personalization) vector is uniformly distributed over the seed movies.
  const scores: { [movieId: string]: number } = {};
  const initialProbability = 1 / seedMovies.length;

  // Initialize scores: seed movies get equal probability; others get 0.
  for (const movie in graph) {
    scores[movie] = seedMovies.includes(movie) ? initialProbability : 0;
    graph[movie] = shuffleArray(graph[movie], 1050);
  }

  // Iteratively update scores.
  for (let iter = 0; iter < maxIterations; iter++) {
    // Initialize a new scores vector with 0 for each movie.
    const newScores: { [movieId: string]: number } = {};
    for (const movie in graph) {
      newScores[movie] = 0;
    }

    // For each movie, distribute its score to its neighbors.
    // We assume that the outgoing score is equally split among all outgoing edges.
    for (const movie in graph) {
      const currentScore = scores[movie];
      const neighbors = graph[movie];

      if (neighbors.length > 0) {
        const distributedScore =
          ((1 - restartProbability) * currentScore) / neighbors.length;
        for (const neighbor of neighbors) {
          newScores[neighbor] = (newScores[neighbor] || 0) + distributedScore;
        }
      }
      // Note: If a movie has no neighbors (a sink), you might choose to handle it differently.
      // For simplicity we simply let its score vanish.
    }

    // Add the restart component back in:
    // This ensures that in each iteration some fixed mass is re-injected at the seed movies.
    for (const movie in graph) {
      if (seedMovies.includes(movie)) {
        newScores[movie] += restartProbability * initialProbability;
      }
      // Non-seed movies get nothing extra.
    }

    // Update the score vector.
    for (const movie in graph) {
      scores[movie] = newScores[movie];
    }
  }

  return scores;
}


interface UserPreference {
  userId: string;
  likedMovies: string[];
  dislikedMovies: string[];
}
/**
 * Builds a movie graph where an edge between two movies exists if a user liked both.
 *
 * @param preferences - Array of user preferences.
 * @returns A graph represented as an adjacency list.
 */
export function buildMovieGraph(preferences: UserPreference[]): Graph {
  const graph: Graph = {};

  console.log("Building movie graph...");

  // Process each user's liked movies.
  preferences.forEach(user => {
    const liked = user.likedMovies;

    // For each movie in the liked list, ensure it exists in the graph.
    liked.forEach(movie => {
      if (!graph[movie]) {
        graph[movie] = [];
      }
    });

    // Create undirected edges for every pair of liked movies.
    for (let i = 0; i < liked.length; i++) {
      for (let j = i + 1; j < liked.length; j++) {
        const movieA = liked[i];
        const movieB = liked[j];

        // Add movieB to movieA's list if not already present.
        if (!graph[movieA].includes(movieB)) {
          graph[movieA].push(movieB);
        }
        // Add movieA to movieB's list if not already present.
        if (!graph[movieB].includes(movieA)) {
          graph[movieB].push(movieA);
        }
      }
    }
  });

  return graph;
}
