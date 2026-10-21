"""Guardrails on the package's runtime shape: import cost, the ``rp`` command, ``@context``/model
parity, one slug grammar. A test that fails because a researcher's data is wrong belongs with the
module it describes. Siblings: ``test_packaging.py`` (built artifacts), ``test_spec_docs.py`` (spec text).
"""

import ast
import hashlib
import json
import subprocess
import sys
import tomllib
from contextlib import contextmanager

import pytest

from researcher_profiles.api import upload
from researcher_profiles.build_state import BuildState
from researcher_profiles.cli import main
from researcher_profiles.client import ApiArtifactStorage, StaticArtifactStorage
from researcher_profiles.errors import ProfileWriteError
from researcher_profiles.profile import ResearcherProfile
from researcher_profiles.profile.storage import ArtifactStorage, DirectoryArtifactStorage
from researcher_profiles.schema import (
    SLUG_RE,
    Anchor,
    ArtifactRef,
    CareerEntry,
    GrantRecord,
    GrantsDocument,
    Identifier,
    PaperRecord,
    PapersDocument,
    PaperStats,
    ProfileDocument,
    Proof,
    ResearchInterest,
    ResearchOutput,
    SeedPaperRecord,
    SeedPapersDocument,
    Training,
)
from researcher_profiles.schema.jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI
from researcher_profiles.store.sql import SqlArtifactStorage
from researcher_profiles.utils import slug as slug_mod

from .factories import REPO_ROOT

#: Ids for the four-backend conformance parametrization.
STORAGE_IDS = ["file", "sql", "api", "static"]

PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The JSON-LD @context lives inside the package (loaded with importlib.resources).
CONTEXT_DIR = REPO_ROOT / "src" / "researcher_profiles" / "context"
CONTEXT_FILE = CONTEXT_DIR / "v1.jsonld"
LOCK_FILE = CONTEXT_DIR / "v1.lock.json"

#: "Must be absent" in ``test_context_term``.
_MISSING = object()

#: JSON-LD keywords are not terms and never need a definition.
KEYWORDS = {"@context", "@id", "@type"}

#: Models whose keys live in the GLOBAL term space.
GLOBAL_MODELS = [
    ProfileDocument,
    PapersDocument,
    PaperRecord,
    GrantsDocument,
    SeedPapersDocument,
    SeedPaperRecord,
    ArtifactRef,
    Proof,
]

#: Models whose keys are covered by a property- or type-scoped context.
SCOPED_MODELS = {
    "training": Training,
    "career": CareerEntry,
    "researchOutputs": ResearchOutput,
    "anchor": Anchor,
    "paper_stats": PaperStats,
    "identifier": Identifier,
    "MonetaryGrant": GrantRecord,
    "rp:researchInterests": ResearchInterest,
}


@pytest.fixture(scope="module")
def context() -> dict:
    return json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))["@context"]


def _emitted_keys(model) -> set[str]:
    return {
        (field.alias or name)
        for name, field in model.model_fields.items()
        # `affiliation_id` is folded into the affiliation node by the
        # serializer and is never an on-disk key of its own.
        if name != "affiliation_id"
    }


def _is_defined(key: str, context: dict) -> bool:
    """Whether ``key`` expands through a real term rather than through ``@vocab``.

    A compact IRI (``rp:sectionVisibility``) needs only a declared prefix: JSON-LD expands it
    against the prefix and never consults ``@vocab``. An undeclared prefix is a plain key with a
    colon in it, the accident this guardrail catches.
    """
    if key in context:
        return True
    prefix, sep, suffix = key.partition(":")
    return bool(sep and suffix) and isinstance(context.get(prefix), str)


def _blocker(mods: set[str]) -> str:
    """Prelude that makes importing any of ``mods`` fail, like a core-only install."""
    return (
        "import sys\n"
        "class Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        f"        if name.split('.')[0] in {sorted(mods)!r}:\n"
        "            raise ImportError(f'{name} is blocked (simulated core-only install)')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
    )


