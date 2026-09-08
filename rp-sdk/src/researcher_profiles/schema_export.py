"""Generate JSON Schema files from the package's Pydantic models.

Consumers who don't want to install this package can validate a profile
directory against the JSON Schema files checked into ``schemas/`` at the repo
root. This module (re)generates them from the authoritative Pydantic models in
:mod:`researcher_profiles.schema`.

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
    PaperEntry,
    PaperSummary,
    ProfileDetail,
    ProfileMetadataPayload,
    ProfileSummary,
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
)

# Model -> output filename. Almost every entry is an on-disk artifact schema
# that defines what a conforming profile directory must contain. The one
# exception is ``profile_export_bundle``, which is an INTERCHANGE contract: a
# payload handed to a knowledge base at export time, never a file written into
# a profile. It is published here so a non-Python consumer can validate a
# bundle without installing this package.
#
# Pydantic's ``model_json_schema()`` defaults to ``by_alias=True``, so the
# emitted schemas correctly show ``@id`` / ``@context`` / ``conformsTo`` rather
# than the Python attribute names. ``tests/test_schema.py`` asserts that,
# so a future model-config change cannot silently publish Python names into the
# contract.
_SCHEMA_MODELS = {
    "profile_jsonld": ProfileDocument,  # profile.jsonld
    "papers_jsonld": PapersDocument,  # sources/papers.jsonld
    "grants_jsonld": GrantsDocument,  # sources/grants.jsonld
    "summary_file": SummaryFile,  # sources/summaries/*.summary.md frontmatter
    # Published-standard schemas (static site artifacts). NOTE: profile.jsonld
    # and papers.jsonld are the same documents authored and published (the
    # "publishable by construction" collapse), so they have no separate
    # published schema: `profile_jsonld` / `papers_jsonld` above cover both.
    "embedding_index": EmbeddingIndex,
    "profile_list": ProfileListDocument,
    "collection": ProfileCollection,
    "topic_index": TopicIndexDocument,
    # INTERCHANGE, not an on-disk artifact: the bundle `build_export_bundle`
    # hands a knowledge base. Nothing writes this into a profile directory.
    "profile_export_bundle": ProfileExportBundle,
}


def build_schemas() -> dict[str, dict]:
    """Return ``{name: json_schema_dict}`` for every exported model."""
    return {name: model.model_json_schema() for name, model in _SCHEMA_MODELS.items()}


# ---------------------------------------------------------------------------
# Wire-contract schema (HTTP types in models/api.py, consumed by the TS viewer).
# ---------------------------------------------------------------------------

# These are the HTTP *wire* types (distinct from the on-disk artifact schemas
# above). They are the single typed contract the researcher-profiles browser
# (``rp-browser``) speaks. The TypeScript
# ``rp-browser/src/types.ts`` is generated from this bundle so it can never
# drift from the pydantic source of truth.


class _WireBundle(BaseModel):
    """Wrapper so ``model_json_schema()`` emits every wire type under ``$defs``."""

    profile_detail: ProfileDetail
    profile_metadata: ProfileMetadataPayload
    profile_summary: ProfileSummary
    paper_entry: PaperEntry
    paper_summary: PaperSummary
    # The privacy-tier surface. Without these the frontend has no typed
    # visibility bodies and would hand-write the shapes it renders, a second
    # definition of the contract and the one place a drift would be invisible.
    visibility_report: VisibilityReport
    artifact_tier: ArtifactTier
    visibility_patch: VisibilityPatch
    artifact_visibility: ArtifactVisibility


def build_wire_schema() -> dict:
    """Return one combined JSON Schema document for the HTTP wire types.

    Every wire model appears under ``$defs`` (``ProfileDetail``,
    ``ProfileMetadataPayload``, ``ProfileSummary``, ``PaperEntry``,
    ``PaperSummary``, ``VisibilityReport``, ``ArtifactTier``,
    ``VisibilityPatch``, ``ArtifactVisibility``), so a single
    ``json-schema-to-typescript`` pass emits all interfaces.
    """
    schema = _WireBundle.model_json_schema()
    # Keep the wrapper's properties so every wire model under ``$defs`` is
    # referenced; ``json-schema-to-typescript`` then emits each as a named
    # top-level interface (ProfileDetail, PaperEntry, ...). The wrapper
    # interface itself is a harmless by-product.
    # Strip per-property ``title`` keys inside each $def so the generator emits
    # inline property types instead of a named alias per field (cleaner output);
    # the $def-level titles are what name the interfaces, so those stay.
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
