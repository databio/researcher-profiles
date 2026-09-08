"""``.build/<slug>/meta/build_state.json``, build bookkeeping, never published.

An optional sidecar a build tool may keep. Everything a build needs to
remember but nobody should read as part of the researcher's record lives here:
per-paper download/verify/reject state, a ledger of completed build phases,
and the deep-level *inputs* (a CV path, website URLs, a grants source) that
produced the published result.

Two rules define this file:

1. It is not part of the published profile: ``build_profile_archive`` never
   ships it, the manifest in ``profile.jsonld`` never lists it, and a profile
   published as static files legitimately has none. ``BuildState.load`` returns
   an empty state when the file is absent.
2. It keeps its own private integer ``schema_version``. The published
   artifacts use a resolvable ``conformsTo`` IRI rather than an integer counter
   precisely because they are published; this file is build-local, so a plain
   counter is the right tool and carries no external promise.
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schema.jsonld import canonical_dumps

#: Bumped when this sidecar's own shape changes. Build-local; never published.
BUILD_STATE_SCHEMA_VERSION = 1

BUILD_STATE_PATH = ("meta", "build_state.json")

# Build-side status of one paper. The SDK acts on exactly one value,
# "downloaded" (fulltext is on disk); the rest exist so a build tool can
# record why a paper is not.
PaperStatus = Literal[
    "pending",
    "downloaded",
    "download_failed",
    "identity_unverified",
    "rejected",
    "skipped",
    "manual",
]

#: Where a profile's grant records came from. "grants-data" = fetched from a
#: grants service; "manual" = a hand-supplied grants file; "none" = grants
#: explicitly not part of this profile.
GrantsSource = Literal["grants-data", "manual", "none"]


class _Sidecar(BaseModel):
    # ``validate_assignment`` closes a write/read asymmetry. Pydantic v2 does
    # not validate on ``setattr`` by default, so every caller that mutates a
    # loaded sidecar attribute-by-attribute could persist a value that
    # the next ``BuildState.load`` then rejects. With it, an invalid status
    # raises at the write site, where the offending code is, instead of at the
    # next load, where it is not.
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PaperBuildState(_Sidecar):
    """Per-paper build bookkeeping, keyed by ``paper_id``."""

    status: PaperStatus = "pending"
    #: Excluded from synthesis, export, publish, and the embedding index.
    contaminated: bool = False
    #: Result of the identity gate. None = not checked; False = the build
    #: could not confirm this paper is the researcher's own work.
    identity_verified: bool | None = None


class BuildInputs(_Sidecar):
    """The supplied private resources that define a ``deep`` build.

    ``lite``/``full`` need only a name + rid (everything else comes from public
    APIs); ``deep``'s value comes precisely from what a human supplies here. A
    deep build with none of them configured must fail its precondition. These
    are build *inputs*: what the profile publishes is the *result* (manifest
    entries for ``sources/cv.md`` / ``sources/web/*.md``, and the person's URLs
    as ``sameAs``).
    """

    grants_source: GrantsSource | None = None
    reporter_supplement: bool = False
    cv_source: str | None = None
    websites: list[str] = []

    #: Search hints for the identity-resolution step, such as ``department``,
    #: ``title``, ``affiliation_aliases``, or ``institution_id``. A department
    #: and a title supplied by an operator are BUILD INPUTS, not published
    #: claims about a person, so they ride here and never in ``profile.jsonld``.
    resolve_hints: dict[str, Any] = {}


class BuildLedger(_Sidecar):
    """Which build phases have completed, and how often each was retried."""

    mode: str = "synthesize"
    completed_phases: list[str] = []
    phase_retries: dict[str, int] = {}


class BuildState(_Sidecar):
    """Top-level model for ``.build/<slug>/meta/build_state.json``."""

    schema_version: int = BUILD_STATE_SCHEMA_VERSION
    build: BuildLedger = Field(default_factory=BuildLedger)
    inputs: BuildInputs = Field(default_factory=BuildInputs)
    papers: dict[str, PaperBuildState] = {}

    # --- convenience ----------------------------------------------------

    def paper(self, paper_id: str) -> PaperBuildState:
        """Return (creating if needed) the build state for ``paper_id``."""
        state = self.papers.get(paper_id)
        if state is None:
            state = PaperBuildState()
            self.papers[paper_id] = state
        return state

    def status_of(self, paper_id: str | None) -> str:
        if not paper_id:
            return "pending"
        state = self.papers.get(paper_id)
        return str(state.status) if state else "pending"

    def is_contaminated(self, paper_id: str | None) -> bool:
        if not paper_id:
            return False
        state = self.papers.get(paper_id)
        return bool(state.contaminated) if state else False

    # --- io -------------------------------------------------------------

    @classmethod
    def path_for(cls, profile_dir: str | Path) -> Path:
        # Build state is build-session bookkeeping: it lives in the build root
        # (``$RESEARCHER_PROFILES_ROOT/.build/<slug>/meta/build_state.json``), not in
        # the published content directory. ``BUILD_STATE_PATH`` is the
        # ("meta", "build_state.json") tail; the build root supplies the head.
        from .utils.paths import build_dir_for

        return build_dir_for(profile_dir).joinpath(*BUILD_STATE_PATH)

    @classmethod
    def load(cls, profile_dir: str | Path) -> "BuildState":
        """Read the sidecar, or return an empty state when absent.

        Absent is not an error: a published profile carries no build state.
        """
        path = cls.path_for(profile_dir)
        if not path.is_file():
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")) or {})

    def save(self, profile_dir: str | Path) -> Path:
        """Write the sidecar with canonical ordering (papers sorted by id)."""
        path = self.path_for(profile_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_none=True)
        data["papers"] = {k: data["papers"][k] for k in sorted(data.get("papers", {}))}
        path.write_text(canonical_dumps(data), encoding="utf-8")
        return path


#: The per-paper keys that belong to the build, not to ``PaperRecord``.
#: Derived from the model so it cannot drift; ``tests/test_profile.py``
#: asserts a published paper record contains none of them.
BUILD_STATE_PAPER_FIELDS: frozenset[str] = frozenset(PaperBuildState.model_fields)


__all__ = [
    "BUILD_STATE_PAPER_FIELDS",
    "BUILD_STATE_PATH",
    "BUILD_STATE_SCHEMA_VERSION",
    "BuildInputs",
    "BuildLedger",
    "BuildState",
    "GrantsSource",
    "PaperBuildState",
    "PaperStatus",
]
