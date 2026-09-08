"""Pytest fixtures for the whole suite. Thin wrappers over ``tests.factories``.

Every builder lives in :mod:`tests.factories`; this file only binds them to
pytest's lifecycle. A test that needs a shape no fixture covers imports the
factory directly rather than growing another fixture here.

The supported test install is ``pip install -e ".[dev]"``, whose ``dev`` extra
pulls in the ``api``, ``vectors``, and ``client`` tiers (see ``pyproject.toml``).
The suite therefore assumes ``fastapi``, ``httpx``, ``numpy``, and ``sqlite_vec``
are present and several test modules import them at module scope. This
conftest itself still imports no optional dependency at module scope (optional
imports go inside fixture bodies) so it stays importable even on a narrower
install.
"""

import hashlib
from contextlib import ExitStack
from pathlib import Path

import pytest

from .factories import FIXTURE_DIR, copy_fixture

# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_operator_config(monkeypatch, tmp_path_factory):
    """Hide an operator's own settings from every test.

    An exported ``$RESEARCHER_PROFILES_ROOT`` or
    ``$RESEARCHER_PROFILES_DATABASE_URL`` would otherwise reach into the suite
    and change what these tests exercise. On a machine with a root exported, a
    "no root" test would see a root passed anyway, and the failure would look
    like a code regression rather than leakage. Deleting both restores the
    unconfigured behaviour, which is what these tests are about.

    Also deletes every retired ``RP_*`` name (see
    :data:`researcher_profiles.env.RETIRED_ENV_VARS`): those now make a
    resolver raise ``RetiredEnvVarError`` rather than get silently ignored, so
    a developer machine that still has one exported would otherwise fail
    tests that have nothing to do with the rename.
    """
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


def _tree_digest(root: Path) -> str:
    """A digest of every file under ``root``: path and bytes."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def _fixture_tree_is_read_only():
    """Fail the session if any test wrote into ``tests/fixtures/``.

    The committed fixtures are inputs, not scratch space, and several code paths
    under test (the coverage cache, ``llm-usage.jsonl``, manifest refresh) write
    beside the profile they are handed. Hashing the tree at both ends turns "we
    intend not to write to fixtures" into something that actually fails.
    """
    before = _tree_digest(FIXTURE_DIR)
    yield
    after = _tree_digest(FIXTURE_DIR)
    assert after == before, (
        "a test mutated tests/fixtures/: every test must work on a copy "
        "(see the fixture_profile / jane_doe fixtures)"
    )


# ---------------------------------------------------------------------------
# Fixture-derived profiles (writable copies)
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_profile(tmp_path: Path):
    """Factory: ``fixture_profile("jane-doe")`` -> a writable copy under tmp_path.

    The profile's build sidecar comes along unless ``with_build=False``;
    ``drop_index=True`` strips any prebuilt ``.cache/embeddings.sqlite``.
    """
    made: list[str] = []

    def _make(slug: str, *, with_build: bool = True, drop_index: bool = False) -> Path:
        root = tmp_path if slug not in made else tmp_path / f"copy-{len(made)}"
        made.append(slug)
        return copy_fixture(slug, root, with_build=with_build, drop_index=drop_index)

    return _make


@pytest.fixture
def fixture_profiles_root(tmp_path: Path):
    """Factory: ``fixture_profiles_root("jane-doe", "john-smith")`` -> a root dir.

    Asking for the same set twice returns the same root, so a test can name it
    from two fixtures (the client and the directory under it) without building
    two trees.
    """
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
    """A freshly loaded ``ResearcherProfile`` over the shared read-only copy.

    Fresh per test, so lazy-loading assertions still see an unloaded object.
    """
    from researcher_profiles import ResearcherProfile

    return ResearcherProfile.from_files(jane_doe_readonly_dir)


# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------


@pytest.fixture
def make_api_client():
    """Factory: ``make_api_client(store_or_root, token=None, **kw)`` -> a TestClient.

    Accepts a profiles-root path for the common case and wraps it in a
    :class:`~researcher_profiles.store.FilesystemProfileStore`; pass a store
    directly (a ``SqlProfileStore``, say) to serve a different backend. The app
    factory itself takes a store only; the convenience is the fixture's, not
    the SDK's.

    Each client is context-managed for the duration of the test; reach the app
    through ``client.app``. ``fastapi`` is imported inside the body, not at module
    scope; see this module's docstring for why.
    """
    from fastapi.testclient import TestClient

    stack = ExitStack()

    def _make(store_or_root, *, token: str | None = None, **kw):
        from researcher_profiles.api.app import create_app
        from researcher_profiles.store import FilesystemProfileStore, ProfileStore

        store = (
            store_or_root
            if isinstance(store_or_root, ProfileStore)
            else FilesystemProfileStore(store_or_root)
        )
        return stack.enter_context(TestClient(create_app(store, token=token, **kw)))

    yield _make
    stack.close()
