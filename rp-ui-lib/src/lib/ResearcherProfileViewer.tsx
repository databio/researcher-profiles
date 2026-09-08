import type { PaperEntry, ProfileDetail } from "../types";
import { ProfileHeader } from "./ProfileHeader";
import { MetadataPanel } from "./MetadataPanel";
import { MarkdownSection } from "./MarkdownSection";
import { PapersList } from "./PapersList";
import type { LoadSummary } from "./PaperRow";
import styles from "./viewer.module.css";

export interface ResearcherProfileViewerProps {
  /** Full profile detail (metadata + expertise/soul markdown). */
  detail: ProfileDetail;
  /** The paper corpus. Pass [] when unloaded/empty. */
  papers?: PaperEntry[];
  /** Lazy per-paper summary loader; omit to disable summary expansion. */
  loadSummary?: LoadSummary;
}

/**
 * Canonical, data-agnostic shell that composes every profile section. Does no
 * fetching and knows nothing about auth. The host supplies already-fetched
 * data and (optionally) a `loadSummary` callback for lazy per-paper summaries.
 *
 * This is the single component a host app (and any other consumer) vendors and
 * wraps with its own data layer + design-system chrome.
 */
export function ResearcherProfileViewer({
  detail,
  papers = [],
  loadSummary,
}: ResearcherProfileViewerProps) {
  return (
    <div className={styles.viewer}>
      <ProfileHeader metadata={detail.metadata} />
      <MetadataPanel metadata={detail.metadata} />
      {/* `expertise` and `soul` are null when the viewer's privacy tier does
          not reach the backing artifact. Withheld and empty are different
          things on the wire; both render as "no section" here, and a host that
          wants to say "withheld from this viewer" reads `detail.withheld`. */}
      <MarkdownSection title="Expertise narrative" body={detail.expertise} />
      <MarkdownSection title="Narrative voice (SOUL)" body={detail.soul} />
      <PapersList papers={papers} loadSummary={loadSummary} />
    </div>
  );
}
