/**
 * Wire contract for the researcher-profiles HTTP API.
 *
 * GENERATED FILE: do not edit by hand.
 * Regenerate with `npm run gen:types` (source: src/researcher_profiles/api_models.py).
 */

/**
 * HTTP wire types for the researcher-profiles API (researcher_profiles.models.api). Generated; do not edit by hand.
 */
export interface ResearcherProfileWireContract {
  artifact_tier: ArtifactTier;
  artifact_visibility: ArtifactVisibility;
  file_list: FileList;
  paper_entry: PaperEntry;
  paper_page: PaperPage;
  paper_record_view: PaperRecordView;
  paper_row: PaperRow;
  paper_summary: PaperSummary;
  passage: Passage;
  passage_list: PassageList;
  passage_request: PassageRequest;
  profile_detail: ProfileDetail;
  profile_metadata: ProfileMetadataPayload;
  profile_parts: ProfileParts;
  profile_record: ProfileRecord;
  profile_summary: ProfileSummary;
  section_tier: SectionTier;
  section_tier_report: SectionTierReport;
  size: Size;
  summary_batch: SummaryBatch;
  text_page: TextPage;
  text_section: TextSection;
  trimmed: Trimmed;
  visibility_patch: VisibilityPatch;
  visibility_report: VisibilityReport;
  [k: string]: unknown;
}
/**
 * One artifact's tier, and why: the read side of the visibility API.
 *
 * Every field is computed by ``privacy.explain_tiers``; nothing here is a
 * restatement of the rule in prose. ``visible_to`` and the report's ``counts``
 * come from ``privacy.tier_allows``, so an interface renders a consequence
 * ("a stranger can see 0 of 63 items") instead of teaching a tier lattice.
 */
export interface ArtifactTier {
  content_url: string;
  declared: string;
  effective: string;
  name?: string | null;
  paper_id?: string | null;
  raised_by?: string[];
  role?: string | null;
  visible_to?: string[];
  [k: string]: unknown;
}
/**
 * One per-artifact visibility change. Supply exactly one selector
 * (``content_url``, ``paper_id``, or ``role``) plus the target ``visibility``.
 */
export interface ArtifactVisibility {
  content_url?: string | null;
  paper_id?: string | null;
  role?: string | null;
  visibility: string;
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}/files``: the manifest, labeled for this caller.
 */
export interface FileList {
  files?: {
    [k: string]: unknown;
  }[];
  withheld?: string[];
  [k: string]: unknown;
}
/**
 * One paper as the static publisher and ``rp-ui-lib`` list it.
 *
 * Not an HTTP response model any more: ``GET /profiles/{slug}/papers``
 * answers with :class:`PaperPage`. Kept because the static ``papers`` view
 * and the generated ``rp-ui-lib`` types are built from it.
 */
export interface PaperEntry {
  authors?: string[] | null;
  doi?: string | null;
  first_author?: string | null;
  full_text_link?: string | null;
  journal?: string | null;
  openalex_id?: string | null;
  paper_id?: string | null;
  pmid?: string | null;
  summary_available?: boolean;
  title: string;
  year?: number | null;
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}/papers``: one page of paper rows.
 */
export interface PaperPage {
  filters_applied?: {
    [k: string]: unknown;
  };
  has_more?: boolean;
  items: PaperRow[];
  limit_applied: number;
  next_cursor?: string | null;
  note?: string | null;
  search_mode_used?: ("hybrid" | "keyword") | null;
  total: number;
  [k: string]: unknown;
}
/**
 * One row of ``GET /profiles/{slug}/papers``: enough to choose the next read.
 */
export interface PaperRow {
  doi?: string | null;
  first_author?: string | null;
  journal?: string | null;
  matched_by?: ("keyword" | "semantic")[] | null;
  openalex_id?: string | null;
  paper_id: string;
  score?: number | null;
  summary: Size;
  summary_short?: string | null;
  text: Size;
  title: string;
  version: string;
  year?: number | null;
  [k: string]: unknown;
}
/**
 * What this caller may fetch of one deeper part, and how big it is.
 *
 * ``available`` means *this* caller can fetch it through a route that exists,
 * not that the store holds it. ``bytes`` is the UTF-8 size of the stored
 * body; ``approx_tokens`` is ``bytes // 4``.
 */
