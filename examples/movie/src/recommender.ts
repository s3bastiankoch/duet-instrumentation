export function recommendMovies(query: string, movies: { name: string }[]) {
  return movies.filter((movie) => movie.name.includes(query));
}