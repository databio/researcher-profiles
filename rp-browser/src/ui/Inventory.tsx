import { useStore, persistSources } from "../store";
import type { ProfileCard } from "../store";
import { useState, useMemo } from "react";
import { Copyable } from "./Copyable";
import { Link } from "react-router";
import { navigate, buildPath } from "../router";
import { useShellSlots } from "../slots";

const REPO_URL = "https://github.com/databio/researcher-profiles";
const EXAMPLE_URL =
  "https://profiles.example.org/api/v1/profiles/doe-jane/profile.jsonld";

export function Inventory() {
  const { cards } = useStore();
  const slots = useShellSlots();
  const [filter, setFilter] = useState("");
  const [sortKey, setSortKey] = useState<keyof ProfileCard>("name");
  const [sortAsc, setSortAsc] = useState(true);

  const filtered = useMemo(() => {
    let result = cards;
    if (filter) {
      const lc = filter.toLowerCase();
      result = result.filter(
        (c) =>
          c.name.toLowerCase().includes(lc) ||
          (c.affiliation || "").toLowerCase().includes(lc) ||
          (c.field || "").toLowerCase().includes(lc),
      );
    }
    return [...result].sort((a, b) => {
      const av = a[sortKey] ?? "";
      const bv = b[sortKey] ?? "";
      const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortAsc ? cmp : -cmp;
    });
  }, [cards, filter, sortKey, sortAsc]);

  function toggleSort(key: keyof ProfileCard) {
    if (sortKey === key) {
      setSortAsc(!sortAsc);
    } else {
      setSortKey(key);
      setSortAsc(true);
    }
  }

  const sortIndicator = (key: keyof ProfileCard) =>
    sortKey === key ? (sortAsc ? " ▲" : " ▼") : "";

  if (cards.length === 0) {
    return <>{slots.emptyScreen ?? <Hero />}</>;
  }

  return (
    <div>
      <div className="toolbar">
        <input
          className="toolbar__filter"
          placeholder="Filter by name, affiliation, or field..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="toolbar__count">
          {filtered.length} of {cards.length}
        </span>
      </div>
      <div className="data-table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th onClick={() => toggleSort("name")}>Name{sortIndicator("name")}</th>
              <th onClick={() => toggleSort("affiliation")}>Affiliation{sortIndicator("affiliation")}</th>
              <th onClick={() => toggleSort("field")}>Field{sortIndicator("field")}</th>
              <th onClick={() => toggleSort("level")}>Level{sortIndicator("level")}</th>
              <th onClick={() => toggleSort("paperCount")}>Papers{sortIndicator("paperCount")}</th>
              <th>Origin</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((c) => (
              <tr
                key={c.base}
                className="data-table__row--clickable"
                onClick={() => navigate({ page: "profile", url: c.base })}
              >
                <td className="font-medium">{c.name}</td>
                <td>{c.affiliation || "-"}</td>
                <td>{c.field || "-"}</td>
                <td>{c.level}</td>
                <td>{c.paperCount}</td>
                <td className="text-muted text-sm">
                  <Copyable value={c.base} title="Copy profile URL">
                    {new URL(c.base).hostname}
                  </Copyable>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Hero() {
  const [url, setUrl] = useState("");

  function add(value: string) {
    const v = value.trim();
    if (!v) return;
    useStore.addSource(v);
    persistSources();
    setUrl("");
  }

  return (
    <div className="inv-hero">
      <h1 className="inv-hero__title">Researcher Profile Browser</h1>
      <p className="inv-hero__lede">
        Read <em>published researcher profiles</em> (a researcher&rsquo;s
        expertise, papers, and AI-generated summaries) straight from their public
        URL. Everything runs in your browser; there is no server and no account.
      </p>

      <div className="input-row">
        <input
          className="input-row__field"
          type="url"
          placeholder="Paste a profile URL (…/profile.jsonld) or a profile-list URL"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add(url)}
        />
        <button className="btn btn--alt" onClick={() => add(url)}>
          Load
        </button>
      </div>

      <p className="inv-hero__hint">
        Don&rsquo;t have one handy? Try the example:{" "}
        <button className="btn--link" onClick={() => add(EXAMPLE_URL)}>
          load a sample profile
        </button>
        .
      </p>

      <div className="inv-hero__links">
        <Link to={buildPath({ page: "about" })}>What is a researcher profile?</Link>
        <span className="inv-hero__dot">·</span>
        <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
          Source on GitHub ↗
        </a>
      </div>
    </div>
  );
}
