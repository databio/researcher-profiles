"""``.build/<slug>/meta/build_state.json``, build bookkeeping, never published.

An optional sidecar a build tool may keep: per-paper download/verify/reject
state, a ledger of completed build phases, and the deep-level inputs (a CV
path, website URLs, a grants source). It is never published or listed in the
manifest, so it uses a private integer ``schema_version`` rather than a
``conformsTo`` IRI.
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

#: Where a profile's grant records came from. "mygrants" = fetched from the
#: MyGrants service; "manual" = a hand-supplied grants file; "none" = grants
#: explicitly not part of this profile.
GrantsSource = Literal["mygrants", "manual", "none"]


class _Sidecar(BaseModel):
    # ``validate_assignment`` makes an invalid value raise at the ``setattr``
    # site instead of at the next ``BuildState.load``.
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

    A deep build with none of them configured must fail its precondition.
    These are inputs; the profile publishes only the results.
    """

    grants_source: GrantsSource | None = None
    reporter_supplement: bool = False
    cv_source: str | None = None
    #: Path to the interview digest that seeded this profile; set by
    #: ``rp interview import``. Not deep-only: a ``full`` build re-ingests it.
    interview_source: str | None = None
    websites: list[str] = []

    #: Search hints for the identity-resolution step, such as ``department``,
    #: ``title``, ``affiliation_aliases``, or ``institution_id``. A department
    #: and title supplied by an operator are build inputs, not published claims
    #: about a person, so they never go in ``profile.jsonld``.
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
        # Lives in the build root, not the published content directory.
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
#: A published paper record contains none of them.
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