def _guard(mods: set[str], who: str) -> str:
    """Prelude that purges ``mods`` from sys.modules and asserts nothing re-imports them.

    The snippet must restore ``builtins.__import__ = real_import`` before it prints.
    """
    return (
        "import sys, builtins\n"
        "for name in list(sys.modules):\n"
        f"    if name.split('.')[0] in {sorted(mods)!r}:\n"
        "        del sys.modules[name]\n"
        "real_import = builtins.__import__\n"
        "def guard(name, *a, **kw):\n"
        f"    if name.split('.')[0] in {sorted(mods)!r}:\n"
        f"        raise AssertionError(f'{{name}} was imported by {who}')\n"
        "    return real_import(name, *a, **kw)\n"
        "builtins.__import__ = guard\n"
    )


_RESTORE_IMPORT = "builtins.__import__ = real_import\n"

# Bare core import: pydantic + pyyaml only. The export surface is the newest thing on this path
# and the likeliest to grow a numpy/httpx/anthropic import by accident.
_CORE_IMPORT = (
    "import sys, researcher_profiles\n"
    "from researcher_profiles.models import api as api_models\n"
    "from researcher_profiles.schema import PaperRecord\n"
    "from researcher_profiles import build_export_bundle, render_export_text\n"
    "from researcher_profiles import ExportOptions, ProfileExportBundle\n"
    "PaperRecord(title='t', year=2020)\n"
    "api_models.MatchRequest(query='q')\n"
    "ExportOptions(max_papers=3)\n"
    "assert callable(build_export_bundle) and callable(render_export_text)\n"
    "assert 'sqlite_vec' not in sys.modules, 'sqlite_vec was imported by core'\n"
    "assert 'httpx' not in sys.modules, 'httpx was imported by core'\n"
)

# The dev install has the extras, so "not in sys.modules" cannot tell a core-only surface from a
# lucky one; blocking them at the import hook is what a bare install looks like.
_EXPORT_SURFACE = (
    "from researcher_profiles import (\n"
    "    ExportOptions, ProfileExportBundle, build_export_bundle, render_export_text,\n"
    ")\n"
    "from researcher_profiles.profile import ResearcherProfile\n"
    "p = ResearcherProfile.from_files('tests/fixtures/jane-doe')\n"
    "text = render_export_text(p, ExportOptions(max_papers=2))\n"
    "assert text.strip(), 'empty export'\n"
    "b = build_export_bundle(p, ExportOptions(max_papers=2))\n"
    "assert isinstance(b, ProfileExportBundle) and b.content_hash.startswith('sha256:')\n"
)

# A roster walk over hundreds of profiles must never pay SQLAlchemy's import cost, even on an
# install that HAS the sql extra. The CLI parser is built too: `rp db` must not tax `rp where`.
_NO_SQLALCHEMY = (
    "import sys\n"
    "import researcher_profiles\n"
    "from researcher_profiles.profile import ResearcherProfile\n"
    "from researcher_profiles.store import ProfileStore, FilesystemProfileStore\n"
    "from researcher_profiles.cli import build_parser\n"
    "build_parser(load_plugins=False)\n"
    "heavy = {'sqlalchemy', 'sqlmodel', 'psycopg2'} & {m.split('.')[0] for m in sys.modules}\n"
    "assert not heavy, f'eagerly imported: {sorted(heavy)}'\n"
    "assert callable(ResearcherProfile.from_db), 'from_db must exist without the import'\n"
    "assert 'sqlalchemy' not in sys.modules, 'naming from_db pulled sqlalchemy'\n"
    # ...and it must still WORK, resolved on first access.
    "assert researcher_profiles.SqlProfileStore.__name__ == 'SqlProfileStore'\n"
    "assert 'sqlalchemy' in sys.modules, 'the lazy name never actually imported'\n"
)

