"""What the built distributions actually contain, checked against real hatchling builds.

They exist for one failure mode: a path that resolves in an editable checkout and nowhere else,
which no test that only imports the installed package can see. Nothing is marked ``slow``: the
builds are in-process and session-scoped (about a second), and a ``slow`` label would invite a
``-m 'not slow'`` run that silently drops the packaging boundary. Runtime shape (import cost, the
``rp`` command) lives in ``test_guardrails.py``; the ratified text in ``test_spec_docs.py``.
"""

import fnmatch
import tarfile
import zipfile
from pathlib import Path

import pytest

from researcher_profiles.cli import main
from researcher_profiles.vocab import MESH_FILE, OPENALEX_FILE

from .factories import MONOREPO_ROOT, REPO_ROOT

#: Every skill file a distribution must carry; the nested entries catch a "copy SKILL.md only" fix.
EXPECTED_MEMBERS = [
    "researcher_profiles/skill/SKILL.md",
    "researcher_profiles/skill/examples/walkthrough.md",
    "researcher_profiles/skill/reference/read-order.md",
    "researcher_profiles/skill/reference/failure-modes.md",
]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> Path:
    """One real hatchling wheel, built once; read-only."""
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

    That is what a consumer with no matching PyPI wheel gets; the checkout would hide anything the
    sdist dropped (pyproject.toml, ``schemas/``, an ``artifacts`` glob target).
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
    """The skill must ship in the built distribution, not just resolve in an editable install.

    ``Path(__file__).parent.parent.parent / "skill"`` resolves only from a source tree. The wheel
    test (``test_member_ships_non_empty``) proves the files sit under the importable package. Honest
    limit: hatchling ships them because of their location, so deleting the ``artifacts`` line
    alone would not turn it red.
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


@pytest.mark.parametrize(
    "artifact, member",
    [
        *[(a, m) for a in ("built_wheel", "wheel_from_sdist") for m in EXPECTED_MEMBERS],
        ("built_wheel", "researcher_profiles/context/v1.jsonld"),
        ("wheel_from_sdist", "researcher_profiles/context/v1.jsonld"),
        ("built_wheel", f"researcher_profiles/vocab/{OPENALEX_FILE}"),
        ("built_wheel", f"researcher_profiles/vocab/{MESH_FILE}"),
        # PEP 639: `license-files` only works if the sdist shipped LICENSE.
        ("wheel_from_sdist", "researcher_profiles-*.dist-info/licenses/LICENSE"),
    ],
    ids=lambda v: v.rsplit("/", 1)[-1] if "/" in v else v,
)
def test_member_ships_non_empty(artifact, member, request):
    """A pure install must find these: skill tree, ``@context`` (self-hosting), vocabularies, license."""
    wheel = request.getfixturevalue(artifact)
    with zipfile.ZipFile(wheel) as zf:
        found = fnmatch.filter(zf.namelist(), member)
        assert found, f"{artifact} ({wheel.name}) is missing {member}"
        assert zf.getinfo(found[0]).file_size > 0, f"{member} is empty in {artifact}"


class TestWheelIsSdkOnly:
    #: Sibling trees of ``src/`` in rp-sdk: what a loosened wheel scope would leak.
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


class TestDistributionsAreSdkOnly:
    """Neither built artifact may carry foreign files (an unscoped sdist swallowed the monorepo)."""

    #: Segments marking a non-SDK file anywhere; only names that can sit beside pyproject.toml.
    FOREIGN_ANYWHERE = {"node_modules"}

    @pytest.mark.parametrize(
        ("artifact", "strip_root"),
        [("built_wheel", False), ("built_sdist", True)],
        ids=["wheel", "sdist"],
    )
    def test_no_foreign_paths(self, artifact, strip_root, request):
        path = request.getfixturevalue(artifact)
        if strip_root:  # sdist members live under <name>-<version>/
            with tarfile.open(path) as tf:
                names = tf.getnames()
        else:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        offenders = [
            n
            for n in names
            if self.FOREIGN_ANYWHERE.intersection(
                Path(n).parts[1:] if strip_root else Path(n).parts
            )
        ]
        assert not offenders, (
            f"{len(offenders)} foreign path(s) in the {artifact}: {offenders[:10]}"
        )

    #: Tripwire, not an equality check: the unscoped sdist had 10,921 entries, the scoped one < 100.
    MAX_SDIST_ENTRIES = 500

    def test_sdist_is_small_and_complete(self, built_sdist):
        with tarfile.open(built_sdist) as tf:
            names = tf.getnames()
        assert len(names) <= self.MAX_SDIST_ENTRIES, (
            f"the sdist grew to {len(names)} entries (ceiling {self.MAX_SDIST_ENTRIES}); "
            f"an include rule is probably pulling in a sibling tree"
        )
        # The package sources and the context document have to be inside to build a working wheel.
        assert any("src/researcher_profiles/__init__.py" in n for n in names)
        assert any(n.endswith("context/v1.jsonld") for n in names)


class TestLicenseCopiesAgree:
    """``rp-sdk/LICENSE`` must equal the canonical monorepo-root one.

    ``license-files`` cannot reach above the hatchling project root, so the wheel carries only the
    local copy; drift would publish text the repo disagrees with.
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
