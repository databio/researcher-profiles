"""``prof.edit``: the owner edit surface and the policy behind it.

An owner may edit every content field of the profile document, including
every field the build AI writes, so they can correct what a model guessed
without a rebuild. Only :data:`LOCKED_METADATA_FIELDS` is off limits.
``personality/expertise.md`` cites paper ids, so no edit route reaches it.

Every mutation applies the patch to a copy and round-trips it through
:class:`~researcher_profiles.schema.ProfileDocument` before anything is
persisted, so a rejected patch never reaches the store.

A bad patch is the caller's fault and becomes an :class:`EditError` (HTTP
400); a failing pre-commit hook is the server's fault and propagates as
:class:`~researcher_profiles.errors.WriteHookError` (HTTP 500). That is why
the methods catch :class:`~researcher_profiles.errors.ProfileWriteError`
only, never bare ``Exception``.
"""

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ..errors import ProfileError, ProfileWriteError
from ..schema import (
    ArtifactRef,
    CareerEntry,
    CareerStage,
    ConceptReference,
    InterestConcept,
    PaperRecord,
    ProfileDocument,
    ResearchInterest,
    ResearchOutput,
    SectionVisibility,
    SiteCapabilities,
    Training,
    effective_interests,
    interests_from_text,
)
from ..schema._common import RENAMED_TIERS
from ..schema.jsonld import canonical_dumps

if TYPE_CHECKING:  # pragma: no cover
    from . import ResearcherProfile


class EditError(ProfileError):
    """A requested edit is not allowed or would produce an invalid profile."""


class WorkNotFoundError(EditError):
    """No work in this profile carries the requested ``paper_id``.

    A subclass so the edit routes can answer 404 here and 400 for every other
    bad work edit.
    """


#: The fields no owner edit may touch.
LOCKED_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        # identity and proof: who the profile is about
        "rid",
        "id_",
        "provenance",
        "provenance_note",
        "proof",
        "verified_at",
        "identifier",
        # computed by code from the corpus, never asserted
        "paper_stats",
        "anchor",
        "level",
        "has_citation_graph",
        "has_embedding_index",
        "expertise_cites_paper_ids",
        "synthesis_inputs_digest",
        # the file manifest
        "has_part",
        "subject_of",
        # format markers and bookkeeping
        "context",
        "type_",
        "conforms_to",
        "url",
        "date_modified",
        # visibility has its own route
        "visibility",
        "section_visibility",
    }
)
assert LOCKED_METADATA_FIELDS <= set(ProfileDocument.model_fields), (
    "locked fields missing from ProfileDocument: "
    f"{sorted(LOCKED_METADATA_FIELDS - set(ProfileDocument.model_fields))}"
)

#: The metadata fields an owner may patch through the interactive edit
#: surface: every ``ProfileDocument`` field that is not locked.
EDITABLE_METADATA_FIELDS: frozenset[str] = (
    frozenset(ProfileDocument.model_fields) - LOCKED_METADATA_FIELDS
)

#: The fields of one work an owner may patch: bibliographic facts. Not
#: ``paper_id`` (the selector) or the counts the corpus build derives.
EDITABLE_WORK_FIELDS: frozenset[str] = frozenset(
    {
        "doi",
        "openalex_id",
        "name",
        "datePublished",
        "type",
        "citation",
        "full_text_link",
        "access",
        "summary",
        "first_author",
        "author_position",
        "is_corresponding",
    }
)

#: Editable list fields whose items are objects. A patch delivers plain dicts
#: and ``model_copy`` does not validate, so they are parsed here, early enough
#: that a failure is a 400 naming the offending entry.
STRUCTURED_METADATA_FIELDS: dict[str, type[BaseModel]] = {
    "training": Training,
    "career": CareerEntry,
    "research_interests": ResearchInterest,
    "therapeutic_areas": ConceptReference,
    "research_outputs": ResearchOutput,
}

#: Editable fields holding a single object rather than a list of them.
_STRUCTURED_SCALAR_FIELDS: dict[str, type[BaseModel]] = {
    "site_capabilities": SiteCapabilities,
    "career_stage": CareerStage,
}


