/**
 * The app shell and layout route element: top nav, people search, the Browse
 * sources sidebar, and the routed page in <main>. Styled with utility classes, BEM
 * (components.css) and design tokens, no inline styles.
 */
import { useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router";
import { isBrowsePage, pageOf } from "./router";
import { SourcesPanel } from "./ui/SourcesPanel";
import { Logo } from "./ui/Logo";
import { BrowseTabs } from "./ui/BrowseTabs";
import { Diagnostics } from "./ui/Diagnostics";
import { useShellSlots } from "./slots";
import { useStore } from "./store";

/** The class for a header nav link; hosts filling the `nav` slot use it too. */
export function navClass(active: boolean): string {
  return active ? "nav-link nav-link--active" : "nav-link";
}

export function AppLayout() {
  const location = useLocation();
  const page = pageOf(location.pathname);
  const { cards } = useStore();
  const slots = useShellSlots();

  const showAnalysisTabs = cards.length > 0;

  const inBrowse = isBrowsePage(page);

  return (
    <div className="flex flex-col min-h-screen">
      <header className="app-header flex items-center flex-wrap gap-4 px-6 py-3">
        <Link to="/" className="app-brand flex items-center gap-2">
          {slots.brand ?? (
            <>
              <Logo />
              <span className="app-brand__name">{__RP_APP_NAME__}</span>
            </>
          )}
        </Link>
        <div className="app-header__left flex items-center flex-wrap gap-4">
          <nav className="app-header__nav flex flex-wrap gap-1">
            {slots.nav ?? (
              <>
                <Link to="/browse" className={navClass(inBrowse)}>
                  Browse
                </Link>
                <Link to="/validate" className={navClass(page === "validate")}>
                  Validate
                </Link>
                <Link to="/skills" className={navClass(page === "skills")}>
                  Skills
                </Link>
              </>
            )}
          </nav>
          {cards.length > 0 && <HeaderSearch />}
        </div>
        <div className="flex items-center flex-wrap gap-3">
          <nav className="flex flex-wrap gap-1" aria-label="Help">
            <Link to="/about" className={navClass(page === "about")}>
              About
            </Link>
            {slots.navExtras}
          </nav>
          <Diagnostics />
          {slots.headerRight}
        </div>
      </header>
      <div className="app-body flex flex-1 min-h-0">
        {inBrowse && slots.showSources && (
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

/**
 * People search: the Browse filter (name, affiliation, field; a plain
 * substring match on loaded profiles, no model) reached from any page.
 * Submitting opens /browse?q=<text>.
 */
function HeaderSearch() {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  return (
    <form
      role="search"
      className="app-header__search"
      onSubmit={(e) => {
        e.preventDefault();
        const q = text.trim();
        navigate(q ? `/browse?q=${encodeURIComponent(q)}` : "/browse");
        setText("");
      }}
    >
      <input
        type="search"
        className="input-row__field input-row__field--sm"
        placeholder="Search researchers..."
        aria-label="Search researchers by name, affiliation or field"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
    </form>
  );
}
