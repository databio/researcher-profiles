/**
 * Secondary navigation for the Browse area: inventory, clusters, topics, and
 * search. The last three show only when `showAnalysis` is true.
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
