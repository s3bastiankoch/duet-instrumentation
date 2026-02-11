import { FeatureExtractionPipeline, pipeline } from "@huggingface/transformers";

class Semaphore {
  private available: number;
  private queue: Array<() => void> = [];

  constructor(maxConcurrency: number) {
    this.available = Math.max(1, maxConcurrency);
  }

  async acquire() {
    if (this.available > 0) {
      this.available--;
      return;
    }

    await new Promise<void>((resolve) => {
      this.queue.push(() => {
        this.available--;
        resolve();
      });
    });
  }

  release() {
    this.available++;
    const next = this.queue.shift();
    if (next) next();
  }

  async run<T>(fn: () => Promise<T>) {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}

class EmbeddingPipeline {
  static task = "feature-extraction" as const;
  static model = "Xenova/all-MiniLM-L6-v2";
  static instance: FeatureExtractionPipeline | null = null;
  static initPromise: Promise<FeatureExtractionPipeline> | null = null;

  static async getInstance(progress_callback = undefined) {
    if (this.instance) return this.instance;
    if (this.initPromise) return await this.initPromise;

    const load = async (retries = 3): Promise<FeatureExtractionPipeline> => {
      try {
        return await pipeline(this.task, this.model, { progress_callback });
      } catch (e) {
        if (retries > 0) {
          console.warn(`Failed to load embedding model, retrying... (${retries} left). Error: ${e}`);
          await new Promise(r => setTimeout(r, 500 + Math.random() * 1000));
          return load(retries - 1);
        }
        throw e;
      }
    };

    this.initPromise = load().then(
      (p) => {
        this.instance = p;
        this.initPromise = null;
        return p;
      }
    );

    return await this.initPromise;
  }
}

export async function preloadModel() {
  await EmbeddingPipeline.getInstance();
}

const embeddingConcurrency = Number.parseInt(
  process.env.EMBEDDING_MAX_CONCURRENCY || "1",
  10
);
const embeddingSemaphore = new Semaphore(
  Number.isFinite(embeddingConcurrency) ? embeddingConcurrency : 1
);

async function embedWithRetry(extractor: FeatureExtractionPipeline, text: string) {
  // Under high load the underlying runtime can throw transient errors.
  // A tiny retry helps, but the main fix is limiting concurrency.
  try {
    return (await extractor(text, {
      pooling: "mean",
      normalize: true,
    })).tolist()[0] as number[];
  } catch (e) {
    // brief backoff
    await new Promise((r) => setTimeout(r, 25));
    return (await extractor(text, {
      pooling: "mean",
      normalize: true,
    })).tolist()[0] as number[];
  }
}

export async function createOneEmbedding(text: string) {
  const extractor = await EmbeddingPipeline.getInstance();

  return await embeddingSemaphore.run(() => embedWithRetry(extractor, text));
}

export async function createMultipleEmbeddings(texts: string[]) {
  const extractor = await EmbeddingPipeline.getInstance();

  // Batch embedding can be memory-heavy; keep it serialized with request embeddings.
  return await embeddingSemaphore.run(async () => {
    return (await extractor(texts, {
      pooling: "mean",
      normalize: true,
    })).tolist() as number[][];
  });
}