# The ProfileStore protocol and the filesystem backend are CORE; only the SQL backend sits behind
# [sql], and asking for it without the extra must raise an ImportError that NAMES the extra.
_SQL_STORE_OFF_CORE_PATH = (
    "import researcher_profiles\n"
    "from researcher_profiles.store import ProfileStore, FilesystemProfileStore\n"
    "from researcher_profiles.api.app import create_app\n"
    "assert 'sqlmodel' not in sys.modules, 'sqlmodel was imported by core'\n"
    "assert isinstance(FilesystemProfileStore('.'), ProfileStore)\n"
    "for name in ('SqlArtifactStorage', 'SqlProfileStore'):\n"
    "    try:\n"
    "        getattr(researcher_profiles, name)\n"
    "    except ImportError as e:\n"
    "        assert 'sql' in str(e), e\n"
    "    else:\n"
    "        raise AssertionError(name + ' resolved without the sql extra')\n"
)

# The @context IRI is an identifier, not a runtime dependency: loading a profile is a pure local
# operation, or every load acquires a network dependency.
_NO_CONTEXT_FETCH = (
    "import sys\n"
    "from pathlib import Path\n"
    "from researcher_profiles.profile import ResearcherProfile\n"
    "from researcher_profiles.schema.jsonld import CONTEXT_URL\n"
    "assert CONTEXT_URL == 'https://profiles.databio.org/context/v1.jsonld'\n"
    "p = ResearcherProfile.from_files(Path('tests/fixtures/jane-doe'))\n"
    "assert p.metadata.name\n"
    "assert p.papers\n"
    "assert 'httpx' not in sys.modules, 'httpx on the profile load path'\n"
    "assert 'urllib.request' not in sys.modules, 'urllib.request on the load path'\n"
    "assert 'requests' not in sys.modules, 'requests on the profile load path'\n"
)

# A consumer with only a published site ranks with numpy alone: the flat reader must not drag
# sqlite_vec (the `vectors` extra's binary half) onto its path.
_REMOTE_VECTOR_PATH = (
    "import hashlib, json, sys\n"
    "import numpy as np\n"
    "from researcher_profiles.store.http import HttpProfileStore  # noqa: F401\n"
    "from researcher_profiles.embeddings.flat import FlatEmbeddingIndex\n"
    "vecs = np.eye(3, dtype='<f4')\n"
    "blob = vecs.tobytes()\n"
    "index = {'backend_spec': 'fake:tiny', 'file': 'fake-tiny.bin', 'dtype': 'float32',\n"
    "         'byte_order': 'little', 'dim': 3, 'count': 3, 'layout': 'row_major',\n"
    "         'normalized': False, 'metric': 'cosine', 'row_key': 'chunk_index',\n"
    "         'rows': [0, 1, 2], 'sha256': hashlib.sha256(blob).hexdigest()}\n"
    "chunks = [{'source_type': 'paper_summary', 'source_id': f'p{i}',\n"
    "           'chunk_index': 0, 'section': None, 'char_count': 1} for i in range(3)]\n"
    "idx = FlatEmbeddingIndex.from_bytes(\n"
    "    json.dumps(index).encode(), blob, json.dumps(chunks).encode())\n"
    "assert len(idx.search_vector(vecs[0], k=2)) == 2\n"
    "assert idx.centroid().shape == (3,)\n"
    "assert 'numpy' in sys.modules, 'the flat reader is supposed to use numpy'\n"
    "assert 'sqlite_vec' not in sys.modules, 'the remote vector path imported sqlite_vec'\n"
)

# `rp list` over a 300-member roster imports the store contract; ndarray/VectorIndex sit under
# TYPE_CHECKING so the protocol module costs nothing. `rp where` must not import the vector stack.
_STORE_CONTRACT_WITHOUT_NUMPY = (
    "from researcher_profiles.store.protocol import ProfileStore, VectorStore\n"
    "from researcher_profiles.store.files import FilesystemProfileStore\n"
    "s = FilesystemProfileStore('tests/fixtures')\n"
    "assert s.resolve_slug('jane-doe') == 'jane-doe'\n"
    "assert s.rid_for('jane-doe')\n"
    "assert s.path_for('jane-doe').name == 'jane-doe'\n"
    + _RESTORE_IMPORT
    + "assert ProfileStore is not None and VectorStore is not None\n"
)

