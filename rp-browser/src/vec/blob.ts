/**
 * Parse a centroid blob: assert byte length, construct Float32Array view,
 * verify little-endian platform, expose row accessor.
 */

/**
 * Parse a raw centroid blob into a structured accessor.
 *
 * @param buffer - The raw ArrayBuffer containing float32 centroid data
 * @param count - Expected number of centroid rows
 * @param dim - Dimensionality of each vector
 * @returns An object with a `row(i)` accessor for individual centroids
 */
export function parseCentroidBlob(
  buffer: ArrayBuffer,
  count: number,
  dim: number,
): CentroidData {
  const expectedBytes = count * dim * 4;
  if (buffer.byteLength !== expectedBytes) {
    throw new Error(
      `Centroid blob byte length mismatch: expected ${expectedBytes} ` +
        `(${count} x ${dim} x 4), got ${buffer.byteLength}`,
    );
  }

  // Assert little-endian platform. Float32Array uses the platform's native
  // byte order, which must match the blob's little-endian encoding.
  assertLittleEndian();

  const vectors = new Float32Array(buffer);

  // Defensively re-normalize each row to unit length. The Python side already
  // normalizes in store.centroids, but a hand-published blob may not.
  for (let i = 0; i < count; i++) {
    const offset = i * dim;
    let norm = 0;
    for (let j = 0; j < dim; j++) {
      norm += vectors[offset + j] * vectors[offset + j];
    }
    norm = Math.sqrt(norm);
    if (norm > 0 && Math.abs(norm - 1.0) > 1e-3) {
      for (let j = 0; j < dim; j++) {
        vectors[offset + j] /= norm;
      }
    }
  }

  return {
    vectors,
    count,
    dim,
    row(i: number): Float32Array {
      if (i < 0 || i >= count) {
        throw new RangeError(`Row index ${i} out of bounds [0, ${count})`);
      }
      return vectors.subarray(i * dim, (i + 1) * dim);
    },
  };
}

export interface CentroidData {
  vectors: Float32Array;
  count: number;
  dim: number;
  row(i: number): Float32Array;
}

// ---------------------------------------------------------------------------
// Platform check
// ---------------------------------------------------------------------------

let _isLittleEndian: boolean | null = null;

function assertLittleEndian(): void {
  if (_isLittleEndian === null) {
    const buf = new ArrayBuffer(2);
    new DataView(buf).setInt16(0, 256, true /* littleEndian */);
    _isLittleEndian = new Int16Array(buf)[0] === 256;
  }
  if (!_isLittleEndian) {
    throw new Error(
      "This platform is big-endian. Centroid blobs are stored as " +
        "little-endian float32 and cannot be used on this platform.",
    );
  }
}
