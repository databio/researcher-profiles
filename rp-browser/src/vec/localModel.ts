/**
 * Local query encoder using transformers.js (Xenova/all-MiniLM-L6-v2).
 *
 * Lazily loaded only when the user first types a free-text query. The
 * quantized ONNX model is self-hosted under public/models/ so the app has
 * no third-party runtime dependency, keeps a tight CSP, and works offline.
 */

export type ModelLoadProgress = {
  status: string;
  file?: string;
  progress?: number;
  loaded?: number;
  total?: number;
};

export type ProgressCallback = (progress: ModelLoadProgress) => void;

let pipeline: any = null;
let loading: Promise<void> | null = null;

/** Ensure the embedding model is loaded, reporting progress on first load. */
export async function ensureModel(
  onProgress?: ProgressCallback,
): Promise<void> {
  if (pipeline) return;
  if (loading) return loading;

  loading = (async () => {
    onProgress?.({ status: "loading", progress: 0 });

    try {
      const { pipeline: createPipeline, env } = await import(
        "@huggingface/transformers"
      );

      // Paths hang off BASE_URL, not "./", so they resolve from any route.
      const base = import.meta.env.BASE_URL;
      const allowRemote = import.meta.env.VITE_RP_ALLOW_REMOTE_MODELS === "1";
      env.allowRemoteModels = allowRemote;
      if (!allowRemote) {
        env.allowLocalModels = true;
        env.localModelPath = `${base}models/`;
      }
      // ORT runtime files copied to /ort/ by vite.config.ts; otherwise ORT uses a CDN.
      const wasm = env.backends.onnx.wasm;
      if (wasm) wasm.wasmPaths = `${base}ort/`;

      pipeline = await createPipeline(
        "feature-extraction",
        "Xenova/all-MiniLM-L6-v2",
        {
          // q8 = onnx/model_quantized.onnx, the file fetch-model.mjs
          // downloads and the one the stored vectors were checked against.
          device: "wasm",
          dtype: "q8",
          progress_callback: onProgress as ((progress: unknown) => void) | undefined,
        },
      );

      onProgress?.({ status: "ready", progress: 100 });
    } catch (e) {
      loading = null;
      pipeline = null;
      onProgress?.({
        status: "error",
        file: String(e),
      });
      throw e;
    }
  })();

  return loading;
}

/**
 * Embed a query with mean pooling and L2 normalization, matching
 * sentence-transformers' all-MiniLM-L6-v2.
 */
export async function embedQuery(text: string): Promise<Float32Array> {
  await ensureModel();

  if (!pipeline) {
    throw new Error("Model not loaded");
  }

  const output = await pipeline(text, {
    pooling: "mean",
    normalize: true,
  });

  return new Float32Array(output.data);
}
