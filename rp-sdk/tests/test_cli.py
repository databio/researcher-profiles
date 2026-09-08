"""The ``rp`` command line: registration, parsing, wiring, exit codes, JSON.

The ``cli`` package is the user-facing entry point and, for most callers, an
*agent*-facing one: it reads ``--help`` once, copies a line, and checks ``$?``. This
file owns five things: every verb is registered and documents itself, a flag
lands on the right ``dest`` with the right default, ``rp foo --bar X`` reaches
the library as ``bar=X``, the documented 0/1/2/4 exit table, and the ``--json``
shapes. It also owns the ``_plugin_handler`` hook that plugin distributions
re-attach through.

**It does not re-test the library through the CLI**: nearly every
verb wraps a function that ``test_publish``, ``test_api``, ``test_validate``,
``test_embeddings``, ``test_schema`` and ``test_profile``
already cover, so wiring is asserted with a recorder, never by re-running the
work. Signing's end-to-end tests stay in ``test_signing.py``, the packaged
skill tree in ``test_packaging.py``, ``prog="rp"`` in ``test_guardrails.py``;
this file takes only their parsing and exit-code edges.
"""

import argparse
import importlib
import json
import re
from functools import cached_property, lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from researcher_profiles.cli import _COMMANDS, build_parser, main

from .factories import break_papers, copy_fixture

#: Every top-level verb ``rp`` registers. Frozen: adding, renaming
#: or dropping one changes a published interface, so it is typed here too.
VERBS_TOP = (
    "index",
    "export-embeddings",
    "export",
    "search",
    "rank-works",
    "schema",
    "graph",
    "db",
    "push",
    "render",
    "site",
    "install",
    "seek",
    "list",
    "listr",
    "login",
    "logout",
    "whoami",
    "skill",
    "where",
    "mint-local-id",
    "validate",
    "manifest",
    "sign",
    "sign-verify",
    "agent",
    "profile",
)

#: Every reachable parser path, including the two ``schema`` subcommands.
VERB_PATHS = tuple((v,) for v in VERBS_TOP) + (
    ("schema", "export"),
    ("schema", "export-wire"),
    ("graph", "build"),
    ("db", "init"),
    ("db", "push"),
    ("db", "pull"),
    ("db", "list"),
    ("db", "rm"),
    ("agent", "whoami"),
    ("agent", "scopes"),
    ("agent", "config"),
    ("profile", "pull"),
    ("profile", "diff"),
    ("profile", "push"),
    ("profile", "visibility"),
)
VERB_IDS = [" ".join(p) for p in VERB_PATHS]


def _choices(parser: argparse.ArgumentParser) -> dict:
    """The subcommand table of a parser that has one."""
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


@lru_cache(maxsize=1)
def _root_parser() -> argparse.ArgumentParser:
    """The full parser, built once. Rebuilding it per verb costs 45 builds."""
    return build_parser()


def _verb_parser(path: tuple[str, ...]) -> argparse.ArgumentParser:
    parser = _root_parser()
    for name in path:
        parser = _choices(parser)[name]
    return parser


class TestRegistration:
    """Every verb exists, is reachable, and documents itself."""

    @pytest.mark.parametrize("path", VERB_PATHS, ids=VERB_IDS)
    def test_every_verb_is_registered_and_documented(self, path, capsys):
        """One walk of the tree asserting everything a parser owes its reader.

        ``--json`` is one namespace attribute across the whole tool. If it
        arrived on several dests (``as_json``, ``json``, ``status_json``,
        ``validate_json``), a caller reading ``args.as_json`` would silently get
        ``False`` on the other verbs and argparse would report nothing.
        """
        parser = _verb_parser(path)
        # cli's package docstring promises both on every parser; `sign` and
        # `sign-verify` were the two that did not carry them.
        assert parser.description
        assert "Examples:" in (parser.epilog or "")
        for action in parser._actions:
            if "--json" in action.option_strings:
                assert action.dest == "as_json", f"rp {' '.join(path)}"
        with pytest.raises(SystemExit) as e:
            main([*path, "--help"])
        assert e.value.code == 0
        assert capsys.readouterr().out.startswith("usage: rp")

    def test_registered_verbs_are_the_declared_set(self):
        # The SDK's OWN verbs, with CLI plugins excluded: the frozen set is the
        # SDK surface, independent of which other distributions happen to be
        # installed in this environment.
        assert set(_choices(build_parser(load_plugins=False))) == set(VERBS_TOP)
        # And every one of them dispatches. Registering a verb and forgetting
        # its `_COMMANDS` entry is silent: `main` falls through to exit 1.
        assert set(_COMMANDS) == set(VERBS_TOP)

    def test_version_prints_and_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as e:
            main(["--version"])
        assert e.value.code == 0
        assert re.match(r"^rp \S+$", capsys.readouterr().out.strip())