# [sql] must not imply [vectors]: every numpy/sqlite_vec import in store/sql/ is inside a function
# body, or numpy lands behind every `rp db` command and every management host.
_SQL_STORE_WITHOUT_VECTOR_EXTRAS = (
    "from researcher_profiles.store.sql import SqlProfileStore\n"
    "from researcher_profiles.store.sql import _vectors\n"
    "from researcher_profiles.store.db import ChunkVectorRow, ProfileVectorRow\n"
    "store = SqlProfileStore('sqlite://')\n"
    "store.create_all()\n"
    "assert store.list_slugs() == []\n"
    "assert store.backend_spec is None\n"
    "assert store.has_vector_index('nobody') is False\n" + _RESTORE_IMPORT
)

# The server tier must not need scikit-learn ([topics], ~190 MB): clustering gates its own import
# at the call site, so the analytics import and a store's accessors stay sklearn-free.
_MATCH_PATH_WITHOUT_SKLEARN = (
    "import sys\n"
    "from researcher_profiles.analytics import MatchManager  # noqa: F401\n"
    "from researcher_profiles.store import FilesystemProfileStore\n"
    "from researcher_profiles.api import deps  # the /match dependency path\n"
    "assert FilesystemProfileStore('.').match is not None\n"
    "assert 'sklearn' not in sys.modules, 'sklearn was imported by the match path'\n"
    "assert 'scipy' not in sys.modules, 'scipy was imported by the match path'\n"
)

_VECTOR_MODS = {"numpy", "sqlite_vec"}

FRESH_INTERPRETER_CASES = [
    pytest.param(_CORE_IMPORT, id="core-import-pulls-no-extra"),
    pytest.param(
        _blocker({"numpy", "httpx", "anthropic", "sqlite_vec", "sentence_transformers"})
        + _EXPORT_SURFACE,
        id="export-surface-needs-no-extra",
    ),
    pytest.param(_NO_SQLALCHEMY, id="package-never-pulls-sqlalchemy"),
    pytest.param(
        _blocker({"sqlmodel", "sqlalchemy"}) + _SQL_STORE_OFF_CORE_PATH,
        id="sql-store-off-core-path",
    ),
    pytest.param(_NO_CONTEXT_FETCH, id="profile-load-never-fetches-context"),
    pytest.param(_REMOTE_VECTOR_PATH, id="remote-vector-path-numpy-only"),
    pytest.param(
        _guard(_VECTOR_MODS, "the store contract") + _STORE_CONTRACT_WITHOUT_NUMPY,
        id="store-contract-without-numpy",
    ),
    pytest.param(
        _guard(_VECTOR_MODS, "the sql store") + _SQL_STORE_WITHOUT_VECTOR_EXTRAS,
        id="sql-store-without-vector-extras",
    ),
    pytest.param(_MATCH_PATH_WITHOUT_SKLEARN, id="match-path-without-sklearn"),
]


@pytest.mark.parametrize("code", FRESH_INTERPRETER_CASES)
def test_fresh_interpreter(code):
    """One subprocess per row: sys.modules in this session is already polluted by the extras."""
    r = subprocess.run(
        [sys.executable, "-c", code + "print('OK')\n"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )


class TestImportCost:
    """Core import must stay cheap: no extras tiers (sqlite_vec, httpx) dragged in by default."""

    def test_core_import_constructs_models(self):
        import researcher_profiles  # noqa: F401
        from researcher_profiles.models import api as api_models
        from researcher_profiles.schema import PaperRecord

        PaperRecord(title="A Paper", year=2020)
        assert api_models.MatchRequest(query="genomics").k == 5

    def test_only_db_and_store_sql_import_sqlmodel(self):
        """Only ``store/db.py`` and ``store/sql/`` import SQLModel, even inside a function body."""
        src = REPO_ROOT / "src" / "researcher_profiles"
        offenders: list[str] = []
        for py in sorted(src.rglob("*.py")):
            rel = py.relative_to(src).as_posix()
            if rel == "store/db.py" or rel.startswith("store/sql/"):
                continue
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(n.split(".")[0] in ("sqlmodel", "sqlalchemy") for n in names):
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            f"these modules import sqlmodel/sqlalchemy: {offenders}. Only store/db.py "
            "and store/sql/ may; everything else keeps the base install free of it."
        )


