"""Every builder used by more than one test module: documents, trees, doubles.

Plain functions, dataclasses and constants: no pytest fixtures, because
``tests/integration/`` imports from here, so no test module ever has
to import another test module or reach into a conftest. ``tests/conftest.py`` is a
thin fixture layer over this file; anything needing a shape no fixture covers
calls the factory directly.

A builder with exactly one consumer does not belong here; it belongs in that
module, as an underscore-prefixed local helper. Promote it back on the second
consumer, not in advance.

Two rules hold for everything in here:

1. No optional dependency at module scope. ``conftest.py`` imports this
   module on every collection, including on a bare core install (no extras)
   where ``fastapi``, ``httpx``, ``numpy``, ``sqlite_vec`` and
   ``sentence_transformers`` are all absent.
   ``researcher_profiles.embeddings`` counts as optional too. Those imports go
   inside function bodies.
2. Nothing writes into ``tests/fixtures/``. Every builder that starts from a
   committed fixture copies it first. ``tests/conftest.py`` enforces this with a
   session-scoped digest of the fixture tree.

What does not belong here: malformed or nonconforming documents that are
the *subject* of a test. A helper that can produce them is a helper that can
hide them. Use :func:`write_raw_profile` for the boilerplate and leave the
deviation visible in the test body.
"""

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from researcher_profiles.schema import GrantsDocument, PapersDocument, ProfileDocument
from researcher_profiles.schema.jsonld import canonical_dumps
from researcher_profiles.utils.paths import build_dir_for, cache_dir

# ---------------------------------------------------------------------------
# Paths and identity
# ---------------------------------------------------------------------------

#: The rp-sdk component root: the directory holding pyproject.toml, src/,
#: tests/, docs/, schemas/, skills/ and scripts/. The package-level guardrail
#: modules (test_guardrails, test_packaging, test_spec_docs) all resolve
#: SDK assets from here, and hatchling builds are rooted at it.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The monorepo root, one level above rp-sdk. The ratified spec (spec/) and the
#: shared conformance corpus (spec/conformance/) live here, OUTSIDE the SDK
#: component, so anything asserting against ratified text resolves from this.
MONOREPO_ROOT = REPO_ROOT.parent

#: Committed fixture profiles. Read-only: copy before handing one to a test.
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

#: The one context IRI every conforming document carries.
CONTEXT_IRI = "https://profiles.databio.org/context/v1.jsonld"

ADA = "0000-0002-1825-0097"
HOPPER = "0000-0001-2345-6789"

#: A fixed timestamp, so anything that saves a document stays byte-stable.
NOW = "2026-08-07T12:00:00+00:00"


def now_iso() -> str:
    """A second-resolution UTC timestamp, for markers that want a real clock."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Default content for synthetic profiles
# ---------------------------------------------------------------------------

DEFAULT_EXPERTISE = (
    "# Expertise\n\n"
    "## ATAC-seq\n\n"
    "We analyze ATAC-seq data using PEPATAC [smith2021pepatac].\n\n"
    "## Region sets\n\n"
    "ExampleOverlap tests example region enrichment [doe2016example]. "
    "We build region universes.\n"
)

DEFAULT_SOUL = "# Soul\n\n## Mission\n\nMake metadata interoperable.\n"

DEFAULT_SUMMARIES = {
    "paperA": "PEPATAC processes ATAC-seq reads with serial alignment.\n",
    "paperB": "[abstract-only]\nA review on single-cell multi-omics methods.\n",
}

DEFAULT_PAPERS: list[dict] = [
    {"paperId": "test2020a", "title": "A Paper", "year": 2020},
]

DEFAULT_GRANTS: list[dict] = [
    {
        "id": "grantX",
        "name": "A grant on regulatory genomics",
        "abstract": "Funds work on enhancer prediction.",
    },
]

DEFAULT_CV = "# CV\n\n## Education\n\nPhD in genomics, 2015.\n"

DEFAULT_WEB = {
    "1-lab": "# Lab site\n\n## Research\n\nWe study chromatin accessibility.\n",
}


# ---------------------------------------------------------------------------
# Validated document writers (they go through the Pydantic models)
# ---------------------------------------------------------------------------


def write_profile(
    profile_dir: Path,
    *,
    name: str = "Test Researcher",
    rid: str = ADA,
    provenance: str = "third_party",
    **extra: Any,
) -> Path:
    """Write a minimal conforming ``profile.jsonld``.

    ``provenance`` has no default in the format itself; this helper supplies
    ``third_party`` because that is what a test fixture almost always is.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    doc = ProfileDocument(name=name, rid=rid, provenance=provenance, **extra)
    path = profile_dir / "profile.jsonld"
    path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
    return path


