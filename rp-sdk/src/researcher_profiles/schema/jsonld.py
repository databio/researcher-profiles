"""JSON-LD primitives: the context IRI, canonical serialization, model base.

This module is the single definition site for everything the on-disk JSON-LD
format needs that is not a domain model:

- :data:`CONTEXT_URL`: the hosted, versioned ``@context`` IRI.
- :data:`PROFILE_FORMAT_IRI`: the ``conformsTo`` value (the format gate).
- :data:`KEY_ORDER` / :data:`PAPER_KEY_ORDER`: canonical key ordering.
- :func:`canonical_dumps`: the only writer of ``.jsonld`` bytes in this package.
- :class:`JsonLdModel`: the shared Pydantic base for every JSON-LD document.

Nothing here performs I/O against the network.
"""

import json
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer


def context_document_text() -> str | None:
    """Return the bundled JSON-LD ``@context`` document text, or ``None``.

    The single canonical copy lives inside the package at
    ``researcher_profiles/context/v1.jsonld``: package data in every install
    layout (wheel, sdist, editable), so a pure ``pip install`` can self-host
    the context when publishing a site.
    """
    try:
        res = resources.files("researcher_profiles").joinpath("context").joinpath("v1.jsonld")
        if res.is_file():
            return res.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError, AttributeError):
        pass
    return None


#: The canonical, hosted, versioned JSON-LD context IRI.
#:
#: Published documents reference it as their ``@context``, and the vocabulary
#: namespace is this IRI plus ``#`` (so ``rp:rid`` expands to
#: ``…/context/v1.jsonld#rid``). The bytes are served from a domain the format's
#: maintainers control; the canonical copy in this repo is
#: ``context/v1.jsonld``.
#:
#: The runtime never fetches this. Loading and validating a profile is a pure
#: local operation against the Pydantic models in ``schema/``; this IRI is an
#: identifier and a documentation pointer, not a runtime dependency. No module
#: on the profile load path may import ``httpx``/``urllib`` to dereference it
#: (``tests/test_guardrails.py`` asserts this).
#:
#: ``profiles.databio.org`` is a domain we control, chosen over an unregistered
#: ``w3id.org`` path (which 404s until a redirect PR is merged, so it is not a
#: legitimate identifier yet). A w3id redirect to this URL may be added later as
#: a vendor-neutral permanent identifier; that is a superset, not a blocker.
#:
#: See ``researcher_profiles/context/README.md`` for the hosting arrangement
#: and freeze policy.
CONTEXT_URL = "https://profiles.databio.org/context/v1.jsonld"

#: The value every conforming published document carries in ``conformsTo``.
#:
#: The same IRI as :data:`CONTEXT_URL` today, but a DISTINCT constant: the
#: vocabulary and the document profile are different things and may diverge at
#: v2 (e.g. a v2 format that still references the v1 vocabulary). This replaces
#: the integer ``schema_version: 2`` gate. An integer counter with no external
#: meaning cannot serve a published standard.
PROFILE_FORMAT_IRI = "https://profiles.databio.org/context/v1.jsonld"

#: Canonical top-level key order for ``profile.jsonld``. Keys not listed sort
#: alphabetically after these.
KEY_ORDER: tuple[str, ...] = (
    "@context",
    "@id",
    "@type",
    "conformsTo",
    "name",
    "rid",
    "provenance",
    "verifiedAt",
    "proof",
    "license",
    "url",
    "dateModified",
    "visibility",
    "hasCitationGraph",
    "hasEmbeddingIndex",
    "expertiseCitesPaperIds",
    "level",
    "affiliation",
    "jobTitle",
    "email",
    "field",
    "subfields",
    "summary",
    "sameAs",
    "identifier",
    "about",
    "training",
    "career",
    "expertise",
    "interests",
    "not_interests",
    "methodological_commitments",
    "recurring_positions",
    "intellectual_lineage",
    "critiques",
    "researchOutputs",
    "collaborators",
    "anchor",
    "paper_stats",
    "hasPart",
    "subjectOf",
)

#: Canonical key order inside one ``ScholarlyArticle`` node.
PAPER_KEY_ORDER: tuple[str, ...] = (
    "paper_id",
    "doi",
    "pmid",
    "pmcid",
    "openalex_id",
    "datePublished",
    "isPartOf",
    "venue",
    "type",
    "author",
    "first_author",
    "last_author",
    "author_position",
    "author_index",
    "total_authors",
    "is_corresponding",
    "citation",
    "cited_by_count",
    "abstract",
    "open_access",
    "is_oa",
    "oa_status",
    "oa_url",
    "pdf_url",
    "full_text_link",
    "access",
    "source",
)