export interface Size {
  approx_tokens?: number | null;
  available: boolean;
  bytes?: number | null;
  reason?: ("not_permitted" | "none" | "not_uploaded") | null;
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}/papers/{paper_id}``: one paper, sized for the caller.
 */
export interface PaperRecordView {
  fields: {
    [k: string]: unknown;
  };
  paper_id: string;
  parts: {
    [k: string]: Size;
  };
  sections?: string[] | null;
  summary?: string | null;
  summary_source?: ("generated" | "record") | null;
  title: string;
  version: string;
  view: "record" | "full";
  withheld?: string[];
  [k: string]: unknown;
}
/**
 * One summary, as the static publisher and ``rp-ui-lib`` carry it.
 */
export interface PaperSummary {
  paper_id: string;
  summary: string;
  [k: string]: unknown;
}
/**
 * One passage that answers a query, from a source this caller may read.
 */
export interface Passage {
  matched_by: ("keyword" | "semantic")[];
  offset?: number | null;
  score: number;
  section?: string | null;
  source: "full_text" | "summary" | "abstract" | "soul" | "expertise" | "cv" | "web" | "grant";
  source_id?: string | null;
  text: string;
  [k: string]: unknown;
}
/**
 * ``POST .../passages``: the best passages, and what was searched.
 */
export interface PassageList {
  k_applied: number;
  note?: string | null;
  passages: Passage[];
  search_mode_used: "hybrid" | "keyword" | "hybrid+keyword_fulltext";
  searched?: string[];
  [k: string]: unknown;
}
/**
 * Body of ``POST .../passages``.
 */
export interface PassageRequest {
  k?: number | null;
  query: string;
  [k: string]: unknown;
}
/**
 * The whole-profile display shape: metadata, both narratives, the manifest.
 *
 * Not an HTTP response model any more: ``GET /profiles/{slug}`` answers with
 * :class:`ProfileRecord`. This is the shape the static publisher writes
 * (``payloads.profile_detail_dict``), the shape ``rp-ui-lib`` renders (its
 * ``types.ts`` is generated from it), and the in-memory shape a host's
 * overlay and lens compositing work on.
 */
export interface ProfileDetail {
  content_hash?: string | null;
  expertise?: string | null;
  manifest?: {
    [k: string]: unknown;
  }[];
  metadata: ProfileMetadataPayload;
  rid?: string | null;
  slug: string;
  soul?: string | null;
  withheld?: string[];
  [k: string]: unknown;
}
/**
 * Wire shape of ``profile.jsonld``-derived metadata.
 *
 * Mirrors the on-disk :class:`~researcher_profiles.schema.ProfileDocument`
 * but uses ``extra="allow"`` so arbitrary keys round-trip cleanly between
 * server and client. This is not JSON-LD: the wire contract and
 * the on-disk format evolve independently, and a client that wants the
 * published bytes fetches ``/profiles/{slug}/profile.jsonld`` instead.
 *
 * The derived ``orcid`` field is gone: ``rid`` is the join key and
 * ``orcid_of(rid)`` is one call away.
 */
export interface ProfileMetadataPayload {
  affiliation?: string | null;
  career?: {
    [k: string]: unknown;
  }[];
  expertise?: string[];
  field?: string | null;
  interests?: string[];
  job_title?: string | null;
  level?: string;
  license?: string | null;
  methodological_commitments?: string[];
  name: string;
  not_interests?: string[];
  openalex_id?: string | null;
  provenance?: string | null;
  regulatory_experience?: string[];
  research_interests?: {
    [k: string]: unknown;
  }[];
  rid?: string | null;
  same_as?: string[];
  scholar_url?: string | null;
  section_visibility?: {
    [k: string]: unknown;
  }[];
  site_capabilities?: {
    [k: string]: unknown;
  } | null;
  subfields?: string[];
  summary?: string | null;
  therapeutic_areas?: {
    [k: string]: unknown;
  }[];
  training?: {
    [k: string]: unknown;
  }[];
  url?: string | null;
  visibility?: string;
  [k: string]: unknown;
}
/**
 * Sizes of a profile's deeper parts, for this caller.
 */