def _merge_object_fields(doc: ProfileDocument, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial object into the stored one, for single-object fields.

    ``{"career_stage": {"current_rank": "professor"}}`` changes that one fact
    and keeps the rest, so an owner never has to resend the whole block. A key
    sent as ``null`` clears that key; the field sent as ``null`` clears it all.
    """
    out = dict(patch)
    for key in _STRUCTURED_SCALAR_FIELDS:
        raw = out.get(key)
        current = getattr(doc, key, None)
        if isinstance(raw, dict) and current is not None:
            out[key] = {**current.model_dump(mode="json"), **raw}
    return out


def _coerce_structured(patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(patch)
    for key, model in STRUCTURED_METADATA_FIELDS.items():
        if key not in out:
            continue
        raw = out[key]
        if not isinstance(raw, list):
            raise EditError(f"{key} must be a list of objects, got {type(raw).__name__}")
        parsed = []
        for i, item in enumerate(raw):
            if isinstance(item, model):
                parsed.append(item)
                continue
            try:
                parsed.append(model.model_validate(item))
            except ValidationError as e:
                raise EditError(f"{key}[{i}] is not a valid {model.__name__}: {e}") from e
        out[key] = parsed
    for key, model in _STRUCTURED_SCALAR_FIELDS.items():
        if key not in out or out[key] is None or isinstance(out[key], model):
            continue
        try:
            out[key] = model.model_validate(out[key])
        except ValidationError as e:
            raise EditError(f"{key} is not a valid {model.__name__}: {e}") from e
    return out


def _declare_text_interests(doc: ProfileDocument, patch: dict[str, Any]) -> dict[str, Any]:
    """Turn a patch of the plain interest lists into declared typed entries.

    The plain lists are a projection of ``research_interests``, so a patch is
    recorded there. For each patched list
    (``interests`` is +0.5, ``not_interests`` is -0.5):

    - a label that names a coded concept already projected into the same list
      is left alone (it is already there, at whatever weight it has);
    - a label that names a coded concept projected into the other list gets a
      declared entry on that concept with the new sign;
    - any other label becomes a declared text-only entry;
    - a coded concept that was in the patched list but whose label is gone gets
      a declared entry with no weight, which outranks the entry that put it
      there and drops it from both lists.

    Earlier text-only entries in a patched list are replaced: the patch is the
    owner's full statement of that list.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = list(patch.get("research_interests", doc.research_interests))
    if not base:
        # No typed interests yet: convert the plain lists so the list this
        # patch does not touch survives.
        base = interests_from_text(
            doc.interests, doc.not_interests, generator="llm", method="inferred", asserted_at=now
        )
    effective = effective_interests(base)
    coded = {
        e.concept.text.casefold(): e
        for e in effective
        if not e.concept.unmapped and e.weight is not None and e.weight != 0
    }
    added: list[ResearchInterest] = []
    seen: set[str] = set()

    def declare(concept: InterestConcept, weight: float | None) -> None:
        added.append(
            ResearchInterest(
                concept=concept, weight=weight, method="declared", generator="user", assertedAt=now
            )
        )

    lists = {
        field: [str(x).strip() for x in patch.pop(field) or [] if str(x).strip()]
        for field in ("interests", "not_interests")
        if field in patch
    }
    mentioned = {label.casefold() for labels in lists.values() for label in labels}
    patched_signs = {1 if field == "interests" else -1 for field in lists}
    # Text-only entries that project into a patched list are replaced; the
    # rest (the other list, unknown, neutral) and every coded entry stay.
    kept = [
        e
        for e in base
        if not e.concept.unmapped
        or e.weight is None
        or e.weight == 0
        or (1 if e.weight > 0 else -1) not in patched_signs
    ]
    for field, labels in lists.items():
        sign = 1 if field == "interests" else -1
        for label in labels:
            k = label.casefold()
            if k in seen:
                continue
            seen.add(k)
            hit = coded.get(k)
            if hit is None:
                declare(InterestConcept(label=label, unmapped=True), sign * 0.5)
            elif (hit.weight or 0) * sign < 0:
                declare(hit.concept, sign * 0.5)
        for k, e in coded.items():
            if (e.weight or 0) * sign > 0 and k not in mentioned:
                declare(e.concept, None)
    return {**patch, "research_interests": kept + added}


def paper_version(record: PaperRecord) -> str:
    """First 16 hex of sha256 over the canonical JSON of one work (64 bits).

    The concurrency token for one work. A work edit does not move the
    profile's ``content_hash``, so without this two editors fixing the same
    paper overwrite each other silently.
    """
    data = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    return hashlib.sha256(canonical_dumps(data).encode("utf-8")).hexdigest()[:16]


def _find_work(papers: list[PaperRecord], paper_id: str) -> int:
    """The position of ``paper_id`` in the corpus, or a :class:`WorkNotFoundError`."""
    for i, record in enumerate(papers):
        if record.paper_id == paper_id:
            return i
    raise WorkNotFoundError(f"no work with paper_id {paper_id!r} in this profile")


def select_parts(entry: dict[str, Any], parts: list[ArtifactRef]) -> list[ArtifactRef]:
    """Every manifest part a visibility selector addresses, in manifest order.

    One selector per entry, in precedence order ``content_url`` -> ``paper_id``
    -> ``role``. A ``role`` selector matches every part with that role, not
    the first hit. Shared by the edit path and a host's patch layer so the
    two cannot disagree.
    """
    out: list[ArtifactRef] = []
    for p in parts:
        if entry.get("content_url"):
            if p.content_url == entry["content_url"]:
                out.append(p)
        elif entry.get("paper_id"):
            if p.paper_id == entry["paper_id"]:
                out.append(p)
        elif entry.get("role"):
            if p.role == entry["role"]:
                out.append(p)
    return out


class EditManager:
    """Owner-edit policy and mutation surface for one profile."""

    def __init__(self, profile: "ResearcherProfile") -> None:
        self._profile = profile

    def patch_metadata(self, patch: dict[str, Any]) -> ProfileDocument:
        """Patch owner-editable metadata fields and persist the document."""
        if not isinstance(patch, dict):
            raise EditError("metadata patch must be an object")
        disallowed = set(patch) - EDITABLE_METADATA_FIELDS
        if disallowed:
            raise EditError(
                "these fields are not owner-editable: "
                + ", ".join(sorted(disallowed))
                + f" (editable: {', '.join(sorted(EDITABLE_METADATA_FIELDS))})"
            )
        if not patch:
            return self._profile.metadata
        coerced = _coerce_structured(_merge_object_fields(self._profile.metadata, patch))
        if "interests" in coerced or "not_interests" in coerced:
            coerced = _declare_text_interests(self._profile.metadata, coerced)
        updated = self._profile.metadata.model_copy(update=coerced)
        try:
            # ``model_copy`` does not validate; re-validating runs the interest
            # projection so the persisted plain lists match the typed entries.
            updated = ProfileDocument.model_validate(updated.model_dump(mode="json"))
        except ValidationError as e:
            raise EditError(f"patched profile is invalid: {e}") from e
        try:
            return self._profile.save_profile(updated)
        except ProfileWriteError as e:
            raise EditError(f"patched profile is invalid: {e}") from e

    def set_soul(self, soul: str) -> None:
        """Replace the free-form SOUL/persona narrative."""
        if not isinstance(soul, str):
            raise EditError("soul must be a string")
        try:
            self._profile.save_soul(soul)
        except ProfileWriteError as e:
            raise EditError(f"soul could not be saved: {e}") from e

    def patch_work(self, paper_id: str, patch: dict[str, Any]) -> PaperRecord:
        """Patch one record in ``sources/papers.jsonld`` and persist the corpus.

        Only fields in :data:`EDITABLE_WORK_FIELDS` are accepted. A bad value
        raises :class:`EditError` before anything is written. Corpus order is
        preserved.
        """
        if not isinstance(patch, dict):
            raise EditError("work patch must be an object")
        disallowed = set(patch) - EDITABLE_WORK_FIELDS
        if disallowed:
            raise EditError(
                "these work fields are not owner-editable: "
                + ", ".join(sorted(disallowed))
                + f" (editable: {', '.join(sorted(EDITABLE_WORK_FIELDS))})"
            )
        papers = list(self._profile.papers)
        index = _find_work(papers, paper_id)
        if not patch:
            return papers[index]
        # The patch uses on-disk names, and ``model_copy`` does not validate.
        merged = {**papers[index].model_dump(by_alias=True), **patch}
        try:
            updated = PaperRecord.model_validate(merged)
        except ValidationError as e:
            raise EditError(f"patched work {paper_id!r} is invalid: {e}") from e
        papers[index] = updated
        self._save_works(papers)
        return updated

    def add_work(self, record: PaperRecord | dict[str, Any]) -> PaperRecord:
        """Add one work, or replace the one already carrying its ``paper_id``."""
        if isinstance(record, PaperRecord):
            parsed = record
        else:
            try:
                parsed = PaperRecord.model_validate(record)
            except ValidationError as e:
                raise EditError(f"work record is invalid: {e}") from e
        if not parsed.paper_id:
            raise EditError("a work record needs a paper_id")
        papers = list(self._profile.papers)
        for i, existing in enumerate(papers):
            if existing.paper_id == parsed.paper_id:
                papers[i] = parsed
                break
        else:
            papers.append(parsed)
        self._save_works(papers)
        return parsed

    def remove_work(self, paper_id: str) -> None:
        """Remove one work from ``sources/papers.jsonld``."""
        papers = list(self._profile.papers)
        del papers[_find_work(papers, paper_id)]
        self._save_works(papers)

    def _save_works(self, papers: list[PaperRecord]) -> None:
        """Persist the corpus, then restamp the manifest entry describing it.

        Otherwise the manifest's ``bytes``/``sha256`` would describe the
        pre-edit file.
        """
        try:
            self._profile.save_papers(papers)
            self._profile.build_manifest(write=True)
        except ProfileWriteError as e:
            raise EditError(f"works could not be saved: {e}") from e

    def set_visibility(
        self,
        *,
        profile_visibility: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        sections: list[dict[str, Any]] | None = None,
    ) -> tuple[ProfileDocument, int]:
        """Set the profile-level, per-artifact, and per-section privacy tiers.

        Sections are the inline fields of the document. Tiers are set only
        here, never through the metadata patch, so there is one privacy
        implementation.
        """
        valid_tiers = {"public", "limited", "private"}

        def _check_tier(tier: Any, where: str) -> None:
            if tier in RENAMED_TIERS:
                raise EditError(
                    f"visibility {tier!r}{where} was renamed to {RENAMED_TIERS[tier]!r} (rp spec 2026-10)"
                )
            if tier not in valid_tiers:
                raise EditError(
                    f"invalid visibility {tier!r}{where} (one of {sorted(valid_tiers)})"
                )

        doc = self._profile.metadata
        update: dict[str, Any] = {}
        changed = 0
        # Manifest copies, built on first use by an artifact or `soul` entry.
        new_has_part: list[ArtifactRef] | None = None
        new_subject_of: list[ArtifactRef] | None = None

        def _retier(entry: dict[str, Any], tier: str) -> None:
            nonlocal changed, new_has_part, new_subject_of
            if new_has_part is None:
                new_has_part = [p.model_copy() for p in doc.has_part]
                new_subject_of = [p.model_copy() for p in doc.subject_of]
            targets = select_parts(entry, new_has_part) + select_parts(entry, new_subject_of)
            if not targets:
                raise EditError(f"no manifest artifact matches {entry!r}")
            for target in targets:
                if target.visibility != tier:
                    changed += 1
                target.visibility = tier  # type: ignore[assignment]

        if profile_visibility is not None:
            _check_tier(profile_visibility, "")
            update["visibility"] = profile_visibility
        if artifacts:
            for entry in artifacts:
                tier = entry.get("visibility")
                _check_tier(tier, f" for artifact {entry!r}")
                _retier(entry, tier)
        if sections:
            declared = {x.section: x.visibility for x in doc.section_visibility}
            for entry in sections:
                tier = entry.get("visibility")
                _check_tier(tier, f" for section {entry!r}")
                try:
                    parsed = SectionVisibility.model_validate(entry)
                except ValidationError as e:
                    raise EditError(f"{entry!r} is not a known section: {e}") from e
                if declared.get(parsed.section) != parsed.visibility:
                    changed += 1
                declared[parsed.section] = parsed.visibility
                # SOUL is an artifact, so the section tier must reach the
                # manifest: re-tier every `soul` part to match.
                if parsed.section == "soul":
                    _retier({"role": "soul", "visibility": parsed.visibility}, parsed.visibility)
            update["section_visibility"] = [
                SectionVisibility(section=s, visibility=v) for s, v in sorted(declared.items())
            ]
        if new_has_part is not None:
            update["has_part"] = new_has_part
            update["subject_of"] = new_subject_of
        if not update:
            return doc, 0
        updated = doc.model_copy(update=update)
        try:
            return self._profile.save_profile(updated), changed
        except ProfileWriteError as e:
            raise EditError(f"visibility change produced an invalid profile: {e}") from e


__all__ = [
    "EditError",
    "EDITABLE_METADATA_FIELDS",
    "EDITABLE_WORK_FIELDS",
    "LOCKED_METADATA_FIELDS",
    "STRUCTURED_METADATA_FIELDS",
    "WorkNotFoundError",
    "paper_version",
    "select_parts",
    "EditManager",
]
