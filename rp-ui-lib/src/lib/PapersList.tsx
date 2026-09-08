import { useMemo, useState } from "react";
import type { PaperEntry } from "../types";
import { PaperRow, type LoadSummary } from "./PaperRow";
import styles from "./viewer.module.css";

type SortMode = "year" | "title";

/**
 * The paper corpus, grouped by year (descending) or listed by title. Each row
 * can lazily fetch its per-paper summary via `loadSummary`.
 */
export function PapersList({
  papers,
  loadSummary,
}: {
  papers: PaperEntry[];
  loadSummary?: LoadSummary;
}) {
  const [sort, setSort] = useState<SortMode>("year");

  const grouped = useMemo(() => {
    if (sort === "title") {
      const sorted = [...papers].sort((a, b) =>
        (a.title || "").localeCompare(b.title || ""),
      );
      return [{ year: null as number | null, items: sorted }];
    }
    const byYear = new Map<number | null, PaperEntry[]>();
    for (const p of papers) {
      const y = p.year ?? null;
      if (!byYear.has(y)) byYear.set(y, []);
      byYear.get(y)!.push(p);
    }
    return [...byYear.entries()]
      .sort((a, b) => (b[0] ?? -1) - (a[0] ?? -1))
      .map(([year, items]) => ({ year, items }));
  }, [papers, sort]);

  if (!papers.length) {
    return (
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Papers</h2>
        <p className={styles.empty}>No papers in this profile.</p>
      </section>
    );
  }

  return (
    <section className={styles.section}>
      <div className={styles.controls}>
        <h2 className={styles.sectionTitle}>Papers ({papers.length})</h2>
        <button
          className={styles.expandBtn}
          onClick={() => setSort(sort === "year" ? "title" : "year")}
        >
          Sort: {sort === "year" ? "year" : "title"}
        </button>
      </div>
      <div className={styles.papers}>
        {grouped.map((g, gi) => (
          <div key={gi} className={styles.paperGroup}>
            {g.year !== null && (
              <div className={styles.paperGroupYear}>{g.year}</div>
            )}
            {g.items.map((p, i) => (
              <PaperRow
                key={p.paper_id ?? `${gi}-${i}`}
                paper={p}
                loadSummary={loadSummary}
              />
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}
