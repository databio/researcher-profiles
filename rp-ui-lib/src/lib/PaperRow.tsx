import { useState } from "react";
import type { PaperEntry } from "../types";
import { Markdown } from "./Markdown";
import styles from "./viewer.module.css";

export type LoadSummary = (paperId: string) => Promise<string>;

/**
 * One paper. When `summary_available`, an "Summary" toggle lazily invokes
 * `loadSummary(paper_id)` and renders the returned markdown inline.
 */
export function PaperRow({
  paper,
  loadSummary,
}: {
  paper: PaperEntry;
  loadSummary?: LoadSummary;
}) {
  const [expanded, setExpanded] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canExpand = Boolean(
    paper.summary_available && paper.paper_id && loadSummary,
  );

  async function toggle() {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (summary === null && !loading && paper.paper_id && loadSummary) {
      setLoading(true);
      setError(null);
      try {
        setSummary(await loadSummary(paper.paper_id));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load summary");
      } finally {
        setLoading(false);
      }
    }
  }

  const metaLine = [paper.first_author, paper.journal, paper.year]
    .filter((x) => x !== null && x !== undefined && x !== "")
    .join(" · ");

  return (
    <div className={styles.paperRow}>
      <div className={styles.paperHead}>
        <div>
          <p className={styles.paperTitle}>{paper.title}</p>
          {metaLine && <p className={styles.paperMeta}>{metaLine}</p>}
        </div>
        <div className={styles.paperActions}>
          {paper.full_text_link && (
            <a
              className={styles.link}
              href={paper.full_text_link}
              target="_blank"
              rel="noreferrer"
            >
              Full text
            </a>
          )}
          {canExpand && (
            <button
              className={styles.expandBtn}
              onClick={toggle}
              disabled={loading}
            >
              {loading ? "Loading…" : expanded ? "Hide summary" : "Summary"}
            </button>
          )}
        </div>
      </div>
      {expanded && (summary || error) && (
        <div className={styles.paperSummary}>
          {error ? (
            <span className={styles.empty}>{error}</span>
          ) : (
            <Markdown source={summary} />
          )}
        </div>
      )}
    </div>
  );
}