class TestCommandHandle:
    """The console script is `rp`: argparse ``prog=`` and ``[project.scripts]`` must agree."""

    @pytest.mark.parametrize(
        ("argv", "stream"),
        [(["--help"], "out"), (["definitely-not-a-subcommand"], "err")],
        ids=["help", "error"],
    )
    def test_output_names_rp(self, capsys, argv, stream):
        with pytest.raises(SystemExit):
            main(argv)
        text = getattr(capsys.readouterr(), stream)
        assert text.splitlines()[0].startswith("usage: rp")
        assert "usage: researcher-profiles" not in text

    def test_pyproject_declares_only_rp(self):
        data = tomllib.loads(PYPROJECT.read_text())
        assert data["project"]["scripts"] == {"rp": "researcher_profiles.cli:main"}


class TestContextParity:
    """The @context and the models describe the same vocabulary; the context bytes match the lock.

    ``@vocab`` tolerates third-party documents, not this repo's own writer output: every key it
    emits needs an explicit term, and an edit to the context must be deliberate.
    """

    def test_every_global_model_key_has_a_context_term(self, context):
        undefined: dict[str, set[str]] = {}
        for model in GLOBAL_MODELS:
            missing = {
                k for k in _emitted_keys(model) if k not in KEYWORDS and not _is_defined(k, context)
            }
            if missing:
                undefined[model.__name__] = missing
        assert not undefined, f"keys with no context term (they would hit @vocab): {undefined}"

    def test_every_scoped_model_key_has_a_scoped_term(self, context):
        undefined: dict[str, set[str]] = {}
        for term, model in SCOPED_MODELS.items():
            definition = context.get(term)
            assert isinstance(definition, dict), f"{term} must carry a scoped @context"
            scoped = definition.get("@context", {})
            missing = {
                k
                for k in _emitted_keys(model)
                if k not in KEYWORDS and k not in scoped and not _is_defined(k, context)
            }
            if missing:
                undefined[term] = missing
        assert not undefined, f"scoped keys with no term: {undefined}"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param(("@vocab",), f"{CONTEXT_URL}#", id="vocab-fallback-ships"),
            pytest.param(("rp",), f"{CONTEXT_URL}#", id="rp-prefix"),
            pytest.param(("@version",), 1.1, id="json-ld-1.1"),
            pytest.param(("type",), "rp:resourceType", id="type-is-not-@type"),
            pytest.param(("id",), _MISSING, id="id-is-not-aliased"),
            pytest.param(("summary",), "schema:description", id="profile-summary-is-description"),
            pytest.param(
                ("ScholarlyArticle", "@context", "summary"),
                "rp:summary",
                id="work-summary-is-llm-summary",
            ),
            # schema.org has no "this person received this grant" relation.
            pytest.param(("heldGrant", "@id"), "rp:heldGrant", id="person-to-grant-is-an-rp-term"),
        ],
    )
    def test_context_term(self, context, path, expected):
        value = context
        for key in path:
            value = value.get(key, _MISSING) if isinstance(value, dict) else _MISSING
        assert value == expected

    def test_context_bytes_match_the_lock(self):
        """An edit to the context must be deliberate: it changes what every profile means."""
        lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        digest = hashlib.sha256(CONTEXT_FILE.read_bytes()).hexdigest()
        assert digest == lock["sha256"], (
            "context/v1.jsonld changed. If the edit is deliberate, update the sha256 in "
            "context/v1.lock.json in the same change (see context/README.md, Change policy)."
        )
        assert lock["iri"] == CONTEXT_URL

    def test_format_iri_is_a_distinct_constant(self):
        """Same value today, but the vocabulary and the document profile may diverge."""
        assert PROFILE_FORMAT_IRI == CONTEXT_URL
        assert ProfileDocument.model_fields["conforms_to"].default == PROFILE_FORMAT_IRI

    def test_build_state_is_not_part_of_the_published_vocabulary(self, context):
        """The sidecar is build-local, so its keys have no terms."""
        assert "papers" not in context
        assert BuildState().schema_version >= 1


