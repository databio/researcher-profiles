"""What the built distributions actually contain.

Two artifacts, four claims. The wheel must ship the consumer skill tree and
the JSON-LD ``@context``; it must not ship a module that does not belong in
the SDK; neither the wheel nor the sdist may carry a sibling
component's files; and the sdist must be able to build that wheel, since a
consumer with no matching wheel on PyPI gets the sdist and nothing else. Every
one of those claims is checked against a real hatchling build. They exist for
one failure mode: a path that resolves in an editable checkout and nowhere
else. That failure is invisible to any test that only imports the installed
package.

None of these are marked ``slow``. hatchling is driven through its Python API
rather than a subprocess and the builds are session-scoped, so the whole module
costs about a second. A ``slow`` label here would be false, and worse, it would
invite a ``-m 'not slow'`` run that silently drops the entire packaging
boundary.

This module is about the *distribution*. Claims about the package's runtime
shape (import cost, the ``rp`` command, ``@context``/model parity, the shared
slug grammar) live in ``test_guardrails.py``; claims about the ratified text (SKILL.md,
the repo-root ``spec/`` tree, the published fixture corpus) live in
``test_spec_docs.py``.
"""

import fnmatch
import tarfile
import zipfile
from pathlib import Path

import pytest

from researcher_profiles.cli import main

from .factories import MONOREPO_ROOT, REPO_ROOT

#: Every file the skill tree must carry into a distribution, as wheel-relative
#: paths. Nested entries are the point: a naive "copy the top-level file"
#: packaging fix would satisfy SKILL.md alone and silently drop the rest.
EXPECTED_MEMBERS = [
    "researcher_profiles/skill/SKILL.md",
    "researcher_profiles/skill/examples/walkthrough.md",
    "researcher_profiles/skill/reference/read-order.md",
    "researcher_profiles/skill/reference/failure-modes.md",
]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> Path:
    """One real hatchling wheel, built once and read by every wheel test here.

    A build takes about 30 ms, so sharing it saves little time; the benefit is
    that the build invocation exists in exactly one place. Consumers only read
    the zip; nothing may mutate the file it points at.
    """
    from hatchling.builders.wheel import WheelBuilder

    out = tmp_path_factory.mktemp("wheel")
    built = list(WheelBuilder(str(REPO_ROOT)).build(directory=str(out), versions=["standard"]))
    assert built, "hatchling produced no wheel"
    return Path(built[0])


@pytest.fixture(scope="session")
def built_sdist(tmp_path_factory) -> Path:
    """One real hatchling sdist. A different builder, so a separate guard."""
    from hatchling.builders.sdist import SdistBuilder

    out = tmp_path_factory.mktemp("sdist")
    built = list(SdistBuilder(str(REPO_ROOT)).build(directory=str(out), versions=["standard"]))
    assert built, "hatchling produced no sdist"
    return Path(built[0])


@pytest.fixture(scope="session")
def wheel_from_sdist(built_sdist, tmp_path_factory) -> Path:
    """A wheel built from the UNPACKED sdist, not from the git checkout.

    This is the artifact a consumer actually gets when PyPI has no wheel for
    their platform/Python: pip downloads the sdist, unpacks it into a temp
    directory that contains only what the tarball carried, and builds. Building
    from ``REPO_ROOT`` (what ``built_wheel`` does) cannot detect that the sdist
    dropped something the build needs, such as pyproject.toml, ``schemas/``,
    or a file referenced by an ``artifacts`` glob. The checkout still has
    those files, so the gap stays hidden.
    """
    from hatchling.builders.wheel import WheelBuilder

    work = tmp_path_factory.mktemp("sdist_roundtrip")
    with tarfile.open(built_sdist) as tf:
        try:
            tf.extractall(work, filter="data")
        except TypeError:  # extraction filters landed mid-3.11
            tf.extractall(work)

    roots = [d for d in work.iterdir() if d.is_dir()]
    assert len(roots) == 1, f"sdist unpacked to {roots}, expected one root dir"

    out = tmp_path_factory.mktemp("sdist_wheel")
    built = list(WheelBuilder(str(roots[0])).build(directory=str(out), versions=["standard"]))
    assert built, "hatchling produced no wheel from the unpacked sdist"
    return Path(built[0])


