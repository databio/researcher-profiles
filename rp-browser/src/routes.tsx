import { createBrowserRouter, type RouteObject } from "react-router";
import { registerRouter, getBootstrapList } from "./router";
import { useStore, loadPersistedSources } from "./store";
import { AppLayout } from "./AppLayout";
import { Home } from "./ui/Home";
import { Inventory } from "./ui/Inventory";
import { ProfilePage } from "./ui/ProfilePage";
import { Clusters } from "./ui/Clusters";
import { Topics } from "./ui/Topics";
import { Search } from "./ui/Search";
import { Validator } from "./ui/Validator";
import { Skills } from "./ui/Skills";
import { About } from "./ui/About";
import { AnalysisRoute } from "./ui/AnalysisRoute";

// Follow the Vite build base so sub-path mounts route correctly: a build with
// `vite build --base=/profiles/` (served under /profiles) gets basename
// "/profiles"; the default root base "/" is unchanged.
const basename = import.meta.env.BASE_URL.replace(/\/$/, "") || "/";

/**
 * One-time app bootstrap, run as the layout route's loader so it happens
 * before first paint (rather than in a post-mount effect): restore the
 * persisted sources, then seed any ?list= source. The store (a
 * useSyncExternalStore singleton) stays the source of truth. The loader only
 * triggers store actions and returns nothing. Guarded so the layout route,
 * which matches on every navigation, does the work only once.
 */
let didBootstrap = false;
function bootstrapLoader() {
  if (!didBootstrap) {
    didBootstrap = true;
    loadPersistedSources();
    const listUrl = getBootstrapList();
    if (listUrl) useStore.addSource(listUrl);
  }
  return null;
}

/** Every route this app serves on its own, with no host application. */
const publicRoutes: RouteObject[] = [
  // Landing.
  { index: true, element: <Home /> },
  // Browse area (inventory + the three analysis views + a profile).
  { path: "browse", element: <Inventory /> },
  // Profile detail, deep-linkable via ?u=<encodeURIComponent(url)>.
  { path: "p", element: <ProfilePage /> },
  // Analysis views: gated behind data + read access via AnalysisRoute.
  {
    path: "clusters",
    element: (
      <AnalysisRoute>
        <Clusters />
      </AnalysisRoute>
    ),
  },
  {
    path: "topics",
    element: (
      <AnalysisRoute>
        <Topics />
      </AnalysisRoute>
    ),
  },
  {
    path: "search",
    element: (
      <AnalysisRoute>
        <Search />
      </AnalysisRoute>
    ),
  },
  // Always reachable, no data required.
  { path: "validate", element: <Validator /> },
  { path: "skills", element: <Skills /> },
  { path: "about", element: <About /> },
];

/**
 * Build the router. A host application mounting this app (see `mount.tsx`)
 * passes its own routes, which land inside the shell layout so they get the
 * same nav, sidebar and bootstrap. The unknown-path catch-all stays last, so
 * host routes are matched before it.
 */
export function createRouter(extraRoutes: RouteObject[] = []) {
  const router = createBrowserRouter(
    [
      {
        element: <AppLayout />,
        loader: bootstrapLoader,
        children: [
          ...publicRoutes,
          ...extraRoutes,
          // Unknown path -> landing.
          { path: "*", element: <Home /> },
        ],
      },
    ],
    { basename },
  );
  // Let plain (non-hook) event handlers navigate via `navigate()` in router.ts.
  registerRouter(router);
  return router;
}
