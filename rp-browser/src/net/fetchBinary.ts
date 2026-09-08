/**
 * Fetch a binary resource (ArrayBuffer). Same discriminated outcome as
 * fetchJson, used for .bin centroid blobs.
 */

import type { FetchOutcome } from "./fetchJson";

/** Maximum binary response size (64 MB). */
const MAX_BINARY_BYTES = 64 * 1024 * 1024;
/** Per-request timeout (20 seconds). */
const REQUEST_TIMEOUT_MS = 20_000;

export async function fetchBinary(
  url: string,
): Promise<FetchOutcome<ArrayBuffer>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      mode: "cors",
      signal: controller.signal,
    });
    clearTimeout(timer);

    const contentType = res.headers.get("Content-Type");

    if (!res.ok) {
      return {
        ok: false,
        kind: "http",
        status: res.status,
        contentType,
        url,
        detail: `HTTP ${res.status} ${res.statusText}`,
      };
    }

    const buf = await res.arrayBuffer();
    if (buf.byteLength > MAX_BINARY_BYTES) {
      return {
        ok: false,
        kind: "parse",
        contentType,
        url,
        detail: `Response exceeds ${MAX_BINARY_BYTES} byte limit (got ${buf.byteLength})`,
      };
    }

    return {
      ok: true,
      value: buf,
      contentType,
      url,
      bytes: buf.byteLength,
    };
  } catch (err) {
    clearTimeout(timer);

    // Two-probe CORS detection, same as fetchJson.
    try {
      const probeController = new AbortController();
      const probeTimer = setTimeout(() => probeController.abort(), 5000);
      await fetch(url, {
        mode: "no-cors",
        signal: probeController.signal,
      });
      clearTimeout(probeTimer);
      return {
        ok: false,
        kind: "cors",
        url,
        detail:
          "The origin is reachable but does not send a usable " +
          "Access-Control-Allow-Origin header.",
      };
    } catch {
      return {
        ok: false,
        kind: "network",
        url,
        detail:
          err instanceof Error
            ? err.message
            : "Network error (DNS, TLS, or offline)",
      };
    }
  }
}
