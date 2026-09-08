"""Guardrails on the package's runtime shape.

Every test here asserts something about ``researcher_profiles`` as an imported
thing: what importing it costs and what it refuses to drag in, what its command
is called, whether its ``@context`` and its models describe the same vocabulary,
and whether the slug grammar is one object rather than two copies. A test that
would fail because a *researcher's data* is wrong does not belong here; it
belongs with the module whose behavior it describes.

Two sibling modules carry the rest of the package-level guardrails:
``test_packaging.py`` (what the built wheel and sdist contain) and
``test_spec_docs.py`` (SKILL.md, the repo-root spec examples, the published
fixture corpus).
"""

import ast
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest
import tomllib

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
    ResearchOutput,
    Training,
)
from researcher_profiles.schema.jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI
from researcher_profiles.store.sql import SqlArtifactStorage
from researcher_profiles.utils import slug as slug_mod

from .factories import REPO_ROOT

#: Ids for the four-backend conformance parametrization.
STORAGE_IDS = ["file", "sql", "api", "static"]

PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The frozen JSON-LD @context lives inside the package (it is loaded with
#: importlib.resources, and the wheel ships it via [tool.hatch...wheel].artifacts).
CONTEXT_DIR = REPO_ROOT / "src" / "researcher_profiles" / "context"
CONTEXT_FILE = CONTEXT_DIR / "v1.jsonld"
LOCK_FILE = CONTEXT_DIR / "v1.lock.json"

#: JSON-LD keywords are not terms and never need a definition.
KEYWORDS = {"@context", "@id", "@type"}

#: Models whose keys live in the GLOBAL term space.
GLOBAL_MODELS = [
    ProfileDocument,
    PapersDocument,
    PaperRecord,
    GrantsDocument,
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


class TestImportCost:
    """Core import must be cheap: only pydantic + pyyaml, no extras pulled.

    Importing ``researcher_profiles`` and its wire models must not drag in the
    ``embeddings`` (``sqlite_vec``) or ``client`` (``httpx``) extra tiers. A
    bare ``pip install researcher-profiles`` (core only) must import cleanly, and even in a dev
    environment where the extras happen to be installed, plain ``import
    researcher_profiles`` must not eagerly load them.
    """

    def test_core_import_constructs_models(self):
        import researcher_profiles  # noqa: F401
        from researcher_profiles.models import api as api_models
        from researcher_profiles.schema import PaperRecord

        rec = PaperRecord(title="A Paper", year=2020)
        assert rec.title == "A Paper"

        req = api_models.MatchRequest(query="genomics")
        assert req.query == "genomics"
        assert req.k == 5

    def test_core_import_does_not_pull_extras(self):
        """Run in a FRESH interpreter so nothing else in the test session has
        already imported sqlite_vec / httpx into sys.modules."""
        code = (
            "import sys, researcher_profiles\n"
            "from researcher_profiles.models import api as api_models\n"
            "from researcher_profiles.schema import PaperRecord\n"
            # The knowledge-base export surface is CORE: a bare install must be
            # able to render one. It is the newest thing on this path and the
            # likeliest to grow a numpy/httpx/anthropic import by accident.
            "from researcher_profiles import build_export_bundle, render_export_text\n"
            "from researcher_profiles import ExportOptions, ProfileExportBundle\n"
            "PaperRecord(title='t', year=2020)\n"
            "api_models.MatchRequest(query='q')\n"
            "ExportOptions(max_papers=3)\n"
            "assert callable(build_export_bundle) and callable(render_export_text)\n"
            "assert 'sqlite_vec' not in sys.modules, 'sqlite_vec was imported by core'\n"
            "assert 'httpx' not in sys.modules, 'httpx was imported by core'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"core import pulled an extra:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_the_export_surface_works_without_any_extra(self):
        """Render an export in an interpreter where the extras cannot be imported.

        The dev install has numpy / httpx / anthropic present, so "is it in
        ``sys.modules``" cannot tell a core-only surface from a lucky one. This
        blocks those distributions at the import hook instead, which is what a
        bare core install (no extras) actually looks like, and then
        renders a real profile through the public names.
        """
        code = (
            "import sys\n"
            "BLOCKED = {'numpy', 'httpx', 'anthropic', 'sqlite_vec', 'sentence_transformers'}\n"
            "class Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        return None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in BLOCKED:\n"
            "            raise ImportError(f'{name} is blocked (simulated core-only install)')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "from researcher_profiles import (\n"
            "    ExportOptions, ProfileExportBundle, build_export_bundle, render_export_text,\n"
            ")\n"
            "from researcher_profiles.profile import ResearcherProfile\n"
            "p = ResearcherProfile.from_files('tests/fixtures/jane-doe')\n"
            "text = render_export_text(p, ExportOptions(max_papers=2))\n"
            "assert text.strip(), 'empty export'\n"
            "b = build_export_bundle(p, ExportOptions(max_papers=2))\n"
            "assert isinstance(b, ProfileExportBundle) and b.content_hash.startswith('sha256:')\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"the export surface needs an extra:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_importing_the_package_never_pulls_sqlalchemy(self):
        """Optional means optional at RUNTIME, not merely at install time.

        This install HAS the ``sql`` extra, which is the whole point: a
        ``try: from .store.sql import ... except ImportError`` at module scope
        would pass the absence test above and still make every
        ``import researcher_profiles`` on this machine pay SQLAlchemy's import
        cost. A roster walk over hundreds of profiles, which must never touch
        a database, is what notices.

        The CLI parser is built too, because that is what an ``rp`` invocation
        actually does before dispatching, and ``rp db`` must not tax ``rp
        where``.
        """
        code = (
            "import sys\n"
            "import researcher_profiles\n"
            "from researcher_profiles.profile import ResearcherProfile\n"
            "from researcher_profiles.store import ProfileStore, FilesystemProfileStore\n"
            "from researcher_profiles.cli import build_parser\n"
            "build_parser(load_plugins=False)\n"
            "heavy = {'sqlalchemy', 'sqlmodel', 'psycopg2'} & {\n"
            "    m.split('.')[0] for m in sys.modules\n"
            "}\n"
            "assert not heavy, f'eagerly imported: {sorted(heavy)}'\n"
            "assert callable(ResearcherProfile.from_db), 'from_db must exist without the import'\n"
            "assert 'sqlalchemy' not in sys.modules, 'naming from_db pulled sqlalchemy'\n"
            # ...and it must still WORK, resolved on first access.
            "assert researcher_profiles.SqlProfileStore.__name__ == 'SqlProfileStore'\n"
            "assert 'sqlalchemy' in sys.modules, 'the lazy name never actually imported'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
        assert result.returncode == 0, (
            f"the package root imports SQLAlchemy eagerly:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_the_sql_store_is_not_on_the_core_import_path(self):
        """A bare install must import with ``sqlmodel`` blocked, and say so.

        The ``ProfileStore`` *protocol* and the filesystem backend are CORE:
        an app factory that could not be typed without an optional extra would
        be a strange thing. Only the SQL backend sits behind ``[sql]``, and
        asking for one of its names on a core install must raise an ImportError
        that NAMES the extra rather than handing back ``None``.
        """
        code = (
            "import sys\n"
            "class Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        return None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in {'sqlmodel', 'sqlalchemy'}:\n"
            "            raise ImportError(f'{name} is blocked (simulated core-only install)')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
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
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
        assert result.returncode == 0, (
            f"the sql store leaked onto the core path:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_only_db_and_store_sql_import_sqlmodel(self):
        """``store/db.py`` and the ``store/sql/`` package own the SQLModel dependency.

        Nothing else may import it. Asserted over the import statements
        themselves, so a module that merely *names* the extra in a docstring is
        not an offender and one that imports it inside a function still is.
        """
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

    def test_loading_a_profile_never_dereferences_the_context(self):
        """The ``@context`` IRI is an identifier, not a runtime dependency.

        Loading and validating a profile is a pure local operation against the
        pydantic models. If any module on that path grew an HTTP fetch of
        ``https://profiles.databio.org/context/v1.jsonld``, every profile load
        would acquire a network dependency and the w3id registration would
        become a blocker for the whole repo. It is not, and this is what keeps
        it that way.
        """
        code = (
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
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"context fetch or extra import on the load path:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_the_remote_vector_path_needs_numpy_but_never_sqlite_vec(self):
        """A consumer with only a published site ranks with numpy alone.

        The flat form exists so an external consumer can search without the
        build-machine index, so the reader that serves it must not drag
        ``sqlite_vec`` (the ``vectors`` extra's binary half) onto its path. A
        stray module-scope import in ``store/http.py`` or in the reader would
        turn "fetch three files and dot some vectors" back into an install
        problem.
        """
        code = (
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
            "assert 'sqlite_vec' not in sys.modules, "
            "'the remote vector path imported sqlite_vec'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"the remote vector path is not numpy-only:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_the_store_contract_stays_importable_without_numpy(self):
        """``ProfileStore`` is on the light path; ``VectorStore`` must not weigh it down.

        ``rp list`` over a 300-member roster imports the store contract. The
        vector capability names ``ndarray`` and ``VectorIndex``, and both are
        under ``TYPE_CHECKING`` precisely so importing the protocol module
        costs nothing.
        """
        code = (
            "import sys\n"
            "for name in list(sys.modules):\n"
            "    if name.split('.')[0] in {'numpy', 'sqlite_vec'}:\n"
            "        del sys.modules[name]\n"
            "import builtins\n"
            "real_import = builtins.__import__\n"
            "def guard(name, *a, **kw):\n"
            "    if name.split('.')[0] in {'numpy', 'sqlite_vec'}:\n"
            "        raise AssertionError(f'{name} was imported by the store contract')\n"
            "    return real_import(name, *a, **kw)\n"
            "builtins.__import__ = guard\n"
            "from researcher_profiles.store.protocol import ProfileStore, VectorStore\n"
            # The identity half of the contract is on the light path too:
            # ``rp where`` is a path lookup and must not import the vector stack
            # to do it. The analytics accessors are lazy so that stays true.
            "from researcher_profiles.store.files import FilesystemProfileStore\n"
            "s = FilesystemProfileStore('tests/fixtures')\n"
            "assert s.resolve_slug('jane-doe') == 'jane-doe'\n"
            "assert s.rid_for('jane-doe')\n"
            "assert s.path_for('jane-doe').name == 'jane-doe'\n"
            "builtins.__import__ = real_import\n"
            "assert ProfileStore is not None and VectorStore is not None\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"the store contract pulled a vector extra:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_the_sql_store_imports_without_the_vector_extras(self):
        """``[sql]`` must not imply ``[vectors]``, even now that it serves vectors.

        The SQL store shreds and queries embeddings, so the temptation is a
        module-scope ``import numpy``. That would put numpy behind every
        ``rp db`` command and every management host that only reads profiles
        out of Postgres. Every numpy / sqlite_vec import in ``store/sql/`` is
        inside a function body, and this is what keeps it there.
        """
        code = (
            "import sys\n"
            "for name in list(sys.modules):\n"
            "    if name.split('.')[0] in {'numpy', 'sqlite_vec'}:\n"
            "        del sys.modules[name]\n"
            "import builtins\n"
            "real_import = builtins.__import__\n"
            "def guard(name, *a, **kw):\n"
            "    if name.split('.')[0] in {'numpy', 'sqlite_vec'}:\n"
            "        raise AssertionError(f'{name} was imported by the sql store')\n"
            "    return real_import(name, *a, **kw)\n"
            "builtins.__import__ = guard\n"
            "from researcher_profiles.store.sql import SqlProfileStore\n"
            "from researcher_profiles.store.sql import _vectors\n"
            "from researcher_profiles.store.db import ChunkVectorRow, ProfileVectorRow\n"
            "store = SqlProfileStore('sqlite://')\n"
            "store.create_all()\n"
            "assert store.list_slugs() == []\n"
            "assert store.backend_spec is None\n"
            "assert store.has_vector_index('nobody') is False\n"
            "builtins.__import__ = real_import\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, (
            f"the sql store pulled a vector extra at import or on a cheap query:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout

    def test_match_path_does_not_pull_sklearn(self):
        """The server tier must not need scikit-learn.

        ``[topics]`` (scikit-learn -> scipy, ~190 MB) is outside the server
        install: a deployment that only serves ``/api/v1/match``
        must never import it. Clustering gates its own import at the call site,
        so importing the analytics and reaching a store's accessors has to stay
        sklearn-free.
        """
        code = (
            "import sys\n"
            "from researcher_profiles.analytics import MatchManager  # noqa: F401\n"
            "from researcher_profiles.store import FilesystemProfileStore\n"
            "from researcher_profiles.api import deps  # the /match dependency path\n"
            "assert FilesystemProfileStore('.').match is not None\n"
            "assert 'sklearn' not in sys.modules, 'sklearn was imported by the match path'\n"
            "assert 'scipy' not in sys.modules, 'scipy was imported by the match path'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"match path pulled sklearn/scipy:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout


class TestCommandHandle:
    """Structural tests pinning the console-script handle to `rp`.

    The distribution is still named `researcher-profiles` and the module is
    still `researcher_profiles`; only the *command* is `rp`. These checks make
    sure the argparse `prog=` and the `[project.scripts]` table agree on that,
    so `--help` and every argparse error name a command the user can actually
    run.

    Pure structure: no subprocess, no network. Whether `rp` is on PATH in this
    environment is an install-time property, not a property of the code.
    """

    def test_help_names_rp(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("usage: rp")
        assert "usage: researcher-profiles" not in out

    def test_errors_name_rp(self, capsys):
        with pytest.raises(SystemExit):
            main(["definitely-not-a-subcommand"])
        err = capsys.readouterr().err
        assert err.startswith("usage: rp")

    def test_pyproject_declares_only_rp(self):
        data = tomllib.loads(PYPROJECT.read_text())
        assert data["project"]["scripts"] == {"rp": "researcher_profiles.cli:main"}


class TestContextParity:
    """The @context and the models must describe the same vocabulary.

    A model field whose on-disk key has no term definition is a term falling
    through ``@vocab`` into an accidental IRI. ``@vocab`` exists to tolerate
    third-party and older documents, not to absorb this repo's own writer
    output, so every key this package emits has to be defined explicitly.

    Also pins the context bytes: ``v1`` is immutable once published, and an
    accidental edit must be a red test rather than a silent break of every
    profile already in the wild.
    """

    def test_every_global_model_key_has_a_context_term(self, context):
        undefined: dict[str, set[str]] = {}
        for model in GLOBAL_MODELS:
            missing = {k for k in _emitted_keys(model) if k not in KEYWORDS and k not in context}
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
                if k not in KEYWORDS and k not in scoped and k not in context
            }
            if missing:
                undefined[term] = missing
        assert not undefined, f"scoped keys with no term: {undefined}"

    def test_context_declares_the_vocab_fallback(self, context):
        """`@vocab` ships: it is what keeps undefined keys from being dropped."""
        assert context["@vocab"] == f"{CONTEXT_URL}#"
        assert context["rp"] == f"{CONTEXT_URL}#"
        assert context["@version"] == 1.1

    def test_id_and_type_are_not_aliased(self, context):
        """A plain ``type:`` on an artifact or a work must not become ``@type``."""
        assert context.get("type") == "rp:resourceType"
        assert "id" not in context

    def test_summary_is_type_scoped_for_works(self, context):
        """The profile's ``summary`` is a description; a work's is an LLM summary."""
        assert context["summary"] == "schema:description"
        assert context["ScholarlyArticle"]["@context"]["summary"] == "rp:summary"

    def test_person_to_grant_link_is_an_rp_term(self, context):
        """schema.org has no "this person received this grant" relation."""
        assert context["heldGrant"]["@id"] == "rp:heldGrant"

    def test_context_bytes_match_the_lock(self):
        """v1 is immutable once published. An edit here breaks every published profile."""
        lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        digest = hashlib.sha256(CONTEXT_FILE.read_bytes()).hexdigest()
        assert digest == lock["sha256"], (
            "context/v1.jsonld changed. v1 is FROZEN: the only permitted edit is "
            "adding a term whose expanded IRI is byte-identical to what @vocab "
            'already produced (i.e. "foo": "rp:foo"). Anything else mints v2 at '
            "a new IRI. If this really was such an addition, update context/v1.lock.json."
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
    """One grammar, one naming authority: claims about package structure.

    The slug grammar is one object shared with the serving side, so the match
    is a fact about identity rather than a claim about two constants (a second
    copy whose docstring says it matches is only a claim).
    """

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
    """Persistence goes through ``ArtifactStorage`` only, asserted structurally.

    Each test below pins one way a write could bypass the storage layer: a
    ``save_*`` method on the profile, ``edit.py`` writing ``prof.path``
    directly, or ``publish/`` doing its own stamp-and-write. Any of those
    leaves a non-filesystem backend unwritable and lets ``set_soul`` on a
    remote profile try to ``mkdir`` at the filesystem root.
    """

    SRC = REPO_ROOT / "src" / "researcher_profiles"

    def _module_ast(self, relpath: str) -> ast.Module:
        return ast.parse((self.SRC / relpath).read_text(encoding="utf-8"))

    @pytest.mark.parametrize("module", ["profile/edit.py", "profile/__init__.py"])
    def test_the_edit_surface_never_names_a_path(self, module):
        """Neither the edit policy nor the aggregate may read ``.path``.

        ``edit.py`` has no path to read; ``profile/__init__.py`` has none
        either, because persistence is the composed storage backend.
        """
        tree = self._module_ast(module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "path":
                raise AssertionError(
                    f"{module} reads `.path` at line {node.lineno}; "
                    "persistence goes through `self._storage`"
                )

    @pytest.mark.parametrize("module", ["profile/edit.py", "profile/__init__.py"])
    def test_the_edit_surface_performs_no_io(self, module):
        """Neither module may know whether there is a disk.

        ``self._writes.open(...)`` is the write unit's context manager, not a
        file, so the receiver is checked as well as the name.
        """
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
        """``stamp_from_disk`` encoded the filesystem assumption in its name;
        ``post_write_hook`` became ``pre_commit_hook``. Both deleted outright:
        no alias, no shim."""
        for py in self.SRC.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for name in ("stamp_from_disk", "post_write_hook"):
                assert name not in text, f"{py} still mentions {name}"

    def test_the_profile_carries_no_storage_hooks(self):
        """Persistence is composed, not inherited. No proxies on the class.

        A backend implements ``ArtifactStorage``; it does not override
        ``_load_*`` / ``_save_*`` hooks on ``ResearcherProfile``. A hook
        appearing here means somebody started growing an inheritance-based
        backend.
        """
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
        """No module may attach methods onto ``ResearcherProfile`` at import.

        Methods attached at import time hide the class's real surface from
        its own body and push callers into ``hasattr`` guards. The capability
        managers are the extension model; attaching is not allowed at all.
        """
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
        """Only the storage backends may open a profile artifact for writing.

        ``storage.py``, ``client/`` and ``store/`` are the backends. Anywhere
        else, a ``write_text`` / ``write_bytes`` / ``mkdir`` / ``unlink`` on
        something derived from a profile is a write bypassing them. The serve-time
        caches under ``.cache/`` are the documented exception and reach their
        directory through ``cache_dir(...)``.
        """
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
        "api/_projection.py",
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
        """``ProfileStore.root`` has one allowlist, and it is this one.

        A filesystem root is the one place the store contract leaks a disk, so
        every new reader is a new way a SQL- or HTTP-backed deployment quietly
        stops working. The ``root`` docstring in ``store/protocol.py`` names the
        readers and why each is allowed; this keeps that list from drifting
        away from the code the way the copy in ``store/__init__.py`` did.
        """
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
        """The four backends satisfy ``ArtifactStorage``, method for method.

        Stronger than a read/write pairing check: this catches a missing
        method, which a pairing list could not. ``ArtifactStorage`` is an ABC,
        so an incomplete subclass also fails to instantiate, but a name
        inherited straight off ``object`` would still slip through, hence the
        second half.
        """
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
