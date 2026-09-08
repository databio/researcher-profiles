/**
 * Probe self-test: verify that the local model produces compatible embeddings
 * for a given group's backend_spec.
 *
 * Before a group's centroids are used with a locally-embedded query, run the
 * probe once per group:
 * - Embed index.json's probe.text locally
 * - Cosine it against probe.vector
 * - Above 0.99: verified
 * - 0.9 to 0.99: degraded (usable but flagged)
 * - Below 0.9: incompatible (free-text disabled for that group)
 */

import { cosine } from "./ops";
import { embedQuery, ensureModel } from "./localModel";

export type ProbeVerdict = "verified" | "degraded" | "incompatible" | "untested";

export interface ProbeResult {
  verdict: ProbeVerdict;
  cosine: number | null;
  backendSpec: string;
}

// Cache probe results per backend_spec
const probeCache = new Map<string, ProbeResult>();

/**
 * Run the probe self-test for a given backend_spec.
 *
 * @param backendSpec - The embedding backend specification string
 * @param probeText - The fixed probe sentence from index.json
 * @param probeVector - The publisher's embedding of the probe sentence
 */
export async function runProbe(
  backendSpec: string,
  probeText: string,
  probeVector: number[],
): Promise<ProbeResult> {
  const cached = probeCache.get(backendSpec);
  if (cached) return cached;

  try {
    await ensureModel();
    const localVec = await embedQuery(probeText);
    const publisherVec = new Float32Array(probeVector);

    if (localVec.length !== publisherVec.length) {
      const result: ProbeResult = {
        verdict: "incompatible",
        cosine: null,
        backendSpec,
      };
      probeCache.set(backendSpec, result);
      return result;
    }

    const sim = cosine(localVec, publisherVec);

    let verdict: ProbeVerdict;
    if (sim > 0.99) {
      verdict = "verified";
    } else if (sim > 0.9) {
      verdict = "degraded";
    } else {
      verdict = "incompatible";
    }

    const result: ProbeResult = { verdict, cosine: sim, backendSpec };
    probeCache.set(backendSpec, result);
    return result;
  } catch {
    const result: ProbeResult = {
      verdict: "incompatible",
      cosine: null,
      backendSpec,
    };
    probeCache.set(backendSpec, result);
    return result;
  }
}

export function getCachedProbe(backendSpec: string): ProbeResult | null {
  // Check in-memory cache first, then localStorage.
  const mem = probeCache.get(backendSpec);
  if (mem) return mem;
  try {
    const raw = localStorage.getItem(`rp-probe:${backendSpec}`);
    if (raw) {
      const parsed: ProbeResult = JSON.parse(raw);
      probeCache.set(backendSpec, parsed);
      return parsed;
    }
  } catch {
    // ignore
  }
  return null;
}

/**
 * Ensure the probe has been run for a given backend_spec. Lazily loads the
 * model on first call. Caches the verdict in localStorage so a repeat visit
 * does not re-download the model.
 */
export async function ensureProbe(
  backendSpec: string,
  probeText: string,
  probeVector: number[],
): Promise<ProbeResult> {
  const cached = getCachedProbe(backendSpec);
  if (cached) return cached;
  const result = await runProbe(backendSpec, probeText, probeVector);
  try {
    localStorage.setItem(`rp-probe:${backendSpec}`, JSON.stringify(result));
  } catch {
    // ignore
  }
  return result;
}
