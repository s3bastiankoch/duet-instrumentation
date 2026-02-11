import mongoose from "mongoose";

const mongoUrl = process.env.MONGO_URL || "mongodb://127.0.0.1:27017/test";

await mongoose.connect(mongoUrl);

// TypeScript interfaces
export interface IMovie {
  showId: string;
  id: number;
  original_language: string;
  original_title?: string | null;
  overview: string;
  popularity: number;
  poster_path: string;
  release_date?: string | null;
  revenue?: number | null;
  name: string;
  vote_average: number;
  vote_count: number;

  region?: string;
}

export interface IPreference {
  userId: string;
  likedMovies: string[];
  dislikedMovies: string[];
  // Add fields for "my list" and "watch history" as needed
}

// Mongoose schemas
const MovieSchema = new mongoose.Schema<IMovie>({
  showId: { type: String, required: true },
  id: { type: Number, required: true },
  original_language: { type: String, required: true },
  original_title: { type: String, required: false, default: null },
  overview: { type: String, required: true },
  popularity: { type: Number, required: true },
  poster_path: { type: String, required: true },
  release_date: { type: String, required: false, default: null },
  revenue: { type: Number, required: false, default: null },
  name: { type: String, required: true },

  region: { type: String, required: false },

  // TODO: Regions as an array of strings
});

export const MovieModel = mongoose.model<IMovie>("Movie", MovieSchema);

const PreferenceSchema = new mongoose.Schema<IPreference>({
  userId: { type: String, required: true },
  likedMovies: { type: [String], required: true },
  dislikedMovies: { type: [String], required: true }
  // Add fields for "my list" and "watch history" as needed
});

export const PreferenceModel = mongoose.model<IPreference>("Preference", PreferenceSchema);