class TestParsing:
    """Flags land on the right ``dest`` with the right default. No dispatch."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["search", "P", "Q"], {"k": 5, "type": None, "as_json": False}),
            (["search", "P", "Q", "-k", "10", "--type", "p"], {"k": 10, "type": "p"}),
            (["search", "P", "Q", "--json"], {"as_json": True}),
            (["validate", "P", "--json"], {"as_json": True}),
            (["manifest", "P", "--json"], {"as_json": True}),
            (["manifest", "P", "--write", "--check"], {"write": True, "check": True}),
            (["index", "P"], {"force": False, "backend": None}),
            (["render", "P"], {"base_url": None, "no_index": False}),
            (["site", "D", "-o", "O"], {"out": "O"}),
            (["site", "D", "--out", "O"], {"out": "O"}),
            (["install", "a", "b"], {"slug": ["a", "b"]}),
            (["install", "a", "--force", "--json"], {"force": True, "as_json": True}),
            (["skill"], {"install": False, "dir": None}),
            (
                ["push", "P", "--url", "U"],
                {"slug": None, "token": None, "include_fulltext": False},
            ),
            (["listr"], {"url": None, "as_json": False}),
        ],
        ids=[
            "search-defaults",
            "search-k-and-type",
            "search-json",
            "validate-json",
            "manifest-json",
            "manifest-write-check",
            "index-defaults",
            "render-defaults",
            "site-o-alias",
            "site-out-alias",
            "install-nargs",
            "install-force-json",
            "skill-defaults",
            "push-defaults",
            "listr-defaults",
        ],
    )
    def test_namespace(self, argv, expected):
        # The `--json` rows carry the most weight. argparse happily sets an
        # attribute nobody reads, so one spelling mapping to several dests
        # (`json`, `status_json`, `validate_json`, `as_json`) goes unnoticed;
        # every `--json` lands on `as_json`, and these rows are where that holds.
        args = build_parser().parse_args(argv)
        assert {k: getattr(args, k) for k in expected} == expected

    @pytest.mark.parametrize(
        "argv",
        [
            ["site", "D"],
            ["mint-local-id"],
            ["search", "P"],
            ["schema"],
            ["schema", "export"],
            ["skill", "--print", "--install"],
            ["index"],
            ["site", "D", "-o", "O", "--force"],
            ["site", "D", "-o", "O", "-v"],
            ["definitely-not-a-verb"],
        ],
        ids=[
            "site-needs-out",
            "mint-needs-name",
            "search-needs-query",
            "schema-needs-subcommand",
            "schema-export-needs-out-dir",
            "skill-print-xor-install",
            "index-needs-profile",
            "site-force-is-gone",
            "site-verbose-is-gone",
            "unknown-verb",
        ],
    )
    def test_usage_error_exits_two(self, argv):
        # The code, not the wording: the code is what an agent branches on.
        # `--force` and `-v/--verbose` were removed from `site`: `build_site`
        # has no such parameters, so both were parsed and discarded, and
        # `--force` documented a guard that does not exist.
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2


def _spy(monkeypatch, dotted: str, result=None) -> list[SimpleNamespace]:
    """Replace a library function with a recorder; return the calls list.

    Patching the *module attribute* suffices because every dispatch branch
    imports inside the branch, so the name is looked up at call time.
    """
    mod_name, _, attr = dotted.rpartition(".")
    calls: list[SimpleNamespace] = []

    def _record(*args, **kwargs):
        calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        return result

    monkeypatch.setattr(importlib.import_module(mod_name), attr, _record)
    return calls


def _results() -> dict:
    """Return values whose *shape* the CLI's own formatting depends on.

    Not bare mocks: `install`'s human branch indexes ``summary["status"]`` and
    ``["path"]`` directly, `push`'s summary line ``.get()``s four keys, and the
    `index` branch reads six report attributes. A thinner stand-in would only
    prove that a KeyError is possible.
    """
    from researcher_profiles.client import RegistryListing
    from researcher_profiles.validate import ProfileValidationReport

    return {
        "researcher_profiles.embeddings.build_index": SimpleNamespace(
            backend_name="fake:tiny", added=1, updated=0, skipped=0, removed=0, duration_s=0.0
        ),
        "researcher_profiles.embeddings.write_flat_export": None,
        "researcher_profiles.publish.render_profile": None,
        "researcher_profiles.publish.build_site": SimpleNamespace(warnings=[]),
        "researcher_profiles.client.push_profile": {
            "slug": "jane-doe",
            "name": "Jane Doe",
            "level": "full",
            "indexed": 0,
        },
        "researcher_profiles.client.install_profile": {
            "status": "installed",
            "slug": "jane-doe",
            "path": "/tmp/jane-doe",
            "name": "Jane Doe",
            "level": "full",
        },
        "researcher_profiles.schema_export.export_schemas": [],
        "researcher_profiles.schema_export.export_wire_schema": "wire.json",
        "researcher_profiles.validate.validate_profile_dir": ProfileValidationReport(
            profile_dir="P", validated_at="", schema_fingerprint="f" * 20, package_version="0"
        ),
        "researcher_profiles.client.list_registry": [
            RegistryListing(
                "https://r",
                ({"slug": "a", "name": "A", "level": "full", "paper_count": 1},),
                None,
            )
        ],
    }


class TestWiring:
    """``rp foo --bar X`` reaches the library as ``bar=X``. Nothing re-run."""

    @pytest.mark.parametrize(
        "argv,target,exp_args,exp_kwargs",
        [
            (
                ["index", "{P}", "--force", "--backend", "st:x"],
                "researcher_profiles.embeddings.build_index",
                (),
                {"force": True, "backend": "st:x"},
            ),
            (
                ["export-embeddings", "{P}"],
                "researcher_profiles.embeddings.write_flat_export",
                ("{P}",),
                {},
            ),
            (
                ["render", "{P}", "--base-url", "U", "--no-index"],
                "researcher_profiles.publish.render_profile",
                ("{P}",),
                {"base_url": "U", "no_index": True},
            ),
            (
                ["site", "{P}", "-o", "O", "--base-url", "U", "--no-index", "--now", "T"],
                "researcher_profiles.publish.build_site",
                ("{P}", "O"),
                {"base_url": "U", "no_index": True, "now": "T"},
            ),
            (
                ["push", "{P}", "--url", "U", "--slug", "S", "--token", "T", "--include-fulltext"],
                "researcher_profiles.client.push_profile",
                ("U", "{P}"),
                {"slug": "S", "token": "T", "include_fulltext": True},
            ),
            (
                ["install", "a", "--url", "U", "--root", "R", "--token", "T", "--force"],
                "researcher_profiles.client.install_profile",
                ("a",),
                {"url": "U", "root": "R", "token": "T", "force": True},
            ),
            (
                ["schema", "export", "D"],
                "researcher_profiles.schema_export.export_schemas",
                ("D",),
                {},
            ),
            (
                ["schema", "export-wire", "F"],
                "researcher_profiles.schema_export.export_wire_schema",
                ("F",),
                {},
            ),
            (
                ["validate", "{P}"],
                "researcher_profiles.validate.validate_profile_dir",
                ("{P}",),
                {},
            ),
        ],
        ids=[
            "index-force-backend",
            "export-embeddings-path",
            "render-flags",
            "site-flags",
            "push-flags",
            "install-flags",
            "schema-export",
            "schema-export-wire",
            "validate-path",
        ],
    )
    def test_args_reach_the_library(
        self, argv, target, exp_args, exp_kwargs, monkeypatch, jane_doe_dir
    ):
        def sub(s):
            return s.replace("{P}", str(jane_doe_dir))

        calls = _spy(monkeypatch, target, _results()[target])
        assert main([sub(a) for a in argv]) == 0
        assert len(calls) == 1
        # A ``profile`` positional reaches the library already resolved to a
        # Path (it may have been a rid or a slug), so compare the spelling
        # rather than the type: every one of these targets takes ``str | Path``.
        assert tuple(str(a) for a in calls[0].args[: len(exp_args)]) == tuple(
            sub(a) for a in exp_args
        )
        assert exp_kwargs.items() <= calls[0].kwargs.items()

    def test_install_loops_over_every_slug(self, monkeypatch):
        # `nargs="+"` exists for exactly this: one install call per slug.
        target = "researcher_profiles.client.install_profile"
        calls = _spy(monkeypatch, target, _results()[target])
        assert main(["install", "a", "b", "--url", "U"]) == 0
        assert [c.args[0] for c in calls] == ["a", "b"]

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["-k", "3", "--type", "paper"], {"k": 3, "filter": {"source_type": "paper"}}),
            ([], {"k": 5, "filter": None}),
        ],
        ids=["k-and-type-filter", "defaults"],
    )
    def test_search_options_become_index_arguments(self, argv, expected, monkeypatch, jane_doe_dir):
        calls: list[dict] = []

        class _FakeIndex:
            def __init__(self, path):
                self.path = path

            def search(self, query, **kwargs):
                calls.append({"query": query, **kwargs})
                return []

        monkeypatch.setattr(
            importlib.import_module("researcher_profiles.embeddings"),
            "SqliteEmbeddingIndex",
            _FakeIndex,
        )
        assert main(["search", str(jane_doe_dir), "q", *argv]) == 0
        assert calls == [{"query": "q", **expected}]


class TestProfileRef:
    """A ``profile`` positional takes a path, a rid, or a slug.

    ``rp install`` prints a slug and leaves the directory under the cache, so
    the slug is the handle a caller has when the next command runs. Every verb
    that takes a profile resolves it the same way; these rows are the contract,
    not a re-test of the verbs (``validate`` and ``manifest`` stand in because
    they need nothing but the tree).
    """

    @pytest.mark.parametrize("verb", ["validate", "manifest"])
    @pytest.mark.parametrize(
        "ref",
        ["jane-doe", "0000-0002-1825-0097"],
        ids=["slug", "rid"],
    )
    def test_ref_resolves_against_the_cache(self, verb, ref, cases):
        assert main([verb, ref, "--root", str(cases.root)]) == 0

    def test_a_real_path_wins_over_a_same_named_cache_entry(self, cases, monkeypatch, capsys):
        """A directory that exists is itself, never the cache entry it is named after."""
        monkeypatch.chdir(cases.root)
        assert main(["validate", "jane-doe", "--root", str(cases.root), "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert Path(report["profile_dir"]).resolve() == (cases.root / "jane-doe").resolve()

    def test_a_miss_is_a_usage_error_that_names_the_root(self, cases, capsys):
        assert main(["validate", "nope-nobody", "--root", str(cases.root)]) == 2
        err = capsys.readouterr().err
        assert "no profile at 'nope-nobody'" in err
        assert str(cases.root) in err


class _Cases:
    """Cheap on-disk material for the tables below, built only when named."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path

    def argv(self, argv: list[str]) -> list[str]:
        """Expand ``{good}``/``{root}``/... in a row into real paths."""
        return [re.sub(r"\{(\w+)}", lambda m: str(getattr(self, m[1])), a) for a in argv]

    @cached_property
    def good(self) -> Path:
        return copy_fixture("jane-doe", self.tmp / "good")

    @cached_property
    def broken(self) -> Path:
        d = copy_fixture("jane-doe", self.tmp / "broken")
        break_papers(d)
        return d

    @cached_property
    def root(self) -> Path:
        root = self.tmp / "root"
        copy_fixture("jane-doe", root)
        return root

    @cached_property
    def empty(self) -> Path:
        d = self.tmp / "empty"
        d.mkdir()
        return d

    @cached_property
    def missing(self) -> Path:
        return self.tmp / "no-such-directory"

    @cached_property
    def stale(self) -> Path:
        """One whose manifest still lists a file that is now gone."""
        d = copy_fixture("jane-doe", self.tmp / "stale")
        (d / "personality" / "expertise.md").unlink()
        return d


