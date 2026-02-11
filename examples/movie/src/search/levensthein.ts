interface Movie {
  id: number;
  title: string;
  description?: string;
}

// Levenshtein distance implementation using the Wagner–Fischer algorithm
function levenshtein(a: string, b: string): number {
  const aLen = a.length;
  const bLen = b.length;

  // If one string is empty, return the length of the other
  if (aLen === 0) return bLen;
  if (bLen === 0) return aLen;

  // Create a matrix with dimensions (aLen + 1) x (bLen + 1)
  const matrix: number[][] = Array.from({ length: aLen + 1 }, (_, i) => {
    const row = new Array(bLen + 1);
    row[0] = i;
    return row;
  });

  // Initialize the first row of the matrix
  for (let j = 0; j <= bLen; j++) {
    matrix[0][j] = j;
  }

  // Compute the rest of the matrix
  for (let i = 1; i <= aLen; i++) {
    for (let j = 1; j <= bLen; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      matrix[i][j] = Math.min(
        matrix[i - 1][j] + 1, // deletion
        matrix[i][j - 1] + 1, // insertion
        matrix[i - 1][j - 1] + cost // substitution
      );
    }
  }

  return matrix[aLen][bLen];
}

// Function to search movies based on a fuzzy query using Levenshtein distance
export function searchMoviesWithLevenshtein(movies: Movie[], query: string): Movie[] {
  const queryLower = query.toLowerCase();
  const descriptionLimit = 200;

  // Map each movie to an object containing the movie and its distance score
  const results = movies.map((movie) => {
    const titleLower = movie.title.toLowerCase();
    const titleDistance = levenshtein(titleLower, queryLower);

    let descriptionDistance = Number.POSITIVE_INFINITY;
    if (movie.description) {
      const descriptionLower = movie.description.toLowerCase();
      const descriptionSlice = descriptionLower.slice(0, descriptionLimit);
      descriptionDistance = levenshtein(descriptionSlice, queryLower);
    }

    return {
      movie,
      distance: Math.min(titleDistance, descriptionDistance),
    };
  });

  // Sort movies by their computed distance (lower distance means more similar)
  results.sort((a, b) => a.distance - b.distance);

  console.log(`Found ${results.length} results`);

  // Return the sorted movies
  return results.map((item) => item.movie);
}
