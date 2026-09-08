"""``prof.edit``: the owner edit surface and the policy behind it.

The push primitive (``PUT /profiles/{slug}``) replaces a whole profile directory
from a tarball, the right shape for a build tool, the wrong shape for a
human tweaking their name in a form. The *interactive* edit surface is
``prof.edit.patch_metadata`` / ``prof.edit.set_soul`` /
``prof.edit.set_visibility``, exposed by :class:`EditManager`. The manager
coordinates those related owner mutations; persistence still goes through the
profile's ``ArtifactStorage``.

What stays policy and shared: the editable field sets, the structured-field
coercion, :class:`EditError`, and :func:`select_parts` (the one visibility
selector, used by both the canonical edit path and a host's patch layer, so
the two cannot disagree about what a patch addresses).

What an owner may edit is narrow. Display metadata (name, affiliation, job
title, field and subfields, summary, expertise labels, interests, training and
career history, links), the SOUL/persona narrative, and per-artifact
visibility. Not the paper corpus, summaries, or embeddings. Those are generated
from the corpus, and an owner editing them would desynchronize the record from
what generated it. Not ``personality/expertise.md`` either: that narrative is
synthesized from the corpus and cites paper ids, so no edit route reaches it
even though ``save_expertise`` exists.

``training`` and ``career`` are authored history. No build tool
supplies them, and a brand-new self-published profile has neither. They arrive
here as plain dicts and are validated by the same round-trip through
:class:`~researcher_profiles.schema.ProfileDocument` as every other field, so a
malformed entry is a 400 and nothing is persisted.

Every mutation:

1. loads the current document,
2. applies the patch to a copy,
3. re-validates by round-tripping the copy through
   :class:`~researcher_profiles.schema.ProfileDocument` (so provenance
   invariants, rid rules, and schema constraints are enforced exactly as on
   load), and only then
4. persists the canonical bytes.

Steps 3 and 4 happen inside ``save_profile``, which validates before it calls
its storage backend, so a rejected patch never reaches the store and
raises :class:`EditError`.

One distinction that must not be blurred: a bad patch is the caller's fault and
becomes an :class:`EditError` (HTTP 400); a failing pre-commit hook is the
server's fault and propagates as
:class:`~researcher_profiles.errors.WriteHookError` (HTTP 500). That is why the
methods catch :class:`~researcher_profiles.errors.ProfileWriteError` only, and
never bare ``Exception``.
"""

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ..errors import ProfileError, ProfileWriteError
from ..privacy import FULLTEXT_LOCK_REASON
from ..schema import (
    ALWAYS_RESTRICTED_ROLES,
    ArtifactRef,
    CareerEntry,
    ProfileDocument,
    Training,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import ResearcherProfile


class EditError(ProfileError):
    """A requested edit is not allowed or would produce an invalid profile."""


#: The metadata fields an owner may patch through the interactive edit surface.
#: These are display/persona fields. Identity (``rid``), provenance, the
#: manifest, and every pipeline-derived field are excluded. An
#: owner renaming themselves is fine; an owner rewriting ``rid`` or
#: ``paper_stats`` is not.
EDITABLE_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "affiliation",
        "job_title",
        "field",
        "subfields",
        "summary",
        "expertise",
        "interests",
        "not_interests",
        "training",
        "career",
        "same_as",
    }
)


#: Editable fields whose values are objects, not scalars or string lists.
#: A patch delivers them as plain dicts (that is what JSON is), and
#: ``model_copy`` does not validate, so they are parsed into their schema models
#: here. This is not a second set of rules. It is the same
#: :class:`~researcher_profiles.schema.Training` /
#: :class:`~researcher_profiles.schema.CareerEntry` the document round-trip
#: would apply, only applied early enough that the copied document is
#: well-typed and the failure is a 400 naming the offending entry rather than a
#: serializer warning about a dict where a model was expected.
STRUCTURED_METADATA_FIELDS: dict[str, type[BaseModel]] = {
    "training": Training,
    "career": CareerEntry,
}


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
    return out


def select_parts(entry: dict[str, Any], parts: list[ArtifactRef]) -> list[ArtifactRef]:
    """Every manifest part a visibility selector addresses, in manifest order.

    One selector per entry, in precedence order ``content_url`` -> ``paper_id``
    -> ``role``. A ``role`` selector matches every part with that role, not
    the first hit: "hide my paper summaries" must hide all sixty-three, not
    one. This is the one selector, shared by the canonical edit path and a
    host's patch layer, so the two cannot disagree about what a patch
    addresses.
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
        updated = self._profile.metadata.model_copy(update=_coerce_structured(patch))
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

    def set_visibility(
        self,
        *,
        profile_visibility: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[ProfileDocument, int]:
        """Set the profile-level and per-artifact privacy tiers."""
        valid_tiers = {"public", "internal", "restricted"}
        doc = self._profile.metadata
        update: dict[str, Any] = {}
        changed = 0
        if profile_visibility is not None:
            if profile_visibility not in valid_tiers:
                raise EditError(
                    f"invalid visibility {profile_visibility!r} (one of {sorted(valid_tiers)})"
                )
            update["visibility"] = profile_visibility
        if artifacts:
            new_has_part = [p.model_copy() for p in doc.has_part]
            new_subject_of = [p.model_copy() for p in doc.subject_of]
            for entry in artifacts:
                tier = entry.get("visibility")
                if tier not in valid_tiers:
                    raise EditError(f"invalid visibility {tier!r} for artifact {entry!r}")
                targets = select_parts(entry, new_has_part) + select_parts(entry, new_subject_of)
                if not targets:
                    raise EditError(f"no manifest artifact matches {entry!r}")
                for target in targets:
                    if target.role in ALWAYS_RESTRICTED_ROLES and tier != "restricted":
                        raise EditError(FULLTEXT_LOCK_REASON)
                    if target.visibility != tier:
                        changed += 1
                    target.visibility = tier  # type: ignore[assignment]
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
    "STRUCTURED_METADATA_FIELDS",
    "select_parts",
    "EditManager",
]