@pytest.fixture
def cases(tmp_path, monkeypatch) -> _Cases:
    # conftest's autouse isolation hides ~/.config/rp and $RESEARCHER_PROFILES_ROOT but
    # not these two, and an operator with either exported would see the
    # "no registry" rows fail for an unrelated reason.
    monkeypatch.delenv("RESEARCHER_PROFILES_REGISTRY_URL", raising=False)
    monkeypatch.delenv("RESEARCHER_PROFILES_TOKEN", raising=False)
    return _Cases(tmp_path)


class TestExitCodes:
    """The documented table: 0 ok, 1 error, 2 usage, 4 validation/contract."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["where", "jane-doe", "--root", "{root}"], 0),
            (["where", "nope", "--root", "{root}"], 2),
            (["seek", "jane-doe", "--root", "{root}"], 0),
            (["seek", "nope", "--root", "{root}"], 2),
            (["list", "--root", "{root}"], 0),
            (["list", "--root", "{empty}"], 0),  # an empty cache is not an error
            (["validate", "{good}"], 0),
            (["validate", "{broken}"], 4),
            (["manifest", "{good}"], 0),
            (["manifest", "{stale}", "--check"], 4),
            (["manifest", "{missing}"], 2),
            (["render", "{missing}"], 2),
            (["push", "{missing}", "--url", "http://x"], 2),
            (["listr"], 2),
            (["install", "jane-doe"], 2),
            (["mint-local-id", "--name", "Elena Voss"], 0),
            (["schema", "export", "{empty}"], 0),
            (["sign", "{empty}"], 2),
            (["sign-verify", "{good}"], 2),
        ],
        ids=[
            "where-hit",
            "where-miss",
            "seek-hit",
            "seek-miss",
            "list-populated",
            "list-empty",
            "validate-ok",
            "validate-broken",
            "manifest-ok",
            "manifest-check-drift",
            "manifest-missing-dir",
            "render-missing-dir",
            "push-missing-dir",
            "listr-no-url",
            "install-no-registry",
            "mint-local-id",
            "schema-export",
            "sign-no-profile",
            "sign-verify-no-jwks",
        ],
    )
    def test_exit_code(self, argv, expected, cases):
        assert main(cases.argv(argv)) == expected


class TestMachineOutput:
    """What a machine parses: the ``--json`` key sets, and a clean stdout."""

    @pytest.mark.parametrize(
        "argv,expected_keys",
        [
            (["list", "--root", "{root}", "--json"], {"root", "profiles"}),
            (["where", "jane-doe", "--root", "{root}", "--json"], {"ref", "path", "root"}),
            (["seek", "jane-doe", "--root", "{root}", "--json"], {"slug", "path", "root"}),
            (["manifest", "{good}", "--json"], {"profile", "entries", "drift"}),
        ],
        ids=["list", "where", "seek", "manifest"],
    )
    def test_top_level_keys(self, argv, expected_keys, cases, capsys):
        assert main(cases.argv(argv)) == 0
        assert set(json.loads(capsys.readouterr().out)) == expected_keys

    def test_manifest_json_reports_drift_by_kind(self, cases, capsys):
        main(cases.argv(["manifest", "{good}", "--json"]))
        drift = json.loads(capsys.readouterr().out)["drift"]
        assert set(drift) == {"missing", "stale"}

    def test_install_json_is_one_record_per_requested_slug(self, monkeypatch, capsys):
        target = "researcher_profiles.client.install_profile"
        _spy(monkeypatch, target, _results()[target])
        assert main(["install", "a", "b", "--url", "U", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 2

    def test_listr_json_record_names_the_answering_registry(self, monkeypatch, capsys):
        target = "researcher_profiles.client.list_registry"
        _spy(monkeypatch, target, _results()[target])
        assert main(["listr", "--url", "https://r", "--json"]) == 0
        records = json.loads(capsys.readouterr().out)
        assert records == [
            {"registry": "https://r", "slug": "a", "name": "A", "level": "full", "paper_count": 1}
        ]

    @pytest.mark.parametrize(
        "argv,stderr_marker",
        [
            (["list", "--root", "{empty}", "--json"], "Next:"),
            (["manifest", "{stale}", "--json"], "STALE in manifest"),
        ],
        ids=["list-empty-root-note", "manifest-drift"],
    )
    def test_diagnostics_never_contaminate_a_json_stdout(self, argv, stderr_marker, cases, capsys):
        # Two source comments promise this and nothing enforced it: `rp ...
        # --json | jq` must not break because the command had something to say.
        main(cases.argv(argv))
        captured = capsys.readouterr()
        json.loads(captured.out)
        assert stderr_marker in captured.err


class TestErrorHandling:
    """A failure is a message and a code, never a traceback."""

    def test_push_to_an_unreachable_server(self, jane_doe_dir, capsys):
        # A real closed port, not a spy: the point is that the CLI, not the
        # client library, owns the top-level catch. httpx.ConnectError is
        # none of (FileNotFoundError, PermissionError, RuntimeError).
        assert main(["push", str(jane_doe_dir), "--url", "http://127.0.0.1:9/x"]) == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_search_without_an_index(self, jane_doe_dir, capsys):
        assert main(["search", str(jane_doe_dir), "q"]) == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err and "rp index" in err

    def test_index_with_an_unknown_backend(self, jane_doe_dir, capsys):
        assert main(["index", str(jane_doe_dir), "--backend", "nonsense:1"]) == 2
        assert "Traceback" not in capsys.readouterr().err

    def test_listr_when_every_registry_is_down(self, cases, capsys):
        # Exit 0 here is indistinguishable from "the registry is empty".
        assert main(["listr", "--url", "http://127.0.0.1:9"]) == 1
        assert "Traceback" not in capsys.readouterr().err


class TestManagementClientDispatch:
    """The CLI selects the ManagementClient resource, not its HTTP shape."""

    def _install(self, monkeypatch, *, fail=None):
        from researcher_profiles.cli.auth import agent

        credential = agent.Credential(
            "rpa_test", "https://example.test", "test", profile="jane-doe"
        )
        calls = []

        class FakeClient:
            def __init__(self, _credential):
                self.identity = SimpleNamespace(
                    whoami=lambda: calls.append("whoami") or {"principal": {}, "scopes": []},
                    scopes=lambda: calls.append("scopes") or {},
                )
                self.profile = SimpleNamespace(
                    get=lambda slug: calls.append(("get", slug)) or {"metadata": {}},
                    patch_visibility=lambda slug, **kw: (
                        (_ for _ in ()).throw(fail)
                        if fail
                        else calls.append(("visibility", slug, kw))
                    ),
                )

        monkeypatch.setattr(agent, "resolve_credential", lambda **_: credential)
        monkeypatch.setattr(agent, "ManagementClient", FakeClient)
        return calls

    def test_agent_identity_subcommands_dispatch_to_identity(self, monkeypatch, capsys):
        calls = self._install(monkeypatch)
        assert main(["agent", "whoami", "--json"]) == 0
        assert main(["agent", "scopes", "--json"]) == 0
        assert calls == ["whoami", "scopes"]
        assert '"principal"' in capsys.readouterr().out

    def test_profile_read_dispatches_to_profile_resource(self, monkeypatch, capsys):
        calls = self._install(monkeypatch)
        assert main(["profile", "visibility", "get"]) == 0
        assert calls == [("get", "jane-doe")]
        assert "visibility:" in capsys.readouterr().out

    def test_profile_pull_reads_the_flat_manifest_list(self, monkeypatch, tmp_path, capsys):
        """``manifest`` is a flat ``list[dict]``, not a ``{"entries": [...]}`` envelope.

        That is what ``models.api.ProfileDetail`` serves and what the spec's
        ``GET /profiles/{slug}`` table declares, so the soul fetch must find
        its entry by iterating the list directly.
        """
        import httpx

        from researcher_profiles.cli.auth import agent

        credential = agent.Credential(
            "rpa_test", "https://profiles.example.org", "test", profile="jane-doe"
        )
        detail = {
            "slug": "jane-doe",
            "rid": "0000-0002-1825-0097",
            "metadata": {"name": "Jane Doe"},
            "content_hash": "sha256:abc",
            "manifest": [
                {"content_url": "papers.json", "role": "papers"},
                {"content_url": "personality/SOUL.md", "role": "soul"},
            ],
        }

        class FakeClient:
            def __init__(self, _credential):
                self.profile = SimpleNamespace(get=lambda slug: detail)

        asked: list[str] = []

        def fake_get(url, headers=None, **kw):
            asked.append(url)
            return SimpleNamespace(raise_for_status=lambda: None, text="I study barnacles.\n")

        monkeypatch.setattr(agent, "resolve_credential", lambda **_: credential)
        monkeypatch.setattr(agent, "ManagementClient", FakeClient)
        monkeypatch.setattr(httpx, "get", fake_get)

        out = tmp_path / "profile.md"
        assert main(["profile", "pull", "-o", str(out)]) == 0
        assert asked == [
            "https://profiles.example.org/api/v1/profiles/jane-doe/content/personality/SOUL.md"
        ]
        body = out.read_text()
        assert "I study barnacles." in body
        assert "base_hash: sha256:abc" in body

    def test_profile_write_preserves_agent_error_exit(self, monkeypatch, capsys):
        from researcher_profiles.cli.auth.agent import AgentAPIError

        calls = self._install(monkeypatch, fail=AgentAPIError(403, "denied"))
        assert main(["profile", "visibility", "set", "--tier", "internal"]) == 1
        assert calls == []
        assert "Error: denied" in capsys.readouterr().err


class TestPluginSeam:
    """How an installed distribution re-attaches its own verbs to ``rp``."""

    def test_plugin_handler_is_dispatched(self, monkeypatch):
        seen: list[argparse.Namespace] = []

        def _register(sub):
            p = sub.add_parser("fake")
            p.add_argument("--n", default=None)
            p.set_defaults(_plugin_handler=lambda a: (seen.append(a), 7)[1])

        monkeypatch.setattr("researcher_profiles.cli._root._load_cli_plugins", _register)
        assert main(["fake", "--n", "3"]) == 7
        assert seen[0].n == "3"

    def test_a_broken_plugin_does_not_break_rp(self, monkeypatch, cases):
        # The bare `except Exception: continue` in _load_cli_plugins is the
        # guarantee under test. Pin it so nobody tidies it up.
        class _Boom:
            name = "boom"

            def load(self):
                raise ImportError("no such module")

        monkeypatch.setattr("importlib.metadata.entry_points", lambda **kw: [_Boom()])
        assert main(cases.argv(["list", "--root", "{empty}"])) == 0
