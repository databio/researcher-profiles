/**
 * Single fetch chokepoint for JSON resources. Every request returns a
 * discriminated result and never throws.
 *
 * The key mechanism: a cross-origin `fetch` blocked by CORS rejects with
 * an opaque TypeError indistinguishable from a DNS/TLS failure. So on
 * rejection, we re-probe the same URL with `{mode: "no-cors"}`. If the
 * no-cors probe resolves (opaque, status 0) while the cors fetch rejected, the
 * origin is reachable and the failure is definitively a missing/incorrect
 * Access-Control-Allow-Origin -> kind: "cors". If both reject, it is
 * DNS/TLS/offline/blocked -> kind: "network".
 *
 * Note: a 404 served WITH a correct ACAO resolves in cors mode with
 * res.ok === false, which is why this two-probe test is sound. It never
 * fires for a reachable-but-erroring server.
 *
 * Content-Type is readable cross-origin without Access-Control-Expose-Headers
 * (it is a CORS-safelisted response header). The value of
 * Access-Control-Allow-Origin is not readable from JavaScript and can only
 * ever be inferred from success/failure.
 */

/** Maximum response size for JSON documents (32 MB). */
const MAX_JSON_BYTES = 32 * 1024 * 1024;
/** Per-request timeout (20 seconds). */
const REQUEST_TIMEOUT_MS = 20_000;

export type FetchOutcome<T> =
  | {
      ok: true;
      value: T;
      contentType: string | null;
      url: string;
      bytes: number;
    }
  | {
      ok: false;
      kind: "cors" | "network" | "http" | "unauthorized" | "forbidden" | "parse" | "schema";
      status?: number;
      contentType?: string | null;
      url: string;
      detail: string;
    };

export async function fetchJson<T>(url: string): Promise<FetchOutcome<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      mode: "cors",
      credentials: "same-origin",
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    clearTimeout(timer);

    const contentType = res.headers.get("Content-Type");

    if (!res.ok) {
      // 401/403 get their own state (a private registry, not a
      // publishing defect). Split them out of the generic "http" bucket
      // so every consumer can render them differently.
      if (res.status === 401 || res.status === 403) {
        return {
          ok: false,
          kind: res.status === 401 ? "unauthorized" : "forbidden",
          status: res.status,
          contentType,
          url,
          detail: `HTTP ${res.status} ${res.statusText}`,
        };
      }
      return {
        ok: false,
        kind: "http",
        status: res.status,
        contentType,
        url,
        detail: `HTTP ${res.status} ${res.statusText}`,
      };
    }

    const text = await res.text();
    if (text.length > MAX_JSON_BYTES) {
      return {
        ok: false,
        kind: "parse",
        contentType,
        url,
        detail: `Response exceeds ${MAX_JSON_BYTES} byte limit (got ${text.length})`,
      };
    }

    let value: T;
    try {
      value = JSON.parse(text) as T;
    } catch (e) {
      return {
        ok: false,
        kind: "parse",
        contentType,
        url,
        detail: `JSON parse error: ${e instanceof Error ? e.message : String(e)}`,
      };
    }

    return {
      ok: true,
      value,
      contentType,
      url,
      bytes: text.length,
    };
  } catch (err) {
    clearTimeout(timer);

    // Two-probe CORS detection: re-probe with no-cors to distinguish CORS
    // from network failure.
    try {
      const probeController = new AbortController();
      const probeTimer = setTimeout(() => probeController.abort(), 5000);
      const probe = await fetch(url, {
        mode: "no-cors",
        credentials: "same-origin",
        signal: probeController.signal,
      });
      clearTimeout(probeTimer);
      // no-cors probe resolved (opaque response, status 0): origin is
      // reachable but CORS headers are missing or wrong.
      void probe;
      return {
        ok: false,
        kind: "cors",
        url,
        detail:
          "The origin is reachable but does not send a usable " +
          "Access-Control-Allow-Origin header. This header's value " +
          "cannot be read from JavaScript. This is inferred from the " +
          "two-probe test (cors fetch rejected, no-cors probe succeeded).",
      };
    } catch {
      // Both cors and no-cors failed: genuine network/DNS/TLS/offline issue.
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
