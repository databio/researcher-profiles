/**
 * Secondary navigation for the Browse area. The four ways of looking at the
 * same registry (the flat inventory, clusters, topics, and free-text search)
 * are one activity, so they live behind one top-nav entry ("Browse") and
 * switch here rather than sitting in the top navbar next to unrelated things
 * (Validate, About, My profile).
 *
 * Clusters / Topics / Search only appear when there is data to analyze and the
 * visitor can read it (`showAnalysis`); Inventory is always present.
 *
 * Styled with the shared `nav-link` component + utilities; no CSS module.
 */
import { Link } from "react-router";
import { buildPath, type Route } from "../router";

interface Props {
  /** Null for a path this app does not serve (a host application's route). */
  page: Route["page"] | null;
  showAnalysis: boolean;
}

export function BrowseTabs({ page, showAnalysis }: Props) {
  const tab = (route: Route, label: string) => {
    const active = page === route.page;
    return (
      <Link
        to={buildPath(route)}
        className={active ? "nav-link nav-link--active" : "nav-link"}
      >
        {label}
      </Link>
    );
  };

  return (
    <nav
      className="browse-tabs flex flex-wrap gap-1 mb-6 pb-2"
      aria-label="Browse views"
    >
      {tab({ page: "browse" }, "Inventory")}
      {showAnalysis && (
        <>
          {tab({ page: "clusters" }, "Clusters")}
          {tab({ page: "topics" }, "Topics")}
          {tab({ page: "search", query: "" }, "Search")}
        </>
      )}
    </nav>
  );
}