class TestSeams:
    """One grammar, one naming authority: the slug grammar is one object, not two matching copies."""

    def test_the_slug_grammar_is_one_object_not_two_copies(self):
        assert slug_mod.SLUG_RE is SLUG_RE
        assert upload.SLUG_RE is SLUG_RE
        assert slug_mod.SLUG_RE.pattern == upload.SLUG_RE.pattern


#: How a profile's directory can be named. ``cache_dir(...)`` is the one
#: sanctioned way through. The serve-time caches (embeddings.sqlite,
#: topics.json, profile_vec.npz, llm-usage.jsonl) sit outside the storage
#: layer and all address their directory that way.
_PROFILE_DIRECTORY_NAMES = {"path", "directory", "require_directory"}


def _roots(node: ast.AST):
    """Every attribute or call name in a receiver chain, outermost first.

    Bare names are skipped: a local called ``path`` is a local, and
    only ``something.path`` / ``something.directory`` /
    ``something.require_directory(...)`` says "this came off a profile".
    """
    while True:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                yield func.id
                return
            yield func.attr
            node = func.value
            continue
        if isinstance(node, ast.BinOp):
            node = node.left
            continue
        if isinstance(node, ast.Attribute):
            yield node.attr
            node = node.value
            continue
        if isinstance(node, ast.Subscript):
            node = node.value
            continue
        return


def _writes_to_a_profile_directory(node: ast.AST) -> bool:
    """Whether the write target is rooted in a profile's own directory."""
    names = list(_roots(node))
    if "cache_dir" in names:
        return False
    return any(n in _PROFILE_DIRECTORY_NAMES for n in names)


