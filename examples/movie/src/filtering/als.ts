
/** Creates a rows×cols matrix filled with zeros. */
function zeros(rows: number, cols: number): number[][] {
  const mat: number[][] = [];
  for (let i = 0; i < rows; i++) {
    mat.push(new Array(cols).fill(0));
  }
  return mat;
}

/** Creates an identity matrix of size n×n. */
function identityMatrix(n: number): number[][] {
  const I = zeros(n, n);
  for (let i = 0; i < n; i++) {
    I[i][i] = 1;
  }
  return I;
}

/** Returns the outer product of a vector with itself. */
function outerProduct(vector: number[]): number[][] {
  const n = vector.length;
  const result = zeros(n, n);
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      result[i][j] = vector[i] * vector[j];
    }
  }
  return result;
}

/** Adds two vectors element-wise. */
function addVectors(a: number[], b: number[]): number[] {
  return a.map((val, i) => val + b[i]);
}

/** Multiplies a vector by a scalar. */
function scalarMultiplyVector(scalar: number, vector: number[]): number[] {
  return vector.map((x) => scalar * x);
}

/** Dot product of two vectors. */
function dotProduct(a: number[], b: number[]): number {
  let sum = 0;
  for (let i = 0; i < a.length; i++) {
    sum += a[i] * b[i];
  }
  return sum;
}

/** Multiplies a matrix (2D array) by a vector. */
function multiplyMatrixVector(matrix: number[][], vector: number[]): number[] {
  const result = new Array(matrix.length).fill(0);
  for (let i = 0; i < matrix.length; i++) {
    let sum = 0;
    for (let j = 0; j < vector.length; j++) {
      sum += matrix[i][j] * vector[j];
    }
    result[i] = sum;
  }
  return result;
}

/** Adds two matrices element-wise. */
function addMatrices(A: number[][], B: number[][]): number[][] {
  const rows = A.length;
  const cols = A[0].length;
  const result = zeros(rows, cols);
  for (let i = 0; i < rows; i++) {
    for (let j = 0; j < cols; j++) {
      result[i][j] = A[i][j] + B[i][j];
    }
  }
  return result;
}

/** Multiplies every element of a matrix by a scalar. */
function scalarMultiplyMatrix(scalar: number, matrix: number[][]): number[][] {
  const rows = matrix.length;
  const cols = matrix[0].length;
  const result = zeros(rows, cols);
  for (let i = 0; i < rows; i++) {
    for (let j = 0; j < cols; j++) {
      result[i][j] = scalar * matrix[i][j];
    }
  }
  return result;
}

/** Generates a random vector of the given length. */
function randomVector(n: number): number[] {
  const v: number[] = [];
  for (let i = 0; i < n; i++) {
    v.push(Math.random());
  }
  return v;
}

/** Generates a random matrix with the given number of rows and columns. */
function randomMatrix(rows: number, cols: number): number[][] {
  const matrix: number[][] = [];
  for (let i = 0; i < rows; i++) {
    matrix.push(randomVector(cols));
  }
  return matrix;
}

/**
 * Inverts a square matrix using Gauss-Jordan elimination.
 * (Assumes the matrix is small and non-singular.)
 */
function invertMatrix(matrix: number[][]): number[][] {
  const n = matrix.length;
  // Create the augmented matrix [matrix | I]
  const I = identityMatrix(n);
  const augmented = matrix.map((row, i) => row.concat(I[i]));

  // Forward elimination
  for (let i = 0; i < n; i++) {
    // Find pivot. If the pivot is near zero, swap with a lower row.
    let pivot = augmented[i][i];
    if (Math.abs(pivot) < 1e-10) {
      for (let j = i + 1; j < n; j++) {
        if (Math.abs(augmented[j][i]) > 1e-10) {
          [augmented[i], augmented[j]] = [augmented[j], augmented[i]];
          pivot = augmented[i][i];
          break;
        }
      }
      if (Math.abs(pivot) < 1e-10) {
        throw new Error("Matrix is singular and cannot be inverted.");
      }
    }

    // Normalize the pivot row.
    for (let j = 0; j < 2 * n; j++) {
      augmented[i][j] /= pivot;
    }

    // Eliminate the pivot column from all other rows.
    for (let k = 0; k < n; k++) {
      if (k === i) continue;
      const factor = augmented[k][i];
      for (let j = 0; j < 2 * n; j++) {
        augmented[k][j] -= factor * augmented[i][j];
      }
    }
  }

  // Extract and return the inverse matrix.
  return augmented.map((row) => row.slice(n));
}



/**
 * ALS-based recommender function (with missing values indicated by 0).
 *
 * For each user i and item j, a rating of 0 means that the rating is not set.
 * Only nonzero ratings are used to update the model.
 *
 * @param ratings    A 2D number array (users × items) of ratings, where 0 indicates a missing rating.
 * @param numFactors The number of latent factors (default 3).
 * @param iterations The number of ALS iterations (default 10).
 * @param lambda     The regularization parameter (default 0.1).
 * @returns An object with the learned user factors, item factors, and predicted ratings.
 */
export function alsRecommender(
  ratings: number[][],
  numFactors: number = 3,
  iterations: number = 10,
  lambda: number = 0.1
): { userFactors: number[][]; itemFactors: number[][]; predictions: number[][] } {
  const m = ratings.length; // number of users
  if (m === 0) {
    throw new Error("Ratings matrix is empty.");
  }
  const n = ratings[0].length; // number of items

  let U = randomMatrix(m, numFactors); // m x numFactors
  let V = randomMatrix(n, numFactors); // n x numFactors

  // Run alternating least squares.
  for (let iter = 0; iter < iterations; iter++) {
    // Update user factors.
    for (let i = 0; i < m; i++) {
      let A = zeros(numFactors, numFactors);
      let b = new Array(numFactors).fill(0);
      for (let j = 0; j < n; j++) {
        const r_ij = ratings[i][j];
        // Only use observed ratings (nonzero values).
        if (r_ij !== 0) {
          A = addMatrices(A, outerProduct(V[j]));
          b = addVectors(b, scalarMultiplyVector(r_ij, V[j]));
        }
      }
      // Regularize: add lambda * I
      A = addMatrices(A, scalarMultiplyMatrix(lambda, identityMatrix(numFactors)));
      // Solve for u_i = A⁻¹ * b
      const A_inv = invertMatrix(A);
      U[i] = multiplyMatrixVector(A_inv, b);
    }

    // Update item factors.
    for (let jIndex = 0; jIndex <  n; jIndex++) {
      const j = jIndex % n;
      let A = zeros(numFactors, numFactors);
      let b = new Array(numFactors).fill(0);
      for (let i = 0; i < m; i++) {
        const r_ij = ratings[i][j];
        // Only use observed ratings (nonzero values).
        if (r_ij !== 0) {
          A = addMatrices(A, outerProduct(U[i]));
          b = addVectors(b, scalarMultiplyVector(r_ij, U[i]));
        }
      }
      A = addMatrices(A, scalarMultiplyMatrix(lambda, identityMatrix(numFactors)));
      const A_inv = invertMatrix(A);
      V[j] = multiplyMatrixVector(A_inv, b);
    }
  }

  // Compute full predictions matrix as U * V^T.
  const predictions = zeros(m, n);
  for (let i = 0; i < m; i++) {
    for (let j = 0; j < n; j++) {
      predictions[i][j] = dotProduct(U[i], V[j]);
    }
  }

  return { userFactors: U, itemFactors: V, predictions };
}