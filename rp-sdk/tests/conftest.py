"""Pytest fixtures for the whole suite: thin wrappers over ``tests.factories``.

Two rules: every builder lives in :mod:`tests.factories` and fixtures here only
bind it to pytest's lifecycle; and no optional dependency is imported at module
scope (``fastapi``, ``numpy``, ... go inside fixture bodies).
"""

from contextlib import ExitStack
from pathlib import Path

import pytest

from .factories import (
    FIXTURE_DIR,
    ConceptBackend,
    FakeBackend,
    add_fulltext_pdf,
    copy_fixture,
    new_sql_store,
    patch_embedding_backends,
    tree_digest,
)
from .factories import tier_from_header as _tier_from_header

# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_operator_config(monkeypatch, tmp_path_factory):
    """Hide an operator's own settings (root, database URL, retired ``RP_*`` names, login)."""
    from researcher_profiles.env import RETIRED_ENV_VARS
    from researcher_profiles.store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR

    monkeypatch.delenv(PROFILES_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(DATABASE_URL_ENV_VAR, raising=False)
    for name in RETIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The same applies to a stored `rp login`: a developer's real credentials.json
    # would otherwise supply a URL and token to every remote command.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    monkeypatch.delenv("RESEARCHER_PROFILES_TOKEN", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _fixture_tree_is_read_only():
    """Fail the session if any test wrote into ``tests/fixtures/`` (inputs, not scratch)."""
    before = tree_digest(FIXTURE_DIR)
    yield
    after = tree_digest(FIXTURE_DIR)
    assert after == before, (
        "a test mutated tests/fixtures/: every test must work on a copy "
        "(see the fixture_profile / jane_doe fixtures)"
    )


# ---------------------------------------------------------------------------
# Fixture-derived profiles (writable copies)
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_profile(tmp_path: Path):
    """Factory: ``fixture_profile(slug, with_build=True, drop_index=False)`` -> a writable copy."""
    made: list[str] = []

    def _make(slug: str, *, with_build: bool = True, drop_index: bool = False) -> Path:
        root = tmp_path if slug not in made else tmp_path / f"copy-{len(made)}"
        made.append(slug)
        return copy_fixture(slug, root, with_build=with_build, drop_index=drop_index)

    return _make


@pytest.fixture
def fixture_profiles_root(tmp_path: Path):
    """Factory: ``fixture_profiles_root(*slugs)`` -> a root dir; the same set returns the same root."""
    roots: dict[tuple, Path] = {}

    def _make(*slugs: str, with_build: bool = True) -> Path:
        key = (slugs, with_build)
        if key in roots:
            return roots[key]
        root = tmp_path / (f"profiles{len(roots)}" if roots else "profiles")
        root.mkdir(parents=True, exist_ok=True)
        for slug in slugs:
            copy_fixture(slug, root, with_build=with_build)
        roots[key] = root
        return root

    return _make


@pytest.fixture
def jane_doe_dir(fixture_profile) -> Path:
    """A writable copy of the ``jane-doe`` fixture directory."""
    return fixture_profile("jane-doe")


@pytest.fixture
def jane_doe(jane_doe_dir: Path):
    """A ``ResearcherProfile`` over a writable copy of ``jane-doe``."""
    from researcher_profiles import ResearcherProfile

    return ResearcherProfile.from_files(jane_doe_dir)


@pytest.fixture(scope="session")
def jane_doe_readonly_dir(tmp_path_factory) -> Path:
    """One session-wide copy of ``jane-doe``, for suites that only assert."""
    return copy_fixture("jane-doe", tmp_path_factory.mktemp("readonly"))


@pytest.fixture
def jane_doe_readonly(jane_doe_readonly_dir: Path):
    """A fresh ``ResearcherProfile`` per test over the shared read-only copy (lazy loads stay unloaded)."""
    from researcher_profiles import ResearcherProfile

    return ResearcherProfile.from_files(jane_doe_readonly_dir)


@pytest.fixture(scope="session")
def golden(tmp_path_factory):
    """Factory: ``golden(name, build)`` -> a dir built once per session by ``build(dest)``.

    Never hand the returned dir to a test; copy it (``shutil.copytree``) into tmp_path.
    """
    built: dict[str, tuple[Path, str]] = {}

    def _get(name: str, build) -> Path:
        if name not in built:
            dest = tmp_path_factory.mktemp(f"golden-{name}")
            build(dest)
            built[name] = (dest, tree_digest(dest))
        return built[name][0]

    yield _get
    changed = [n for n, (p, d) in built.items() if tree_digest(p) != d]
    assert not changed, f"a test wrote into a golden corpus: {changed}"


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_embedding_model(request, monkeypatch):
    """Fail any non-integration test that loads a real sentence-transformers model."""
    if request.node.get_closest_marker("integration"):
        yield
        return
    try:
        from researcher_profiles.embeddings.backends import SentenceTransformerBackend
    except ImportError:
        yield
        return
    tried: list[str] = []

    def _refuse(self):
        if self._model is not None:  # a test-planted double, not a real load
            return self._model
        tried.append(self.model_name)
        raise RuntimeError(f"unit test tried to load real model {self.model_name!r}")

    monkeypatch.setattr(SentenceTransformerBackend, "_load", _refuse)
    yield
    # Callers catch Exception around backend loads, so the raise alone could be swallowed.
    assert not tried, f"real embedding model requested: {tried}; use the fake_backend fixture"


@pytest.fixture
def use_backend(monkeypatch):
    """Factory: ``use_backend(backend)`` -> ``backend``, now every backend lookup in the SDK."""
    return lambda backend: patch_embedding_backends(monkeypatch, backend)


@pytest.fixture
def fake_backend(use_backend) -> FakeBackend:
    """Every backend lookup resolves to one ``FakeBackend``: no model, no network."""
    return use_backend(FakeBackend())


@pytest.fixture
def concept_backend(use_backend) -> ConceptBackend:
    """Every backend lookup resolves to one meaning-aware ``ConceptBackend``."""
    return use_backend(ConceptBackend())


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_sql_store():
    """An empty in-memory ``SqlProfileStore`` with its tables created."""
    return new_sql_store()


@pytest.fixture
def make_sql_store():
    """Factory: ``make_sql_store(url="sqlite://")`` -> a fresh SQL store, for tests needing two."""
    return new_sql_store


@pytest.fixture(params=["filesystem", "sql"])
def any_store(request, tmp_path: Path):
    """An empty store, once per backend (ids ``filesystem`` / ``sql``)."""
    if request.param == "sql":
        return new_sql_store()
    from researcher_profiles.store import FilesystemProfileStore

    root = tmp_path / "profiles"
    root.mkdir()
    return FilesystemProfileStore(root)


@pytest.fixture
def server_root_with_pdf(fixture_profiles_root) -> Path:
    """A served root whose jane-doe profile carries a copyrighted PDF."""
    root = fixture_profiles_root("jane-doe")
    add_fulltext_pdf(root / "jane-doe")
    return root


# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------


@pytest.fixture
def make_api_client():
    """Factory: ``make_api_client(store_or_root, token=None, tier_from_header=False, **kw)``.

    A path is wrapped in a ``FilesystemProfileStore``; ``tier_from_header`` makes
    the ``X-Tier`` header set the caller's tier. Clients close at teardown.
    """
    from fastapi.testclient import TestClient

    stack = ExitStack()

    def _make(store_or_root, *, token: str | None = None, tier_from_header: bool = False, **kw):
        from researcher_profiles.api.app import create_app
        from researcher_profiles.store import FilesystemProfileStore, ProfileStore

        store = (
            store_or_root
            if isinstance(store_or_root, ProfileStore)
            else FilesystemProfileStore(store_or_root)
        )
        client = stack.enter_context(TestClient(create_app(store, token=token, **kw)))
        if tier_from_header:
            client.app.state.hooks.caller_resolver = _tier_from_header
        return client

    yield _make
    stack.close()


@pytest.fixture
def api_client(make_api_client, fixture_profiles_root):
    """A TestClient over a profiles root holding one copy of ``jane-doe``."""
    return make_api_client(fixture_profiles_root("jane-doe"))