def write_papers(profile_dir: Path, papers: list[dict], *, about: str | None = None) -> Path:
    """Write ``sources/papers.jsonld`` from plain dicts of PaperRecord fields."""
    doc = PapersDocument.model_validate({"about": about, "hasPart": papers})
    path = profile_dir / "sources" / "papers.jsonld"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
    return path


def write_grants(profile_dir: Path, grants: list[dict] | None = None) -> Path:
    """Write ``sources/grants.jsonld``: a deep-level, restricted source."""
    doc = GrantsDocument.model_validate({"hasPart": grants or DEFAULT_GRANTS})
    path = profile_dir / "sources" / "grants.jsonld"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Raw document writers (unvalidated)
# ---------------------------------------------------------------------------


def write_raw_profile(profile_dir: Path, doc: dict) -> Path:
    """Write ``profile.jsonld`` verbatim, bypassing the models.

    For documents that must be malformed, nonconforming, or otherwise unrepresentable
    through ``ProfileDocument``. The deviation belongs in the caller.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_dir / "profile.jsonld"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def break_papers(profile_dir: Path) -> Path:
    """Make ``sources/papers.jsonld`` schema-invalid, leaving the tree intact."""
    path = profile_dir / "sources" / "papers.jsonld"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hasPart": [{"year": "not-a-year"}]}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Content writers
# ---------------------------------------------------------------------------


def write_personality(
    profile_dir: Path,
    *,
    expertise: str = DEFAULT_EXPERTISE,
    soul: str = DEFAULT_SOUL,
) -> Path:
    """Write ``personality/expertise.md`` and ``personality/SOUL.md``."""
    d = profile_dir / "personality"
    d.mkdir(parents=True, exist_ok=True)
    (d / "expertise.md").write_text(expertise, encoding="utf-8")
    (d / "SOUL.md").write_text(soul, encoding="utf-8")
    return d


def write_summaries(profile_dir: Path, mapping: Mapping[str, str]) -> Path:
    """Write ``sources/summaries/<paper_id>.summary.md`` for each entry."""
    d = profile_dir / "sources" / "summaries"
    d.mkdir(parents=True, exist_ok=True)
    for paper_id, body in mapping.items():
        (d / f"{paper_id}.summary.md").write_text(body, encoding="utf-8")
    return d


def write_cv(profile_dir: Path, text: str = DEFAULT_CV) -> Path:
    """Write ``sources/cv.md``: a deep-level, restricted source."""
    path = profile_dir / "sources" / "cv.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_web(profile_dir: Path, mapping: Mapping[str, str] | None = None) -> Path:
    """Write ``sources/web/<page_id>.md``: deep-level, restricted sources."""
    d = profile_dir / "sources" / "web"
    d.mkdir(parents=True, exist_ok=True)
    for page_id, body in (mapping or DEFAULT_WEB).items():
        (d / f"{page_id}.md").write_text(body, encoding="utf-8")
    return d


def write_sqlite_index_stub(profile_dir: Path) -> Path:
    """A ``.cache/embeddings.sqlite`` file that is a marker, not an index.

    Enough for a milestone probe (which asks only whether the file exists);
    never enough to search. A real index comes from ``SqliteEmbeddingIndex.build_index``.
    """
    path = cache_dir(profile_dir) / "embeddings.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite format 3\x00")
    return path


def write_flat_index_stub(profile_dir: Path) -> Path:
    """An ``embeddings/index.json`` marker for the flat half of the index any-of."""
    path = profile_dir / "embeddings" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"count": 0, "dim": 0}), encoding="utf-8")
    return path


def refresh_manifest(profile_dir: Path) -> None:
    """Record the directory's actual contents in the profile's manifest."""
    from researcher_profiles.profile import ResearcherProfile

    ResearcherProfile.from_files(profile_dir).build_manifest(write=True)


# ---------------------------------------------------------------------------
# The composite builder: one call per synthetic profile
# ---------------------------------------------------------------------------


def build_profile_dir(
    path: Path,
    *,
    name: str = "Test Researcher",
    rid: str = "0000-0004-4600-113X",
    level: str = "full",
    papers: bool | list[dict] = True,
    personality: bool = True,
    summaries: bool | Mapping[str, str] = True,
    grants: bool | list[dict] = False,
    cv: bool | str = False,
    web: bool | Mapping[str, str] = False,
    index: str | None = None,
    manifest: bool = True,
    **extra: Any,
) -> Path:
    """Materialize the suite's standard built profile at ``path``.

    Every synthetic profile in the suite is one call to this. Flags may be
    ``True`` for the default content or an explicit payload (rows, mapping,
    text).

    ``index`` is ``None | "sqlite" | "flat" | "both"`` and writes **stub** files:
    enough for a milestone probe, never enough to search. A profile with a
    real, searchable index comes from the ``indexed_profile`` fixture, which
    runs ``SqliteEmbeddingIndex.build_index``.
    """
    path.mkdir(parents=True, exist_ok=True)
    write_profile(path, name=name, rid=rid, level=level, **extra)
    if papers:
        write_papers(path, DEFAULT_PAPERS if papers is True else list(papers))
    if personality:
        write_personality(path)
    if summaries:
        write_summaries(path, DEFAULT_SUMMARIES if summaries is True else summaries)
    if grants:
        write_grants(path, None if grants is True else list(grants))
    if cv:
        write_cv(path, DEFAULT_CV if cv is True else cv)
    if web:
        write_web(path, None if web is True else web)
    if index in ("sqlite", "both"):
        write_sqlite_index_stub(path)
    if index in ("flat", "both"):
        write_flat_index_stub(path)
    if index not in (None, "sqlite", "flat", "both"):
        raise ValueError(f"index must be None|sqlite|flat|both, got {index!r}")
    if manifest:
        refresh_manifest(path)
    return path


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def copy_profile_tree(src: Path, dest: Path, *, drop_index: bool = True) -> Path:
    """Copy a profile directory and (by default) strip any prebuilt index.

    Tests always build their own index so a stale one from the source tree
    cannot decide the outcome.
    """
    shutil.copytree(src, dest, symlinks=False)
    if drop_index:
        sqlite = cache_dir(dest) / "embeddings.sqlite"
        if sqlite.exists():
            sqlite.unlink()
    return dest


def copy_fixture(
    slug: str, dest_root: Path, *, with_build: bool = True, drop_index: bool = False
) -> Path:
    """Copy ``tests/fixtures/<slug>`` into ``dest_root`` and return the copy.

    The sibling build sidecar (``tests/fixtures/.build/<slug>``) comes along by
    default: it lives outside the content tree but it is part of the committed
    fixture, and code under test reads it through ``profile.build_state``. Pass
    ``with_build=False`` for a content-root-only copy.
    """
    src = FIXTURE_DIR / slug
    if not src.is_dir():
        raise FileNotFoundError(f"no such fixture profile: {src}")
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = copy_profile_tree(src, dest_root / slug, drop_index=drop_index)
    if with_build:
        src_build = build_dir_for(src)
        if src_build.exists():
            shutil.copytree(src_build, build_dir_for(dest))
    return dest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeBackend:
    """Deterministic backend producing FINITE, normalized vectors.

    Byte-normalized rather than hash-float-unpacked: unpacking a sha256 digest
    as float32 occasionally yields NaN, which sqlite-vec tolerates but which
    breaks exact numeric round-trip asserts on the flat export.
    """

    def __init__(self, name: str = "fake:tiny", dim: int = 16):
        self.name = name
        self.dim = dim

    def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            vals = [(h[i % len(h)] / 255.0) - 0.5 for i in range(self.dim)]
            mag = sum(x * x for x in vals) ** 0.5 or 1.0
            out.append([x / mag for x in vals])
        return out


def fake_llm_response(
    text: str = "reply",
    *,
    model: str = "claude-sonnet-4-6",
    usage: dict | None = None,
    request_id: str = "req_test_123",
):
    """An ``LLMResponse`` literal with a plausible usage block."""
    from unittest.mock import MagicMock

    from researcher_profiles import LLMResponse

    return LLMResponse(
        text=text,
        model=model,
        usage=usage
        or {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 80,
            "cache_read_input_tokens": 0,
        },
        stop_reason="end_turn",
        raw=MagicMock(),
        request_id=request_id,
    )


def stub_llm(profile, response_text: str | None = None):
    """Attach a stubbed LLM client + no-op ``index.search``; return the fake client.

    With ``response_text`` the client answers every ``.complete`` with it; without,
    the mock is left unprimed so a test can assert the LLM was never called.
    """
    from unittest.mock import MagicMock

    from researcher_profiles import LLMClient

    fake = MagicMock(spec=LLMClient)
    if response_text is not None:
        fake.complete.return_value = fake_llm_response(response_text)
    object.__setattr__(profile, "_llm_client", fake)
    profile.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]
    return fake


# ---------------------------------------------------------------------------
# Publish / serve
# ---------------------------------------------------------------------------


def sync_with_publishignore(src: Path, dst: Path) -> None:
    """Copy ``src`` -> ``dst`` honoring only ``.publishignore``.

    Mirrors ``rsync -a --exclude-from=<src>/.publishignore`` for the pattern
    forms this project emits: a line ending in ``/`` excludes that directory
    subtree; any other line excludes that exact relative path.
    """
    patterns = [
        ln.strip()
        for ln in (src / ".publishignore").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    dir_prefixes = tuple(p for p in patterns if p.endswith("/"))
    exact = {p for p in patterns if not p.endswith("/")}

    def excluded(rel: str) -> bool:
        if rel in exact:
            return True
        return any(rel == d.rstrip("/") or rel.startswith(d) for d in dir_prefixes)

    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src).as_posix()
        if excluded(rel):
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
