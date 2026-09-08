/**
 * The app shell: top nav (brand / primary nav / a host slot), an optional
 * sources sidebar for the Browse area, and the routed page in <main>.
 *
 * This is the react-router layout route element; every page renders through
 * its <Outlet>. It carries the cross-cutting shell logic: which nav link is
 * active, whether the Browse analysis tabs and sources sidebar show, and the
 * bootstrap effects (persisted sources + ?list=, in routes.tsx's loader).
 *
 * A host application adds nav links and a header cluster through the shell
 * slots (`slots.tsx`); this file knows nothing about what they contain.
 *
 * Chrome is styled with utility classes + BEM (components.css) and design
 * tokens: no inline styles, no per-file CSS module for the shell.
 */
import { Link, Outlet, useLocation } from "react-router";
import { pageOf } from "./router";
import { SourcesPanel } from "./ui/SourcesPanel";
import { Logo } from "./ui/Logo";
import { BrowseTabs } from "./ui/BrowseTabs";
import { Diagnostics } from "./ui/Diagnostics";
import { useShellSlots } from "./slots";
import { useStore } from "./store";

export function AppLayout() {
  const location = useLocation();
  const page = pageOf(location.pathname);
  const { cards } = useStore();
  const slots = useShellSlots();

  // Persisted-sources restore + ?list= bootstrap happen once, before first
  // paint, in the layout route's loader (see routes.tsx bootstrapLoader).

  const showAnalysisTabs = cards.length > 0;

  // The Browse area is the only place the sources sidebar is meaningful.
  const inBrowse =
    page === "browse" ||
    page === "clusters" ||
    page === "topics" ||
    page === "search" ||
    page === "profile";

  const navClass = (active: boolean) =>
    active ? "nav-link nav-link--active" : "nav-link";

  return (
    <div className="flex flex-col min-h-screen">
      <header className="app-header flex items-center flex-wrap gap-4 px-6 py-3">
        <Link to="/" className="app-brand flex items-center gap-2">
          <Logo />
          <span className="app-brand__name">{__RP_APP_NAME__}</span>
        </Link>
        <nav className="app-header__nav flex flex-wrap gap-1">
          <Link to="/" className={navClass(page === "home")}>
            Home
          </Link>
          <Link to="/browse" className={navClass(inBrowse)}>
            Browse
          </Link>
          <Link to="/validate" className={navClass(page === "validate")}>
            Validate
          </Link>
          <Link to="/skills" className={navClass(page === "skills")}>
            Skills
          </Link>
          <Link to="/about" className={navClass(page === "about")}>
            About
          </Link>
          {slots.navExtras}
        </nav>
        <div className="flex items-center flex-wrap gap-3">
          <Diagnostics />
          {slots.headerRight}
        </div>
      </header>
      <div className="app-body flex flex-1 min-h-0">
        {inBrowse && (
          <aside className="app-sidebar p-4">
            <SourcesPanel />
          </aside>
        )}
        <main className="app-main flex-1 p-6">
          {inBrowse && page !== "profile" && (
            <BrowseTabs page={page} showAnalysis={showAnalysisTabs} />
          )}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
