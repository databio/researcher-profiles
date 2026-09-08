/**
 * Route model + path helpers for the browser (history) router.
 *
 * The app runs on react-router v7's `createBrowserRouter` (see `routes.tsx`).
 * This module holds the small, framework-agnostic pieces the rest of the app
 * depends on:
 *
 *   - the `Route` discriminated union, so call sites say
 *     `navigate({ page: "profile", url })` instead of hand-writing paths;
 *   - `buildPath(route)` -> the real, history-API path for a route (used by
 *     `<Link to>` and by `navigate`);
 *   - `navigate(route)` -> imperative navigation from non-hook code, delegating
 *     to the live router registered by `routes.tsx` (`registerRouter`);
 *   - `pageOf(pathname)` -> the inverse of `buildPath`, so the shell can derive
 *     the active page from `useLocation().pathname` for nav highlighting.
 *
 * Routes (browser paths, not hash fragments):
 *   /                              - landing (hero + what-this-is)
 *   /browse                        - sources + inventory
 *   /p?u=<encodeURIComponent(url)> - profile detail
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
 * `null` for anything this app does not serve, including a host application's
 * own routes, so mounting this app under a host never lights up the wrong
 * nav link.
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
 * Register the live router instance. Called once by `routes.tsx` after the
 * browser router is created. Kept here (rather than importing the router into
 * this module) so `router.ts` stays free of any React/route-table imports and
 * there is no import cycle: `routes.tsx` -> components -> `router.ts`.
 */
export function registerRouter(router: NavigableRouter): void {
  liveRouter = router;
}

/**
 * Imperative navigation for event handlers in plain functions. Prefer
 * `<Link to={buildPath(route)}>` in JSX; use this where a click handler must
 * navigate. No-ops (with a warning) before the router is registered, which
 * cannot happen at runtime once the app has mounted.
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