class TestSkillPackaging:
    """Whether the consumer skill actually ships inside the built distribution.

    Why these tests exist
    ---------------------
    Locating SKILL.md with ``Path(__file__).parent.parent.parent / "skill"``
    (three levels up out of ``src/researcher_profiles/`` and into the repo
    checkout) resolves only when the package is installed with ``pip install
    -e`` from a source tree. From a wheel, an sdist install, or a container
    image the directory is not there. ``rp skill --install`` is the only
    supported way to get the skill into ``~/.claude/skills/``, so a path like
    that fails everywhere except a developer's machine.

    A test that only imports the editable install cannot catch that. So the
    load-bearing test here (``test_wheel_contains_skill_tree``) inspects a real
    wheel built from this repo by the ``built_wheel`` fixture. What that
    proves, precisely:

      * the wheel contains ``researcher_profiles/skill/SKILL.md`` and every nested
        file under ``examples/`` and ``reference/``; and
      * the files sit under the *importable package* prefix, so
        ``importlib.resources.files("researcher_profiles") / "skill"`` finds them
        in any install layout.

    It fails if the skill tree is moved outside the package, if an exclude or
    ignore rule drops it from the build, or if a nested subdirectory stops being
    collected. Two honest limits: it exercises the one build backend this project
    declares in pyproject.toml, and hatchling ships the files because of the
    *location inside the package*. The ``artifacts`` line in
    ``[tool.hatch.build.targets.wheel]`` is not what does it, so deleting that
    line alone would not turn this test red. Deleting it plus moving the tree,
    or excluding it, would.

    Two cheaper companions need no build:
    ``test_skill_resources_live_inside_the_package`` states the invariant
    directly (the resource path is inside ``src/researcher_profiles``), and
    ``test_skill_cli_contract`` pins the ``rp skill`` / ``rp skill --install``
    output shape.
    """

    def test_skill_resources_live_inside_the_package(self):
        """The resource root must be under the package, not up in the checkout."""
        from researcher_profiles.cli import iter_skill_files, skill_resource_root

        root = skill_resource_root()
        assert (root / "SKILL.md").is_file(), f"no SKILL.md under {root}"

        pkg_dir = Path(__import__("researcher_profiles").__file__).resolve().parent
        assert str(Path(str(root)).resolve()).startswith(str(pkg_dir)), (
            f"skill resources resolved to {root}, which is outside the package "
            f"directory {pkg_dir}; that only works in an editable install"
        )

        found = {str(rel) for rel, _ in iter_skill_files(root)}
        expected = {m.split("researcher_profiles/skill/", 1)[1] for m in EXPECTED_MEMBERS}
        assert expected <= found, f"traversal missed {sorted(expected - found)}"

    def test_skill_cli_contract(self, tmp_path, capsys):
        """`rp skill` prints SKILL.md; `--install --dir` writes the whole tree."""
        from researcher_profiles.cli import skill_resource_root

        assert main(["skill"]) == 0
        printed = capsys.readouterr().out
        assert printed == (skill_resource_root() / "SKILL.md").read_text(encoding="utf-8")

        dest = tmp_path / "researcher-profile"
        assert main(["skill", "--install", "--dir", str(dest)]) == 0
        listed = capsys.readouterr().out
        written = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
        expected = {m.split("researcher_profiles/skill/", 1)[1] for m in EXPECTED_MEMBERS}
        assert written == expected
        for rel in expected:
            assert f"  {rel}\n" in listed
        assert f"installed to {dest}" in listed

    def test_wheel_contains_skill_tree(self, built_wheel):
        """The skill files are inside a real wheel, and non-empty."""
        with zipfile.ZipFile(built_wheel) as zf:
            names = set(zf.namelist())
            missing = [m for m in EXPECTED_MEMBERS if m not in names]
            assert not missing, (
                f"wheel {built_wheel.name} is missing {missing}; the consumer "
                f"skill would not ship. Wheel contains: {sorted(names)[:40]}"
            )
            # The files must also be non-empty: real content, not a stub.
            for member in EXPECTED_MEMBERS:
                assert zf.getinfo(member).file_size > 0, f"{member} is empty in the wheel"


