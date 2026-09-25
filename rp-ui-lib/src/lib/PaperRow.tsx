import { useState } from "react";
import type { PaperEntry } from "../types";
import { Markdown } from "./Markdown";
import styles from "./viewer.module.css";

export type LoadSummary = (paperId: string) => Promise<string>;

/**
 * Resolve a paper's internal full-text artifact (the profile's own copy) to a
 * URL, or null when the profile has none for that paper.
 */
export type FullTextHref = (paperId: string) => string | null;

/**
 * One paper, with small labeled links: "Summary" (when `summary_available`,
 * lazily invokes `loadSummary(paper_id)` and renders the markdown inline),
 * "Full text" (the profile's own copy, via `fullTextHref`), and "Publisher"
 * (the external `full_text_link`). Each shows only when its target exists.
 */
export function PaperRow({
  paper,
  loadSummary,
  fullTextHref,
}: {
  paper: PaperEntry;
  loadSummary?: LoadSummary;
  fullTextHref?: FullTextHref;
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

  const internalFullText =
    paper.paper_id && fullTextHref ? fullTextHref(paper.paper_id) : null;

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
          {canExpand && (
            <button
              className={styles.linkBtn}
              onClick={toggle}
              disabled={loading}
              aria-expanded={expanded}
            >
              {loading ? "Loading…" : expanded ? "Hide summary" : "Summary"}
            </button>
          )}
          {internalFullText && (
            <a
              className={styles.link}
              href={internalFullText}
              target="_blank"
              rel="noreferrer"
              title="The profile's copy of the full text"
            >
              Full text
            </a>
          )}
          {paper.full_text_link && (
            <a
              className={styles.link}
              href={paper.full_text_link}
              target="_blank"
              rel="noreferrer"
              title={paper.full_text_link}
            >
              Publisher
            </a>
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
