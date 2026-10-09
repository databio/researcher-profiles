/**
 * Route model and path helpers, free of React imports. The router itself is
 * built in `routes.tsx`.
 *
 * Routes (browser paths, not hash fragments):
 *   /                              - landing (hero + what-this-is)
 *   /browse                        - sources + inventory
 *   /p?u=<encodeURIComponent(url)> - profile detail (`u` may also be a slug,
 *                                    see `resolveProfileRef`)
 *   /clusters                      - cluster view
 *   /topics                        - topic view
 *   /search?q=...                  - free-text search
 *   /validate?u=<url>              - conformance validator
 *   /skills                        - agent skills (SKILL.md) directory
 *   /about                         - what a researcher profile is
 *
 * A host application that mounts this app adds its own routes on top; they are
 * not modelled here, and `pageOf` returns null for them.
 *
 * ?list=<url> on first load bootstraps a source before routing settles.
 */

import type { ProfileCard, SourceEntry } from "./store";

export type Route =
  | { page: "home" }
  | { page: "browse" }
  | { page: "about" }
  | { page: "profile"; url: string }
  | { page: "clusters" }
  | { page: "topics" }
  | { page: "search"; query: string }
  | { page: "validate"; url: string }
  | { page: "skills" };

/** The history-API path (with query where relevant) for a route. */
export function buildPath(route: Route): string {
  switch (route.page) {
    case "home":
      return "/";
    case "browse":
      return "/browse";
    case "about":
      return "/about";
    case "profile":
      return `/p?u=${encodeURIComponent(route.url)}`;
    case "clusters":
      return "/clusters";
    case "topics":
      return "/topics";
    case "search":
      return `/search?q=${encodeURIComponent(route.query)}`;
    case "validate":
      return `/validate?u=${encodeURIComponent(route.url)}`;
    case "skills":
      return "/skills";
  }
}

/**
 * The active page for a (basename-relative) pathname, for nav highlighting.
 * `null` for anything this app does not serve, including a host's own routes.
 */
export function pageOf(pathname: string): Route["page"] | null {
  const p = pathname.replace(/\/+$/, "") || "/";
  switch (p) {
    case "/":
      return "home";
    case "/browse":
      return "browse";
    case "/p":
      return "profile";
    case "/clusters":
      return "clusters";
    case "/topics":
      return "topics";
    case "/search":
      return "search";
    case "/validate":
      return "validate";
    case "/skills":
      return "skills";
    case "/about":
      return "about";
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Imperative navigation from non-hook code
// ---------------------------------------------------------------------------

interface NavigableRouter {
  navigate: (to: string) => void | Promise<void>;
}

let liveRouter: NavigableRouter | null = null;

/**
 * Register the live router instance. Registration, rather than an import,
 * avoids the cycle `routes.tsx` -> components -> `router.ts`.
 */
export function registerRouter(router: NavigableRouter): void {
  liveRouter = router;
}

/**
 * Imperative navigation for plain functions. Prefer `<Link to={buildPath(route)}>`
 * in JSX. Warns and does nothing before the router is registered.
 */
export function navigate(route: Route): void {
  const to = buildPath(route);
  if (liveRouter) {
    void liveRouter.navigate(to);
  } else if (typeof console !== "undefined") {
    console.warn(`navigate() called before router was registered: ${to}`);
  }
}

/** Get the ?list=<url> bootstrap parameter from the initial page load. */
export function getBootstrapList(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("list");
}

// ---------------------------------------------------------------------------
// The profile route's `u`: a profile URL or a slug
// ---------------------------------------------------------------------------

export type ProfileRef =
  | { kind: "url"; url: string }
  | { kind: "pending" }
  | { kind: "error"; message: string };

/**
 * What `/p?u=<ref>` points at. An http(s) URL is used as is. Anything else is a
 * slug looked up among loaded cards, the host's home source first. `pending`
 * while a source still loads; an `error` result, never a throw, otherwise.
 */
export function resolveProfileRef(
  ref: string,
  cards: ProfileCard[],
  sources: SourceEntry[],
): ProfileRef {
  const text = ref.trim();
  if (!text) return { kind: "error", message: "No profile given. Open a profile from Browse, or pass ?u=<profile URL>." };
  if (/^[a-z][a-z0-9+.-]*:/i.test(text)) {
    return /^https?:\/\//i.test(text)
      ? { kind: "url", url: text }
      : { kind: "error", message: `Only http(s) profile URLs are supported, got "${text}".` };
  }
  const home = new Set(sources.filter((s) => s.builtin).map((s) => s.url));
  const matches = cards.filter((c) => c.slug === text);
  const match = matches.find((c) => home.has(c.sourceUrl)) ?? matches[0];
  if (match) return { kind: "url", url: match.base };
  if (sources.some((s) => s.status === "loading")) return { kind: "pending" };
  return {
    kind: "error",
    message: `"${text}" is not a profile URL, and no loaded source has a profile with that slug.`,
  };
}
