/**
 * Wire contract for the researcher-profiles HTTP API.
 *
 * GENERATED FILE: do not edit by hand.
 * Regenerate with `npm run gen:types` (source: src/researcher_profiles/api_models.py).
 */

/**
 * HTTP wire types for the researcher-profiles API (researcher_profiles.api_models). Generated; do not edit by hand.
 */
export interface ResearcherProfileWireContract {
  artifact_tier: ArtifactTier;
  artifact_visibility: ArtifactVisibility;
  paper_entry: PaperEntry;
  paper_summary: PaperSummary;
  profile_detail: ProfileDetail;
  profile_metadata: ProfileMetadataPayload;
  profile_summary: ProfileSummary;
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
  lock_reason?: string | null;
  locked?: boolean;
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
export interface PaperSummary {
  paper_id: string;
  summary: string;
  [k: string]: unknown;
}
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
  name: string;
  not_interests?: string[];
  openalex_id?: string | null;
  provenance?: string | null;
  rid?: string | null;
  same_as?: string[];
  scholar_url?: string | null;
  subfields?: string[];
  summary?: string | null;
  training?: {
    [k: string]: unknown;
  }[];
  url?: string | null;
  visibility?: string;
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
 * Set the profile-level default tier and/or per-artifact tiers.
 */
export interface VisibilityPatch {
  artifacts?: ArtifactVisibility[];
  base_hash?: string | null;
  profile_visibility?: string | null;
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
  slug: string;
  [k: string]: unknown;
}
