/**
 * Canonical presentational library for the researcher-profiles wire contract.
 *
 * These components are data-agnostic (they take already-fetched props and do no
 * fetching or auth) and self-contained (markdown via marked + dompurify, styles
 * via a scoped CSS module). This directory + `../types.ts` is what a host app
 * vendors; keep it free of any harness- or host-specific code.
 */
export { ResearcherProfileViewer } from "./ResearcherProfileViewer";
export type { ResearcherProfileViewerProps } from "./ResearcherProfileViewer";
export { ProfileHeader } from "./ProfileHeader";
export { MetadataPanel } from "./MetadataPanel";
export { MarkdownSection } from "./MarkdownSection";
export { PapersList } from "./PapersList";
export { PaperRow } from "./PaperRow";
export type { LoadSummary } from "./PaperRow";
export { Markdown } from "./Markdown";
export { worksGraphToPapers } from "./works";
export type {
  ProfileDetail,
  ProfileMetadataPayload,
  ProfileSummary,
  PaperEntry,
  PaperSummary,
} from "../types";