class TestWheelIsSdkOnly:
    """A real wheel is built from this repo and inspected, so a file that
    does not belong in the SDK fails here instead of in a user's pip install.
    """

    #: Sibling trees of ``src/`` in rp-sdk. ``pyproject.toml`` scopes the wheel
    #: to ``src/researcher_profiles``; this is what a loosened scope would leak.
    REPO_TREES = ("tests/", "scripts/", "schemas/", "skills/")

    def test_wheel_holds_only_the_package_and_its_metadata(self, built_wheel):
        with zipfile.ZipFile(built_wheel) as zf:
            names = zf.namelist()
        stray = [
            n for n in names if not (n.startswith("researcher_profiles/") or ".dist-info/" in n)
        ]
        assert not stray, f"files outside the package leaked into the SDK wheel: {stray}"
        leaked = [n for n in names if n.startswith(self.REPO_TREES)]
        assert not leaked, f"repo trees leaked into the SDK wheel: {leaked}"
        compiled = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        assert not compiled, f"bytecode leaked into the SDK wheel: {compiled}"

    def test_wheel_contains_jsonld_context(self, built_wheel):
        """The JSON-LD @context must ship inside the importable package.

        A pure core install (no extras) must be able to self-host the
        ``@context`` document when publishing a static site (build_site copies
        it via ``jsonld.context_document_text``). The single canonical copy
        lives at ``src/researcher_profiles/context/`` (inside the package),
        and this asserts it stays in the built wheel.
        """
        member = "researcher_profiles/context/v1.jsonld"
        with zipfile.ZipFile(built_wheel) as zf:
            names = set(zf.namelist())
            assert member in names, (
                f"{member} is missing from the wheel; a pure pip install could "
                f"not self-host the @context. Wheel contains: {sorted(names)[:40]}"
            )
            assert zf.getinfo(member).file_size > 0, f"{member} is empty in the wheel"


class TestDistributionsAreSdkOnly:
    """Neither built artifact may carry anything but the SDK's own files.

    An unscoped sdist silently accumulates the whole repo (``node_modules``
    trees and sibling components). hatchling's project root does not honour
    nested ``.gitignore`` files, so the sdist is only correct if pyproject.toml
    scopes it (or the project root contains nothing foreign). This builds both
    artifacts and rejects any foreign path in either.
    """

    #: Segments that mark a non-SDK file wherever they appear. Only names that
    #: can exist beside pyproject.toml belong here; a name that cannot occur
    #: asserts nothing.
    FOREIGN_ANYWHERE = {"node_modules"}

    def _foreign(self, names: list[str], *, strip_root: bool) -> list[str]:
        offenders = []
        for name in names:
            parts = Path(name).parts
            if strip_root:
                parts = parts[1:]  # sdist members live under <name>-<version>/
            if self.FOREIGN_ANYWHERE.intersection(parts):
                offenders.append(name)
        return offenders

    def test_wheel_contains_no_foreign_paths(self, built_wheel):
        with zipfile.ZipFile(built_wheel) as zf:
            offenders = self._foreign(zf.namelist(), strip_root=False)
        assert not offenders, (
            f"foreign paths in the wheel: {offenders[:10]}{' ...' if len(offenders) > 10 else ''}"
        )

    def test_sdist_contains_no_foreign_paths(self, built_sdist):
        with tarfile.open(built_sdist) as tf:
            names = tf.getnames()
        offenders = self._foreign(names, strip_root=True)
        assert not offenders, (
            f"{len(offenders)} foreign path(s) in the sdist (first 10): {offenders[:10]}"
        )
        # And the sdist must still be able to produce a working install:
        # the package sources and the context document have to be inside.
        assert any("src/researcher_profiles/__init__.py" in n for n in names)
        assert any(n.endswith("context/v1.jsonld") for n in names)

    #: A generous ceiling on sdist size, in entries. The pre-scoping sdist
    #: carried the entire monorepo: 10,921 entries, 35 MB, node_modules and
    #: all. The scoped one is under 100. This is not an equality check: adding
    #: SDK source files is normal and must not break the build. It is a tripwire
    #: for the failure that already happened once, where an unscoped include
    #: swallows a sibling tree wholesale.
    MAX_SDIST_ENTRIES = 500

    def test_sdist_stays_small(self, built_sdist):
        """Order-of-magnitude guard: the sdist is the SDK, not the monorepo."""
        with tarfile.open(built_sdist) as tf:
            count = len(tf.getnames())
        assert count <= self.MAX_SDIST_ENTRIES, (
            f"the sdist grew to {count} entries (ceiling {self.MAX_SDIST_ENTRIES}); "
            f"an include rule is probably pulling in a sibling tree"
        )