#: Canonical key order inside one ``MonetaryGrant`` node.
GRANT_KEY_ORDER: tuple[str, ...] = (
    "id",
    "funder",
    "role",
    "status",
    "start",
    "end",
    "abstract",
)

#: Canonical key order inside one manifest entry.
PART_KEY_ORDER: tuple[str, ...] = (
    "role",
    "paperId",
    "encodingFormat",
    "contentUrl",
    "visibility",
    "derivedFrom",
    "bytes",
    "sha256",
)

# One merged rank map. ``canonical_dumps`` walks untyped dicts, so a single
# ordering table is what it can actually apply; the per-document tuples above
# remain the documented contract for each artifact.
_RANK: dict[str, int] = {
    key: i
    for i, key in enumerate(
        dict.fromkeys(KEY_ORDER + PAPER_KEY_ORDER + GRANT_KEY_ORDER + PART_KEY_ORDER)
    )
}
_UNRANKED = len(_RANK)


def _sort_key(key: str) -> tuple[int, str]:
    return (_RANK.get(key, _UNRANKED), key)


def canonicalize(obj: Any) -> Any:
    """Recursively reorder every dict in ``obj`` into canonical key order.

    Known keys come first in :data:`KEY_ORDER` (then paper / grant / part
    order); everything else follows alphabetically. List order is preserved.
    A papers list is meaningful and is never re-sorted here.
    """
    if isinstance(obj, dict):
        return {k: canonicalize(obj[k]) for k in sorted(obj, key=_sort_key)}
    if isinstance(obj, (list, tuple)):
        return [canonicalize(v) for v in obj]
    return obj


def canonical_dumps(obj: Any) -> str:
    """Serialize ``obj`` as canonical JSON-LD text.

    Stable key order + fixed 2-space indentation + no ASCII escaping + a
    trailing newline, so a version-controlled profile diffs cleanly and
    re-serializing an unchanged document is a byte-level no-op.

    This is the only writer of ``.jsonld`` bytes in the codebase. Callers
    outside this package import it rather than reimplementing the ordering.
    """
    return json.dumps(canonicalize(obj), indent=2, ensure_ascii=False) + "\n"


def read_jsonld(path: str | Path) -> Any:
    """Parse a ``.jsonld`` file. Raises :class:`json.JSONDecodeError` / OSError."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonld(path: str | Path, model: Any) -> Path:
    """Write ``model`` (a :class:`JsonLdModel` or a plain dict) to ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    p.write_text(canonical_dumps(data), encoding="utf-8")
    return p


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty-collection values from a serialized node.

    A JSON-LD node states what is known. ``"journal": null`` is not a weaker
    claim than an absent key, and ``"critiques": []`` is not a weaker claim
    than no ``critiques``. Both are noise that bloats every published document
    and every diff. Consumers must treat an absent key and an empty one
    identically (see the baseline-required / extras-allowed posture).
    """
    return {
        k: v
        for k, v in data.items()
        if v is not None and not (isinstance(v, (list, dict)) and len(v) == 0)
    }


class JsonLdModel(BaseModel):
    """Shared base for every model that serializes to a JSON-LD node object.

    ``extra="allow"`` because conformance means the
    baseline fields are present and well-formed, and additional keys are
    permitted and preserved. Published documents keep
    ``additionalProperties: true``; consumers must ignore unknown keys rather
    than treat them as validation failures.

    Only the ``@``-keywords and the handful of camelCase JSON-LD terms carry
    aliases. Every other field has one name in both worlds, so
    ``serialize_by_alias`` cannot reshuffle unrelated fields.

    Pruning (:func:`_prune`) is a property of every JSON-LD node in this
    format, not of any one document, so it lives here and every subclass gets
    it. A subclass that needs to reshape a node overrides :meth:`_jsonld_node`
    and never the serializer: that keeps the prune step last and, with it,
    key order fixed. A ``super()``-calling override would prune before the
    subclass re-assigned a key, moving that key to the end of the object,
    invisible on disk (``canonical_dumps`` re-sorts) but visible in
    ``model_dump_json()``, which is the API surface.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        serialize_by_alias=True,
        str_strip_whitespace=True,
    )

    context: str | None = Field(default=None, alias="@context")
    id_: str | None = Field(default=None, alias="@id")
    type_: str | None = Field(default=None, alias="@type")

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        """Subclass hook: adjust the raw node before pruning. Base is identity."""
        return data

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> dict[str, Any]:
        return _prune(self._jsonld_node(handler(self)))


__all__ = [
    "CONTEXT_URL",
    "GRANT_KEY_ORDER",
    "JsonLdModel",
    "KEY_ORDER",
    "PAPER_KEY_ORDER",
    "PART_KEY_ORDER",
    "PROFILE_FORMAT_IRI",
    "canonical_dumps",
    "canonicalize",
    "read_jsonld",
    "write_jsonld",
]
