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
 * Data-agnostic shell composing every profile section. No fetching, no auth:
 * the host supplies fetched data and an optional `loadSummary` callback.
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
      {/* `expertise` and `soul` are null both when withheld by privacy tier and
          when empty. A host that wants to say "withheld" reads `detail.withheld`. */}
      <MarkdownSection title="Expertise narrative" body={detail.expertise} />
      <MarkdownSection title="Narrative voice (SOUL)" body={detail.soul} />
      <PapersList papers={papers} loadSummary={loadSummary} />
    </div>
  );
}
