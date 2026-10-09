/**
 * Presentational library for the researcher-profiles wire contract. This
 * directory plus `../types.ts` is what a host vendors, so keep it free of
 * host-specific code.
 */
export { ResearcherProfileViewer } from "./ResearcherProfileViewer";
export type { ResearcherProfileViewerProps } from "./ResearcherProfileViewer";
export { ProfileHeader } from "./ProfileHeader";
export { MetadataPanel } from "./MetadataPanel";
export { MarkdownSection } from "./MarkdownSection";
export { PapersList } from "./PapersList";
export { PaperRow } from "./PaperRow";
export type { FullTextHref, LoadSummary } from "./PaperRow";
export { Markdown } from "./Markdown";
export { worksGraphToPapers } from "./works";
export type {
  ProfileDetail,
  ProfileMetadataPayload,
  ProfileSummary,
  PaperEntry,
  PaperSummary,
  // The `PaperRow` type is not re-exported: the component above takes that name.
  ProfileRecord,
  ProfileParts,
  Size,
  Trimmed,
  PaperPage,
  PaperRecordView,
  SummaryBatch,
  FileList,
  TextPage,
  TextSection,
  PassageRequest,
  Passage,
  PassageList,
} from "../types";
