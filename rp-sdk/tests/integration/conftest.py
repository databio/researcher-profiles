"""Fixtures + collection guards for the end-to-end integration suite.

These tests exercise the real embeddings stack (sentence-transformers +
sqlite-vec) and the FastAPI surface against actual on-disk researcher
profiles.

The plain ``integration`` marker is added to every collected item in this
directory automatically (no need to decorate each test). ``pyproject.toml``
deselects ``integration`` by default (``addopts = "-m 'not integration and
not llm'"``), and ``scripts/test-integration.sh`` passes ``-m integration``
to opt back in. LLM tests must *additionally* carry ``@pytest.mark.llm`` and
depend on the ``anthropic_required`` fixture, which provides the second gate
(``RUN_LLM_TESTS=true`` and ``ANTHROPIC_API_KEY``).
"""

import os
import shutil
from pathlib import Path

import pytest

from researcher_profiles.utils.paths import build_dir_for

from ..factories import copy_profile_tree

# Integration tests run against real on-disk profiles, which are never checked
# into this (public) repo. Point them at a local directory of real profiles via
# environment variables; when unset, the fixtures below skip cleanly.
REAL_PROFILES_ROOT = Path(
    os.environ.get(
        "RESEARCHER_PROFILES_TEST_PROFILE_DIR",
        "/nonexistent/researcher-profiles",
    )
).expanduser()
PRIMARY_SLUG = os.environ.get("RESEARCHER_PROFILES_TEST_PRIMARY_SLUG", "primary")
SECONDARY_SLUG = os.environ.get("RESEARCHER_PROFILES_TEST_SECONDARY_SLUG", "secondary")


# ---------------------------------------------------------------------------
# Collection guard: mark everything here `integration`.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Mark every item in this directory ``integration``.

    Selection is the marker's job: ``pyproject.toml`` deselects
    ``integration`` by default, and ``scripts/test-integration.sh`` passes
    ``-m integration`` to opt back in. There is no env-var gate: a conftest
    hook runs before pytest's mark plugin, so an added skip marker was
    deselected before it could ever fire.
    """
    here = Path(__file__).resolve().parent
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        try:
            item_path.relative_to(here)
        except ValueError:
            continue
        item.add_marker(pytest.mark.integration)


# ---------------------------------------------------------------------------
# Profile fixtures
# ---------------------------------------------------------------------------


def _copy_real_profile(slug: str, dest_root: Path) -> Path:
    """Copy a real profile dir into ``dest_root`` and strip any prebuilt index.

    The source is a developer's own profiles directory (see the env vars above),
    not ``tests/fixtures/``. That is why the gating and the root live here
    rather than in ``tests/factories.py``. The copy itself is shared.
    """
    src = REAL_PROFILES_ROOT / slug
    if not src.is_dir():
        pytest.skip(f"real profile not available: {src}")
    dest = copy_profile_tree(src, dest_root / slug)
    # The build sidecar lives outside the content root (paths.build_dir_for);
    # copy it too so a copied profile is the same profile.
    src_build = build_dir_for(src)
    if src_build.is_dir():
        shutil.copytree(src_build, build_dir_for(dest))
    return dest


@pytest.fixture(scope="session")
def primary_slug() -> str:
    """The slug the real-profile fixtures load as `primary`."""
    return PRIMARY_SLUG


@pytest.fixture(scope="session")
def secondary_slug() -> str:
    """The slug the real-profile fixtures load as `secondary`."""
    return SECONDARY_SLUG


@pytest.fixture(scope="session")
def copy_real_profile():
    """Factory: ``copy_real_profile(slug, dest_root)`` -> an isolated copy.

    Skips the requesting test when the real profile is not on this machine, which
    is why this is a fixture and not a plain helper in ``tests/factories.py``:
    the env-var gating and the ``pytest.skip`` are run-scoped concerns, and a
    fixture is how a test module gets at them without importing this conftest.
    """
    return _copy_real_profile


@pytest.fixture(scope="session")
def real_profile_copy(tmp_path_factory) -> Path:
    """A session-scoped, isolated copy of the primary real profile."""
    root = tmp_path_factory.mktemp("profile_primary")
    return _copy_real_profile(PRIMARY_SLUG, root)


@pytest.fixture
def real_profile(real_profile_copy):
    """A fresh ``ResearcherProfile`` over the primary real-profile copy."""
    from researcher_profiles import ResearcherProfile

    return ResearcherProfile.from_files(real_profile_copy)


@pytest.fixture(scope="session")
def built_index_profile(tmp_path_factory):
    """A real profile with ``prof.index.build()`` run exactly once.

    Session-scoped so the (expensive) sentence-transformers download +
    embedding pass only happens once. All search tests share it.
    """
    from researcher_profiles import ResearcherProfile

    root = tmp_path_factory.mktemp("profile_built")
    path = _copy_real_profile(PRIMARY_SLUG, root)
    prof = ResearcherProfile.from_files(path)
    prof.index.build(force=True)
    return prof


# ---------------------------------------------------------------------------
# API fixtures (TestClient against a tmp profiles_dir holding both slugs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_root(tmp_path_factory) -> Path:
    """A profiles_dir containing copies of both primary + secondary."""
    root = tmp_path_factory.mktemp("api_root")
    _copy_real_profile(PRIMARY_SLUG, root)
    _copy_real_profile(SECONDARY_SLUG, root)
    return root


@pytest.fixture(scope="session")
def api_app(api_root):
    from researcher_profiles.api.app import create_app

    return create_app(api_root, token=None)


# ---------------------------------------------------------------------------
# LLM gating
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_required():
    """Second gate for tests marked ``@pytest.mark.llm``.

    Requires both ``RUN_LLM_TESTS=true`` and a non-empty
    ``ANTHROPIC_API_KEY``; otherwise the test is skipped.
    """
    if os.environ.get("RUN_LLM_TESTS") != "true":
        pytest.skip("set RUN_LLM_TESTS=true to run real-API LLM tests")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; cannot run real-API LLM tests")
