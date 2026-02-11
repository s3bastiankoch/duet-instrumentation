import { RateLimiterMongo } from "rate-limiter-flexible";
import { MongoClient } from "mongodb";
import { NextFunction, Request, Response } from "express";

const mongoUrl = process.env.MONGO_URL || "mongodb://127.0.0.1:27017/test";

const client = new MongoClient(mongoUrl);

const points = Number.parseInt(process.env.RATE_LIMIT_POINTS || "100000", 10);
const durationSeconds = Number.parseInt(
  process.env.RATE_LIMIT_DURATION_SECONDS || "1",
  10
);

const rateLimiter = new RateLimiterMongo({
  storeClient: client,
  keyPrefix: "middleware",
  points: Number.isFinite(points) ? points : 10,
  duration: Number.isFinite(durationSeconds) ? durationSeconds : 1,
});

export function rateLimiterMiddleware(
  req: Request,
  res: Response,
  next: NextFunction
) {
  rateLimiter
    .consume(req.url)
    .then(() => {
      next();
    })
    .catch((e) => {
      console.error(e);
      res.status(429).send("Too Many Requests");
    });
}