class TestSdistBuildsAWorkingWheel:
    """The sdist must be clean, and it must still produce a working wheel.

    ``TestDistributionsAreSdkOnly`` proves the tarball carries no foreign paths
    and does carry the package sources. Neither of those proves it BUILDS. An
    sdist that lost pyproject.toml, or whose ``artifacts`` globs point at files
    the tarball did not include, is a clean tarball and a broken release. That
    is what every consumer gets when no wheel matches their platform.

    So: unpack the sdist, build a wheel from the unpacked tree alone, and
    assert that the three things a consumer install depends on are in it: the
    skill tree, the JSON-LD ``@context``, and the license in dist-info.
    """

    def test_wheel_from_sdist_contains_the_skill_tree(self, wheel_from_sdist):
        with zipfile.ZipFile(wheel_from_sdist) as zf:
            names = set(zf.namelist())
        missing = [m for m in EXPECTED_MEMBERS if m not in names]
        assert not missing, (
            f"a wheel built from the sdist is missing {missing}; `pip install "
            f"researcher-profiles` from source would ship no consumer skill. "
            f"Wheel contains: {sorted(names)[:40]}"
        )

    def test_wheel_from_sdist_contains_the_jsonld_context(self, wheel_from_sdist):
        member = "researcher_profiles/context/v1.jsonld"
        with zipfile.ZipFile(wheel_from_sdist) as zf:
            names = set(zf.namelist())
            assert member in names, (
                f"{member} is missing from a wheel built from the sdist; an "
                f"sdist-only install could not self-host the @context"
            )
            assert zf.getinfo(member).file_size > 0, f"{member} is empty"

    def test_wheel_from_sdist_carries_the_license(self, wheel_from_sdist):
        """PEP 639: `license-files` only works if the sdist shipped LICENSE."""
        pattern = "researcher_profiles-*.dist-info/licenses/LICENSE"
        with zipfile.ZipFile(wheel_from_sdist) as zf:
            names = zf.namelist()
            found = [n for n in names if fnmatch.fnmatch(n, pattern)]
            assert found, (
                f"no {pattern} in a wheel built from the sdist; the published "
                f"package would carry no license text. Wheel dist-info: "
                f"{[n for n in names if 'dist-info' in n]}"
            )
            assert zf.getinfo(found[0]).file_size > 0, f"{found[0]} is empty"


class TestLicenseCopiesAgree:
    """``rp-sdk/LICENSE`` must stay byte-identical to the monorepo-root one.

    The ROOT LICENSE is canonical: it covers every component. The rp-sdk copy
    exists only because hatchling's project root is ``rp-sdk/`` and PEP 639's
    ``license-files`` cannot reach above it, so the published distribution can
    only ever carry the local copy. If the two drift, PyPI ships license text
    that the repository does not agree with, which is why this is a packaging
    claim and not a housekeeping one.

    No build needed: this is a comparison of two files on disk.
    """

    def test_sdk_license_matches_the_monorepo_root(self):
        root = MONOREPO_ROOT / "LICENSE"
        local = REPO_ROOT / "LICENSE"
        assert root.is_file(), f"canonical license missing at {root}"
        assert local.is_file(), (
            f"{local} is missing; `license-files` cannot reach above the "
            f"hatchling project root, so the wheel would ship no license"
        )
        assert local.read_bytes() == root.read_bytes(), (
            f"{local} has drifted from the canonical {root}; the published "
            f"distribution would carry license text this repo disagrees with. "
            f"Copy the root file down, do not edit the copy."
        )
