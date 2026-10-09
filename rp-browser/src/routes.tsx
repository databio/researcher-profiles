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

// Follow the Vite build base so sub-path mounts (`--base=/profiles/`) route correctly.
const basename = import.meta.env.BASE_URL.replace(/\/$/, "") || "/";

/**
 * One-time bootstrap: restore persisted sources, then seed any ?list= source.
 * Runs as the layout loader so it happens before first paint. Guarded because
 * the layout route matches on every navigation.
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
 * Build the router. Host routes land inside the shell layout, before the
 * unknown-path catch-all.
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