class TestStoragePurity:
    """Persistence goes through ``ArtifactStorage`` only; each test pins one way a write could bypass it."""

    SRC = REPO_ROOT / "src" / "researcher_profiles"

    def _module_ast(self, relpath: str) -> ast.Module:
        return ast.parse((self.SRC / relpath).read_text(encoding="utf-8"))

    @pytest.mark.parametrize("module", ["profile/edit.py", "profile/__init__.py"])
    def test_the_edit_surface_never_names_a_path(self, module):
        """Neither module may read ``.path``: persistence is the composed storage backend."""
        tree = self._module_ast(module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "path":
                raise AssertionError(
                    f"{module} reads `.path` at line {node.lineno}; "
                    "persistence goes through `self._storage`"
                )

    @pytest.mark.parametrize("module", ["profile/edit.py", "profile/__init__.py"])
    def test_the_edit_surface_performs_no_io(self, module):
        """Neither module may know whether there is a disk (``self._writes.open`` is not a file)."""
        tree = self._module_ast(module)
        banned = {"write_text", "write_bytes", "mkdir", "unlink", "open"}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in banned:
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Attribute) and receiver.attr == "_writes":
                continue
            raise AssertionError(
                f"{module} calls {node.func.attr}() at line {node.lineno}; "
                "this module must not know whether there is a disk"
            )

    def test_filesystem_era_names_are_gone(self):
        """``stamp_from_disk`` and ``post_write_hook`` were deleted outright: no alias, no shim."""
        for py in self.SRC.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for name in ("stamp_from_disk", "post_write_hook"):
                assert name not in text, f"{py} still mentions {name}"

    def test_the_profile_carries_no_storage_hooks(self):
        """Persistence is composed, not inherited: no ``_load_*`` / ``_save_*`` proxies on the class."""
        leftovers = sorted(
            name
            for name in dir(ResearcherProfile)
            if name.startswith(("_load_", "_save_", "_delete_"))
        )
        assert leftovers == [], (
            f"ResearcherProfile grew storage hooks again: {leftovers}. "
            "Persistence belongs on the composed ArtifactStorage backend."
        )

    def test_the_monkey_patching_extension_model_is_gone(self):
        """Attached methods hide the class's real surface; capability managers are the extension model."""
        offenders = [
            py.relative_to(self.SRC).as_posix()
            for py in self.SRC.rglob("*.py")
            if "_attach" in py.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            f"these modules still attach methods at import time: {offenders}. "
            "Add a capability manager instead."
        )

    def test_nothing_outside_the_backends_writes_to_a_profile_path(self):
        """Only ``storage.py``, ``client/`` and ``store/`` write profile artifacts; ``.cache/`` is exempt."""
        banned = {"write_text", "write_bytes", "mkdir", "unlink"}
        offenders: list[str] = []
        for py in self.SRC.rglob("*.py"):
            rel = py.relative_to(self.SRC).as_posix()
            if rel == "storage.py" or rel.startswith(("client/", "store/")):
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in banned:
                    continue
                if not _writes_to_a_profile_directory(node.func.value):
                    continue
                offenders.append(f"{rel}:{node.lineno} {node.func.attr}()")
        assert offenders == [], (
            "these write straight to a path instead of going through the "
            f"profile's storage backend: {offenders}"
        )

    #: The sanctioned readers of ``ProfileStore.root``, matching the list on the
    #: ``root`` property in ``store/protocol.py``. ``store/`` is here because the
    #: backends define the attribute; everything else is a named exception.
    ROOT_READERS = {
        "analytics/centroids.py",
        "analytics/match.py",
        "analytics/roster.py",
        "api/service.py",
        "api/deps.py",
        "api/routers/push.py",
        "api/upload.py",
    }

    #: Receivers a bare ``.root`` is read off when it is the *store's* root, not
    #: a local path or an argparse ``args.root``. ``roster`` is in the set
    #: because ``analytics.roster._Roster`` re-exposes the store's root.
    STORE_RECEIVERS = {"store", "_store", "roster", "_roster"}

    @staticmethod
    def _reads_a_store_root(node: ast.Attribute) -> bool:
        receiver = node.value
        if isinstance(receiver, ast.Name):
            return receiver.id in TestStoragePurity.STORE_RECEIVERS
        if isinstance(receiver, ast.Attribute):  # self.store, app.state.store
            return receiver.attr in TestStoragePurity.STORE_RECEIVERS
        return False

    def test_only_the_sanctioned_modules_branch_on_the_store_root(self):
        """``ProfileStore.root`` leaks a disk into the contract, so its readers are an allowlist."""
        offenders: list[str] = []
        for py in self.SRC.rglob("*.py"):
            rel = py.relative_to(self.SRC).as_posix()
            if rel in self.ROOT_READERS or rel.startswith("store/"):
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute) and node.attr == "root"):
                    continue
                if self._reads_a_store_root(node):
                    offenders.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
        assert offenders == [], (
            f"these read the store's filesystem root without being sanctioned: {offenders}. "
            "Add a store interface instead, or add the module to ROOT_READERS and to the "
            "``root`` docstring in store/protocol.py."
        )

    #: Every writer on the ``ArtifactStorage`` contract, with a call that
    #: exercises it. A read-only backend must refuse all of them.
    READ_ONLY_WRITERS = {
        "save_document": lambda st: st.save_document({}),
        "save_expertise": lambda st: st.save_expertise("x"),
        "save_soul": lambda st: st.save_soul("x"),
        "save_papers": lambda st: st.save_papers([]),
        "save_grants": lambda st: st.save_grants([]),
        "save_citations": lambda st: st.save_citations({}),
        "save_summary": lambda st: st.save_summary("p", "x"),
        "delete_summary": lambda st: st.delete_summary("p"),
        "save_build_state": lambda st: st.save_build_state(BuildState()),
    }

    def _read_only_storage(self, cls):
        if cls is ApiArtifactStorage:
            return cls(slug="jane-doe", base_url="https://example.org")
        return cls("https://example.org/profiles/jane-doe")

    @pytest.mark.parametrize(
        "cls", [ApiArtifactStorage, StaticArtifactStorage], ids=["api", "static"]
    )
    def test_read_only_backends_refuse_every_writer(self, cls):
        """A published profile is a view. Every writer says so, with one message."""
        storage = self._read_only_storage(cls)
        try:
            for name, call in self.READ_ONLY_WRITERS.items():
                with pytest.raises(ProfileWriteError):
                    call(storage)
                assert name in dir(storage)
            # The refusal happens BEFORE a context exists, so a registered
            # pre-commit hook never observes a write that cannot happen.
            with pytest.raises(ProfileWriteError):
                storage.new_write_context(None, "document")
        finally:
            storage.close()

    @pytest.mark.parametrize(
        "cls",
        [DirectoryArtifactStorage, SqlArtifactStorage, ApiArtifactStorage, StaticArtifactStorage],
        ids=STORAGE_IDS,
    )
    def test_every_backend_implements_the_whole_contract(self, cls):
        """The four backends satisfy ``ArtifactStorage`` method for method (an ABC alone misses names off ``object``)."""
        assert issubclass(cls, ArtifactStorage)
        assert not getattr(cls, "__abstractmethods__", frozenset()), (
            f"{cls.__name__} leaves {sorted(cls.__abstractmethods__)} abstract"
        )
        for name in dir(ArtifactStorage):
            if name.startswith("_"):
                continue
            assert hasattr(cls, name), f"{cls.__name__} is missing {name}"

    def test_an_incomplete_backend_cannot_be_constructed(self):
        """An ABC, so a half-written backend fails loudly at construction."""

        class HalfABackend(ArtifactStorage):
            pass

        with pytest.raises(TypeError):
            HalfABackend()

    def test_every_public_writer_runs_inside_one_write_unit(self, jane_doe_dir, monkeypatch):
        """No writer may bypass the unit: that is what makes hooks reachable."""
        from researcher_profiles.schema import PaperRecord

        calls: list[str] = []
        real = ResearcherProfile.write_unit

        @contextmanager
        def recording(self, kind):
            calls.append(kind)
            with real(self, kind) as ctx:
                yield ctx

        monkeypatch.setattr(ResearcherProfile, "write_unit", recording)

        prof = ResearcherProfile.from_files(jane_doe_dir)
        writers = {
            "save_profile": lambda: prof.save_profile(),
            "save_expertise": lambda: prof.save_expertise("# e\n"),
            "save_soul": lambda: prof.save_soul("# s\n"),
            "save_papers": lambda: prof.save_papers([PaperRecord(paper_id="x", title="t")]),
            "save_grants": lambda: prof.save_grants([]),
            "save_trials": lambda: prof.save_trials([]),
            "save_seed_papers": lambda: prof.save_seed_papers([]),
            "save_clinical_expertise": lambda: prof.save_clinical_expertise("# c\n"),
            "save_citations": lambda: prof.save_citations(None),
            "save_summary": lambda: prof.save_summary("x", "body"),
            "delete_summary": lambda: prof.delete_summary("x"),
            "save_build_state": lambda: prof.save_build_state(),
        }
        public = {
            name
            for name in dir(ResearcherProfile)
            if (name.startswith("save_") or name.startswith("delete_"))
            and callable(getattr(ResearcherProfile, name))
        }
        assert public == set(writers), (
            "a public writer was added or removed without registering it here; "
            "every one of them must run inside a write_unit"
        )

        for name, call in writers.items():
            calls.clear()
            call()
            assert len(calls) == 1, f"{name} opened {len(calls)} write units, expected exactly 1"


class TestNoConditionalSkips:
    """The default suite runs every test it collects: zero skips.

    A `pytest.skip` outside tests/integration/ means either a combination that
    should not have been generated (the 168-skip read-projection matrix that
    this guardrail replaced) or a dependency the [dev] extra already
    guarantees. Both are bugs in the test, not facts about the machine. The
    genuinely conditional skips all live under tests/integration/: a real
    profile that is not on this box, a Postgres URL, an API key.
    """

    def test_no_skips_outside_the_integration_suite(self):
        # Excluded by name, not by splitting the literals below: this file's
        # own source contains the strings it scans for, and that cleverness
        # rots faster than a plain exclusion.
        offenders = []
        tests_dir = REPO_ROOT / "tests"
        for path in sorted(tests_dir.rglob("test_*.py")):
            if path.name == "test_guardrails.py":
                continue
            if "integration" in path.relative_to(tests_dir).parts:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if "pytest.skip(" in line or "pytest.mark.skip" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}")
        assert not offenders, "conditional skips outside tests/integration/: " + ", ".join(
            offenders
        )
