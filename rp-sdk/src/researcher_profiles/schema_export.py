"""Generate JSON Schema files from the package's Pydantic models.

Consumers who don't want to install this package can validate a profile
directory against the JSON Schema files checked into ``schemas/`` at the repo
root.

Run via the CLI::

    rp schema export schemas/

or programmatically::

    from researcher_profiles.schema_export import export_schemas
    export_schemas("schemas")
"""

import json
from pathlib import Path

from pydantic import BaseModel

from .models.api import (
    ArtifactTier,
    ArtifactVisibility,
    FileList,
    PaperEntry,
    PaperPage,
    PaperRecordView,
    PaperRow,
    PaperSummary,
    Passage,
    PassageList,
    PassageRequest,
    ProfileDetail,
    ProfileMetadataPayload,
    ProfileParts,
    ProfileRecord,
    ProfileSummary,
    SectionTier,
    SectionTierReport,
    Size,
    SummaryBatch,
    TextPage,
    TextSection,
    Trimmed,
    VisibilityPatch,
    VisibilityReport,
)
from .models.published import (
    EmbeddingIndex,
    ProfileCollection,
    ProfileListDocument,
    TopicIndexDocument,
)
from .profile.export import ProfileExportBundle
from .schema import (
    GrantsDocument,
    PapersDocument,
    ProfileDocument,
    SummaryFile,
    TrialsDocument,
)

# Model -> output filename. Every entry but ``profile_export_bundle`` is an
# on-disk artifact schema. Schemas use field aliases (``@id``, ``conformsTo``),
# not Python attribute names.
_SCHEMA_MODELS = {
    "profile_jsonld": ProfileDocument,  # profile.jsonld
    "papers_jsonld": PapersDocument,  # sources/papers.jsonld
    "grants_jsonld": GrantsDocument,  # sources/grants.jsonld
    "trials_jsonld": TrialsDocument,  # sources/trials.jsonld (optional, clinical)
    "summary_file": SummaryFile,  # sources/summaries/*.summary.md frontmatter
    # Published static-site artifacts. profile.jsonld and papers.jsonld are
    # the same documents authored and published, so the entries above cover both.
    "embedding_index": EmbeddingIndex,
    "profile_list": ProfileListDocument,
    "collection": ProfileCollection,
    "topic_index": TopicIndexDocument,
    # Interchange: the bundle `build_export_bundle` hands a knowledge base,
    # published so non-Python consumers can validate it. Never written to disk.
    "profile_export_bundle": ProfileExportBundle,
}


def build_schemas() -> dict[str, dict]:
    """Return ``{name: json_schema_dict}`` for every exported model."""
    return {name: model.model_json_schema() for name, model in _SCHEMA_MODELS.items()}


# ---------------------------------------------------------------------------
# Wire-contract schema (HTTP types in models/api.py, consumed by the TS viewer).
# ---------------------------------------------------------------------------

# ``rp-ui-lib/src/types.ts`` is generated from this bundle.


class _WireBundle(BaseModel):
    """Wrapper so ``model_json_schema()`` emits every wire type under ``$defs``."""

    # Display shapes: what the static publisher writes and rp-ui-lib renders.
    profile_detail: ProfileDetail
    profile_metadata: ProfileMetadataPayload
    profile_summary: ProfileSummary
    paper_entry: PaperEntry
    paper_summary: PaperSummary
    # The sized HTTP reads: ``GET /profiles/{slug}`` and its sub-routes.
    profile_record: ProfileRecord
    profile_parts: ProfileParts
    size: Size
    trimmed: Trimmed
    paper_page: PaperPage
    paper_row: PaperRow
    paper_record_view: PaperRecordView
    summary_batch: SummaryBatch
    file_list: FileList
    text_page: TextPage
    text_section: TextSection
    passage_request: PassageRequest
    passage: Passage
    passage_list: PassageList
    # The privacy-tier surface.
    visibility_report: VisibilityReport
    artifact_tier: ArtifactTier
    visibility_patch: VisibilityPatch
    artifact_visibility: ArtifactVisibility
    section_tier: SectionTier
    section_tier_report: SectionTierReport


def build_wire_schema() -> dict:
    """Return one combined JSON Schema document for the HTTP wire types.

    Every wire model appears under ``$defs``, so a single
    ``json-schema-to-typescript`` pass emits all interfaces.
    """
    schema = _WireBundle.model_json_schema()
    # Drop per-property titles so the generator inlines property types instead
    # of naming an alias per field. The $def titles name the interfaces.
    for defn in schema.get("$defs", {}).values():
        for prop in defn.get("properties", {}).values():
            prop.pop("title", None)
    schema["title"] = "ResearcherProfileWireContract"
    schema["description"] = (
        "HTTP wire types for the researcher-profiles API "
        "(researcher_profiles.models.api). Generated; do not edit by hand."
    )
    return schema


def export_wire_schema(out_file: str | Path) -> Path:
    """Write the combined wire-contract JSON Schema to ``out_file``.

    Creates the parent directory if needed. Returns the written path.
    """
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_wire_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def export_schemas(out_dir: str | Path) -> list[Path]:
    """Write one ``<name>.schema.json`` file per model into ``out_dir``.

    Returns the list of written paths. Creates ``out_dir`` if needed.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in build_schemas().items():
        path = out / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


__all__ = [
    "build_schemas",
    "export_schemas",
    "build_wire_schema",
    "export_wire_schema",
]
