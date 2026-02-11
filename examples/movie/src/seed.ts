import { MovieModel, PreferenceModel, IMovie, IPreference } from "./model";
import { faker } from "@faker-js/faker";
// import seed from "./seed.json";
import { randomUUID } from "crypto";
import { createMultipleEmbeddings, preloadModel } from "./embedding";

faker.seed(42);

export const cachedEmbeddings: Record<string, number[]> = {};

export async function seedDB() {
  await preloadModel();

  const movieCount = await MovieModel.countDocuments();
  if (movieCount >= 500) {
    console.log("Database already seeded. Skipping seeding.");
    return;
  }

  console.log("Seeding the database with some movies...");

  // Clear the database
  await MovieModel.deleteMany({});
  await PreferenceModel.deleteMany({});

  const movies: IMovie[] = [];

  for (let i = 0; i < 500; i++) {
    const movie: IMovie = {
      id: i,
      original_title: faker.lorem.words(3),
      overview: faker.lorem.paragraph(),
      release_date: faker.date
        .past({
          years: 10,
          refDate: new Date(),
        })
        .toISOString(),
      popularity: faker.number.int({ min: 0, max: 100 }),
      poster_path: faker.image.url(),
      revenue: faker.number.int({ min: 0, max: 1000000000 }),
      name: faker.lorem.words(2),
      vote_average: faker.number.float({ min: 0, max: 10 }),
      vote_count: faker.number.int({ min: 0, max: 10000 }),
      showId: randomUUID(),
      original_language: faker.helpers.arrayElement(["en", "es", "fr", "de"]),
      region: faker.helpers.arrayElement(["US", "ES", "FR", "DE"]),
    };
    movies.push(movie);
  }

  // Save all movies to the database
  await MovieModel.insertMany(movies);

  const embeddings = await createMultipleEmbeddings(
    movies.map((movie) => movie.name)
  );

  for (let i = 0; i < movies.length; i++) {
    cachedEmbeddings[movies[i].name] = embeddings[i];
  }

  console.log("Movies seeded successfully!");
  console.log("Seeding user preferences...");

  // seed.userPreferences.forEach(async (preference) => {
  //   await PreferenceModel.create(preference);
  // });

  const userPreferences: IPreference[] = [];

  for (let i = 0; i < 20; i++) {
    // Like 1-5 movies
    const likedMovies: string[] = [];

    const likedMoviesCount = Math.floor(Math.random() * 5) + 1;
    for (let j = 0; j < likedMoviesCount; j++) {
      const randomMovie = Math.floor(Math.random() * 10000);
      likedMovies.push(randomMovie.toString());
    }

    // Dislike 1-5 movies
    const dislikedMovies: string[] = [];
    const dislikedMoviesCount = Math.floor(Math.random() * 5) + 1;
    for (let j = 0; j < dislikedMoviesCount; j++) {
      const randomMovie = Math.floor(Math.random() * 10000);
      dislikedMovies.push(randomMovie.toString());
    }

    // Create a preference object

    const preference: IPreference = {
      userId: `user${i}`,
      likedMovies,
      dislikedMovies,
    };

    // Add the preference to the array
    userPreferences.push(preference);
  }

  // Save all user preferences to the database
  await PreferenceModel.insertMany(userPreferences);
  console.log("User preferences seeded successfully!");

  console.log("Seeding completed!");
}