export interface ProfileParts {
  expertise: Size;
  files_withheld?: {
    [k: string]: number;
  };
  papers: Size;
  soul: Size;
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}``: one profile, sized for the caller.
 *
 * ``view="record"`` (the default) is the trimmed record: the fields an agent
 * or a list view commonly needs, long lists cut to their top entries with a
 * total, no JSON-LD plumbing, no file manifest, no narrative bodies. It stays
 * under 8 KB. ``view="full"`` carries every metadata field untrimmed, plus
 * the ``soul`` and ``expertise`` bodies, for an edit form.
 */
export interface ProfileRecord {
  content_hash?: string | null;
  expertise?: string | null;
  fields: {
    [k: string]: unknown;
  };
  parts: ProfileParts;
  rid?: string | null;
  slug: string;
  soul?: string | null;
  view: "record" | "full";
  withheld?: string[];
  [k: string]: unknown;
}
export interface ProfileSummary {
  affiliation?: string | null;
  contaminated_count?: number;
  field?: string | null;
  fulltext_pct?: number;
  level?: string;
  name: string;
  paper_count?: number;
  rid?: string | null;
  slug: string;
  summary_count?: number;
  [k: string]: unknown;
}
/**
 * One inline section's declared tier.
 *
 * Sections travel on the visibility patch rather than the metadata patch
 * because a tier is a privacy decision, not a display field: the one surface
 * that knows about host floors and the full-text lock has to be the one that
 * sets them.
 */
export interface SectionTier {
  section: string;
  visibility: string;
  [k: string]: unknown;
}
/**
 * One inline section's tiers, and who they let in: the read side of the
 * section mechanism.
 *
 * A section has both a tier the owner *declared* and a tier that actually
 * *governs* after the profile default folds in, and an editor has to show
 * both: the control sits on ``declared``, the "resolves to" badge on
 * ``effective``. The old report collapsed the two into one ``{section: tier}``
 * map, so an owner could not tell what they set from what it became.
 */
export interface SectionTierReport {
  declared: string;
  effective: string;
  section: string;
  visible_to?: string[];
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}/summaries?ids=``: several summaries in one read.
 */
export interface SummaryBatch {
  limit_applied: number;
  not_processed?: string[];
  summaries?: {
    [k: string]: string;
  };
  unavailable?: {
    [k: string]: "not_permitted" | "none" | "not_uploaded";
  };
  [k: string]: unknown;
}
/**
 * One bounded page of a long text (``.../text``).
 */
export interface TextPage {
  content_hash?: string | null;
  has_more: boolean;
  next_offset?: number | null;
  offset: number;
  returned_chars: number;
  section?: string | null;
  sections?: TextSection[];
  text: string;
  total_chars: number;
  [k: string]: unknown;
}
/**
 * One heading's span in a text: what ``section=`` accepts.
 */
export interface TextSection {
  chars: number;
  name: string;
  offset: number;
  [k: string]: unknown;
}
/**
 * The first entries of a long list, and how many there are in all.
 */
export interface Trimmed {
  top?: unknown[];
  total?: number;
  [k: string]: unknown;
}
/**
 * Set the profile-level default tier, per-artifact tiers, section tiers.
 */
export interface VisibilityPatch {
  artifacts?: ArtifactVisibility[];
  base_hash?: string | null;
  profile_visibility?: string | null;
  sections?: SectionTier[];
  [k: string]: unknown;
}
/**
 * ``GET /profiles/{slug}/visibility``: what is published, and to whom.
 */
export interface VisibilityReport {
  artifacts?: ArtifactTier[];
  counts?: {
    [k: string]: number;
  };
  profile_floor?: string | null;
  profile_floor_reason?: string | null;
  profile_visibility: string;
  rid?: string | null;
  sections?: SectionTierReport[];
  slug: string;
  [k: string]: unknown;
}
