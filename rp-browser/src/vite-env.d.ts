/// <reference types="vite/client" />

// Build-time app name, injected via vite.config.ts `define`. Defaults to
// "Researcher Profile Browser"; a deployment sets VITE_RP_APP_NAME.
declare const __RP_APP_NAME__: string;

// @xenova/transformers (dynamically imported, types not available)
declare module "@xenova/transformers" {
  export function pipeline(
    task: string,
    model: string,
    options?: { progress_callback?: (progress: unknown) => void },
  ): Promise<(text: string, options?: Record<string, unknown>) => Promise<{ data: ArrayLike<number> }>>;
  export const env: {
    allowRemoteModels: boolean;
    localModelPath: string;
    backends: {
      onnx: {
        wasm: {
          wasmPaths: string;
        };
      };
    };
  };
}
