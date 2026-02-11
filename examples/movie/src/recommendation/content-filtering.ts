import { createMultipleEmbeddings, createOneEmbedding } from "../embedding";

function cosineSimilarity(a: number[], b: number[]) {
  const dotProduct = a.reduce((sum, value, index) => sum + value * b[index], 0);
  const magnitudeA = Math.sqrt(a.reduce((sum, value) => sum + value ** 2, 0));
  const magnitudeB = Math.sqrt(b.reduce((sum, value) => sum + value ** 2, 0));
  return dotProduct / (magnitudeA * magnitudeB);
}

type UserPreferenceData = {
  watchHistory: {
    movieId: string;
    watchedAt: Date;
    // The percentage of the movie watched
    progress: number;

    rating?: "like" | "dislike";
  }[];
  myList: {
    movieId: string;
    addedAt: Date;
  }[];
};

export async function contentFilter(
  allMovies: { id: string; name: string }[],
  preferences: UserPreferenceData,
  metadata: { countryCode: string }
) {
  if (allMovies.length === 0) {
    return [];
  }
  
  // Cosine similarity search on embeddings

  // Compute sentence embeddings
  const texts = allMovies.map((movie) => movie.name);
  const embeddings = await createMultipleEmbeddings(texts);

  const { watchHistory } = preferences;

  // Find the most recent movie watched by the user that has been watched for at least 90%
  let recentWatchedMovie = watchHistory
    .filter((watch) => watch.progress >= 0.9)
    .sort((a, b) => b.watchedAt.getTime() - a.watchedAt.getTime())[0];

  if (!recentWatchedMovie && watchHistory.length > 0) {
    recentWatchedMovie = watchHistory[0];
  }

  const movieData = allMovies.find(
    (movie) => movie.id === recentWatchedMovie?.movieId
  );

  const embeddedQuery = await createOneEmbedding(movieData?.name || "");

  // Compute cosine similarity
  const similarities: number[] = [];

  for (let i = 0; i < embeddings.length; i++) {
    const similarity = cosineSimilarity(embeddings[i], embeddedQuery);
    similarities.push(similarity);
  }

  // Sort by similarity
  const sortedMovies = allMovies
    .map((movie, index) => ({ ...movie, similarity: similarities[index] }))
    .sort((a, b) => b.similarity - a.similarity);

  return sortedMovies;
}
