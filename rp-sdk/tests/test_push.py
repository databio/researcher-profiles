"""The push and archive surface: ``api/routers/push.py`` and ``api/upload.py``.

``PUT /api/v1/profiles/{slug}`` (tarball and JSON), ``GET .../archive``,
``/capabilities``, and the spec-whitelist archive builder. The client side of
push (``push_profile``) is tested in test_client.py.
"""

import io
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from researcher_profiles.client import (
    push_profile,
)
from researcher_profiles.store.sql import SqlProfileStore

from .factories import (
    FIXTURE_DIR,
    add_fulltext_pdf,
    tar_names,
)

SLUG = "jane-doe"


OTHER = "john-smith"


def _tar_with_symlink() -> bytes:
    """A tarball whose second member is a symlink escaping to /etc/passwd."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"name: X\n"
        info = tarfile.TarInfo(name="profile.jsonld")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(name="escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    return buf.getvalue()


def _tar_of_dir(src: Path) -> bytes:
    """Gzipped tar of a directory's CONTENTS (``profile.jsonld`` at tar root)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(src.iterdir()):
            tf.add(child, arcname=child.name)
    return buf.getvalue()


def _tar_of_members(members) -> bytes:
    """Gzipped tar built member-by-member, so a test can plant a hostile name."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestPush:
    """Tests for the profile push endpoint (PUT /api/v1/profiles/{slug}).

    Covers: push -> live listing roundtrip, overwrite of an existing profile,
    traversal/symlink rejection, the size cap, bad-archive handling, and the
    critical invalidation behavior: a push drops the cached registry snapshot so
    a subsequent /match rebuilds over the new profile set.
    """

    # ----------------------------------------------------------------------
    # Roundtrip + overwrite
    # ----------------------------------------------------------------------

    def test_push_new_profile_appears_live(self, api_client):
        r = api_client.put(f"/api/v1/profiles/{OTHER}", content=_tar_of_dir(FIXTURE_DIR / OTHER))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["slug"] == OTHER
        assert body["name"]
        assert body["indexed"] is False  # fixture has no embeddings.sqlite

        slugs = [x["slug"] for x in api_client.get("/api/v1/profiles").json()["profiles"]]
        assert OTHER in slugs
        assert api_client.get(f"/api/v1/profiles/{OTHER}").status_code == 200

    def test_push_overwrites_existing_profile(
        self, api_client, fixture_profiles_root, fixture_profile
    ):
        root = fixture_profiles_root(SLUG)
        # Warm the cache with the original profile.
        orig_name = api_client.get(f"/api/v1/profiles/{SLUG}").json()["fields"]["name"]

        staged = fixture_profile(SLUG)
        py = (staged / "profile.jsonld").read_text()
        assert orig_name in py
        (staged / "profile.jsonld").write_text(py.replace(orig_name, "Renamed Person"))

        r = api_client.put(f"/api/v1/profiles/{SLUG}", content=_tar_of_dir(staged))
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Renamed Person"
        # The cached profile object was evicted -> reads reflect the new content.
        d = api_client.get(f"/api/v1/profiles/{SLUG}").json()
        assert d["fields"]["name"] == "Renamed Person"
        # No leftover staging/backup dirs in the profiles root or the listing.
        leftovers = [p.name for p in root.iterdir() if p.name.startswith((".upload-", ".old-"))]
        assert leftovers == []

    # ----------------------------------------------------------------------
    # Rejection paths
    # ----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "slug, make_body, detail",
        [
            ("Bad_Slug", lambda: _tar_of_dir(FIXTURE_DIR / OTHER), "invalid slug"),
            (OTHER, lambda: _tar_of_members({"notes.md": b"hello"}), "profile.jsonld"),
            (
                OTHER,
                lambda: _tar_of_members({"profile.jsonld": b"name: X\n", "../evil.txt": b"pwned"}),
                "traversal",
            ),
            (
                OTHER,
                lambda: _tar_of_members(
                    {"profile.jsonld": b"name: X\n", "/tmp/evil.txt": b"pwned"}
                ),
                None,
            ),
            (OTHER, _tar_with_symlink, "link"),
            (OTHER, lambda: b"not a tarball", None),
        ],
        ids=[
            "bad-slug",
            "missing-profile-document",
            "path-traversal",
            "absolute-path",
            "symlink-member",
            "garbage-body",
        ],
    )
    def test_push_rejects_bad_upload(self, api_client, slug, make_body, detail):
        """Every malformed or hostile upload is a 400, never a 5xx or a partial write."""
        r = api_client.put(f"/api/v1/profiles/{slug}", content=make_body())
        assert r.status_code == 400
        if detail is not None:
            assert detail in r.json()["detail"]

    def test_push_size_cap(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG), max_upload_bytes=1024)
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 413

    def test_push_requires_token_when_configured(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG), token="sekrit")
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 401
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
            headers={"Authorization": "Bearer sekrit"},
        )
        assert r.status_code == 200

    def test_failed_push_leaves_existing_profile_intact(self, api_client):
        orig = api_client.get(f"/api/v1/profiles/{SLUG}").json()
        r = api_client.put(
            f"/api/v1/profiles/{SLUG}",
            content=_tar_of_members({"profile.jsonld": b": not [valid yaml\n"}),
        )
        assert r.status_code == 400
        assert api_client.get(f"/api/v1/profiles/{SLUG}").json() == orig

    # ----------------------------------------------------------------------
    # Registry invalidation (the critical one)
    # ----------------------------------------------------------------------

    def test_push_moves_the_write_generation_and_drops_disk_caches(
        self, api_client, fixture_profiles_root
    ):
        """A profile pushed AFTER a /match must appear in subsequent matches.

        There is no snapshot on ``app.state`` to drop any more. The analytics
        cache on the store, stamped with its write generation, so the push has
        to move that generation (which is what rebuilds the roster) and take
        the on-disk ``.cache`` memos with it.
        """
        from researcher_profiles.api.deps import get_store

        reg_dir = fixture_profiles_root(SLUG) / ".cache"
        reg_dir.mkdir()
        (reg_dir / "centroids.npz").write_bytes(b"stale")
        (reg_dir / "topics.json").write_text("{}")

        app = api_client.app
        store = get_store(SimpleNamespace(app=app))
        before = store.generation
        roster = store._rostered()
        assert roster.slugs == [SLUG]

        r = api_client.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 200
        assert store.generation > before
        # Nobody called invalidate: the stamp moved, so the roster rebuilt.
        assert store._rostered() is not roster
        assert store._rostered().slugs == sorted([SLUG, OTHER])
        assert not (reg_dir / "centroids.npz").exists()
        assert not (reg_dir / "topics.json").exists()

    # ----------------------------------------------------------------------
    # Push goes through the spec-whitelist archive builder
    # ----------------------------------------------------------------------

    def test_push_strips_non_spec_html(self, api_client, fixture_profile):
        """A sources/html/ scrape on disk never reaches the server via push."""
        src = fixture_profile(OTHER)
        html_dir = src / "sources" / "html"
        html_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / "big.html").write_text("<html>" + "x" * 200000 + "</html>")

        result = push_profile("http://testserver", src, client=api_client)
        assert result.summary["slug"] == OTHER
        # ...and the preflight said so before the bytes moved.
        assert result.plan.dropped["not_in_spec"] == ["sources/html/big.html"]

        # The pushed-and-swapped profile on the server carries no sources/html/.
        server_profile = api_client.app.state.store.root / OTHER
        assert not (server_profile / "sources" / "html").exists()
        assert (server_profile / "profile.jsonld").is_file()


_LOCAL_RID_RE = re.compile(r"^local:[a-z0-9][a-z0-9-]*-[0-9a-f]{6}$")


class TestJsonUpsertAndMint:
    """``PUT /api/v1/profiles/{slug}`` with ``Content-Type: application/json``.

    The tarball push (``TestPush``) shares this URL; a JSON body is dispatched
    to the document-only upsert instead. Covers create/replace, validation and
    slug guards, ``If-Match`` optimistic concurrency, and server-side ``local:``
    rid minting. Backend-agnostic, so served over a fresh filesystem store.
    """

    def test_json_create_then_replace(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        body = {"name": "Ada Lovelace", "rid": "local:ada-a1b2c3", "provenance": "synthetic"}
        r = c.put("/api/v1/profiles/ada", json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        # PushResponse shape.
        assert payload["slug"] == "ada"
        assert payload["rid"] == "local:ada-a1b2c3"
        assert payload["name"] == "Ada Lovelace"
        assert "level" in payload
        assert payload["indexed"] is False  # JSON upsert never carries an index
        assert c.get("/api/v1/profiles/ada").json()["fields"]["name"] == "Ada Lovelace"

        # Replace: same slug + rid, new name.
        body["name"] = "Ada, Countess of Lovelace"
        r = c.put("/api/v1/profiles/ada", json=body)
        assert r.status_code == 200, r.text
        assert c.get("/api/v1/profiles/ada").json()["fields"]["name"] == (
            "Ada, Countess of Lovelace"
        )

    def test_json_invalid_document_is_422(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        # A rid is present (so the mint 400 does not fire), but it is malformed,
        # so ProfileDocument validation rejects it.
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "Ada", "rid": "not-a-valid-rid", "provenance": "synthetic"},
        )
        assert r.status_code == 422, r.text

    def test_json_bad_slug_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/Bad_Slug",
            json={"name": "Ada", "rid": "local:ada-a1b2c3", "provenance": "synthetic"},
        )
        assert r.status_code == 400, r.text

    def test_if_match_conflict_returns_409_with_current_hash(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        body = {"name": "Ada Lovelace", "rid": "local:ada-a1b2c3", "provenance": "synthetic"}
        assert c.put("/api/v1/profiles/ada", json=body).status_code == 200
        current = c.get("/api/v1/profiles/ada").json()["content_hash"]

        body["name"] = "Someone Else"
        r = c.put(
            "/api/v1/profiles/ada",
            json=body,
            headers={"If-Match": "sha256:stale-and-wrong"},
        )
        assert r.status_code == 409, r.text
        assert r.headers["X-RP-Content-Hash"] == current

        # A matching If-Match is accepted.
        r = c.put("/api/v1/profiles/ada", json=body, headers={"If-Match": current})
        assert r.status_code == 200, r.text

    def test_mint_local_rid_json_flag(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "Ada Lovelace", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert _LOCAL_RID_RE.match(r.json()["rid"])

    def test_mint_local_rid_query_param(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/grace?mint=local",
            json={"name": "Grace Hopper", "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert _LOCAL_RID_RE.match(r.json()["rid"])

    def test_no_rid_without_opt_in_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put("/api/v1/profiles/ada", json={"name": "Ada", "provenance": "synthetic"})
        assert r.status_code == 400, r.text

    def test_mint_with_empty_name_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 400, r.text

    def test_rid_present_and_mint_flag_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={
                "name": "Ada",
                "rid": "local:ada-a1b2c3",
                "mintLocalRid": True,
                "provenance": "synthetic",
            },
        )
        assert r.status_code == 400, r.text

    def test_a_non_json_body_still_takes_the_tarball_path(self, make_api_client, tmp_path):
        """Content-Type dispatch must not break the tarball push: a gzipped-tar
        body (no application/json) still ingests as a bundle."""
        c = make_api_client(tmp_path)
        r = c.put(f"/api/v1/profiles/{OTHER}", content=_tar_of_dir(FIXTURE_DIR / OTHER))
        assert r.status_code == 200, r.text
        assert c.get(f"/api/v1/profiles/{OTHER}").status_code == 200


class TestArchive:
    """The archive endpoint and the spec-whitelist archive builder."""

    @pytest.fixture
    def server_root(self, fixture_profiles_root) -> Path:
        """A served profiles root whose one profile carries a copyrighted PDF."""
        root = fixture_profiles_root(SLUG)
        add_fulltext_pdf(root / SLUG)
        return root

    def test_archive_endpoint_headers(self, make_api_client, server_root):
        http = make_api_client(server_root)
        resp = http.get(f"/api/v1/profiles/{SLUG}/archive")
        assert resp.status_code == 200
        assert resp.headers["X-RP-Archive-Digest"]
        assert resp.headers["X-RP-Archive-Tier"] == "public"
        assert "attachment" in resp.headers["content-disposition"]

    def test_archive_404_for_unknown_slug(self, make_api_client, server_root):
        http = make_api_client(server_root)
        assert http.get("/api/v1/profiles/nobody/archive").status_code == 404

    # ----------------------------------------------------------------------
    # Spec-whitelist archive builder (build_profile_archive)
    # ----------------------------------------------------------------------

    def test_archive_drops_non_spec_members(self, fixture_profile) -> None:
        """A stale sources/html/ (and other cruft) never reaches the tarball."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)
        # Scrape residue + assorted non-spec cruft on disk.
        (src / "sources" / "html").mkdir(parents=True, exist_ok=True)
        (src / "sources" / "html" / "big.html").write_text("<html>" + "x" * 100000 + "</html>")
        (src / "sources" / "works.json").write_text("[]")  # non-spec file
        (src / "scratch.txt").write_text("not a profile member")

        names = tar_names(build_profile_archive(src, include_fulltext=True).data)

        # No non-spec member survives.
        assert not any(n.startswith("sources/html") for n in names)
        assert "sources/works.json" not in names
        assert "scratch.txt" not in names
        # Documented members are kept.
        assert "profile.jsonld" in names
        # Pushing a profile publishes the RECORD, not the build: the sidecar with
        # download attempts, rejection reasons, and verification bookkeeping never
        # leaves this machine.
        assert "meta/build_state.json" not in names
        assert not any(
            n.startswith("meta/") and n != "meta" for n in names if n != "meta/embeddings.sqlite"
        )
        assert any(n.startswith("sources/papers/") for n in names)
        assert "sources/papers.jsonld" in names
        assert any(n.startswith("sources/summaries/") for n in names)

    @pytest.mark.parametrize(
        "archive_kwargs, expect_papers",
        [
            ({"include_fulltext": True}, True),
            ({"include_fulltext": False}, False),
            # The shared builder's *default* is the safe one, not the permissive
            # one. Every archive that leaves this machine is built here; a caller
            # who says nothing about fulltext must not end up redistributing it.
            ({}, False),
        ],
        ids=["include-fulltext", "exclude-fulltext", "default-no-kwarg"],
    )
    def test_archive_fulltext_gate(self, fixture_profile, archive_kwargs, expect_papers) -> None:
        """The gate is copyright-scoped: it drops sources/papers/ and nothing else."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)

        names = tar_names(build_profile_archive(src, **archive_kwargs).data)

        assert any(n.startswith("sources/papers/") for n in names) is expect_papers
        # Scoped to sources/papers/ only; summaries + metadata always remain.
        assert "profile.jsonld" in names
        assert "sources/papers.jsonld" in names
        assert any(n.startswith("sources/summaries/") for n in names)

    def test_archive_reports_what_it_dropped(self, fixture_profile) -> None:
        """Every exclusion is named, by reason. Silent is how a user loses work."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)
        (src / "sources" / "html").mkdir(parents=True, exist_ok=True)
        (src / "sources" / "html" / "scrape.html").write_text("<html></html>")
        (src / "scratch.txt").write_text("not a profile member")
        (src / "cache").mkdir()  # pre-rename .cache/
        (src / "cache" / "embeddings.sqlite").write_bytes(b"not really sqlite")
        (src / ".git").mkdir()
        (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

        archive = build_profile_archive(src)

        assert archive.dropped["legacy_cache"] == ["cache/embeddings.sqlite"]
        assert archive.dropped["fulltext"] == [
            "sources/papers/doe2016example.md",
            "sources/papers/doe2019methods.md",
        ]
        not_in_spec = archive.dropped["not_in_spec"]
        assert "sources/html/scrape.html" in not_in_spec
        assert "scratch.txt" in not_in_spec
        # A non-member top-level directory is reported as itself, not walked:
        # a profile that is also a git checkout must not drown the report.
        assert ".git/" in not_in_spec
        assert ".git/HEAD" not in not_in_spec
        # members is what the tarball actually holds, and agrees with it.
        assert archive.members == tar_names(archive.data)
        assert "profile.jsonld" in archive.members
        assert not (set(archive.members) & set(not_in_spec))

    def test_archive_only_ships_the_named_files(self, fixture_profile) -> None:
        """``only=`` is the document plus exactly what was asked for."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)

        archive = build_profile_archive(src, only=["sources/papers.jsonld"])

        assert tar_names(archive.data) == {"profile.jsonld", "sources/papers.jsonld"}
        assert archive.members == {"profile.jsonld", "sources/papers.jsonld"}
        assert archive.dropped == {}  # nothing was dropped; the rest was not asked for

    @pytest.mark.parametrize(
        "only, match",
        [
            (["sources/nope.jsonld"], "not a file in the profile"),
            (["scratch.txt"], "not a profile member"),
            (["sources/papers/doe2016example.md"], "include_fulltext"),
        ],
        ids=["missing", "not-a-member", "withheld-fulltext"],
    )
    def test_archive_only_refuses_a_path_it_cannot_ship(self, fixture_profile, only, match) -> None:
        """Naming a file and watching it vanish is the bug ``only=`` replaces."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)
        (src / "scratch.txt").write_text("not a profile member")

        with pytest.raises(ValueError, match=match):
            build_profile_archive(src, only=only)


class TestPushCopyrightBoundary:
    """The server must not rely on the client: ingest trims fulltext unless the operator opts in."""

    @pytest.fixture
    def push_src(self, fixture_profile) -> Path:
        """A local profile directory carrying a copyrighted fulltext PDF."""
        src = fixture_profile(SLUG)
        add_fulltext_pdf(src)
        return src

    @pytest.fixture
    def push_target(self, tmp_path: Path) -> Path:
        root = tmp_path / "push-target"
        root.mkdir()
        return root

    @pytest.mark.parametrize(
        "client_kwargs, expect_stored_fulltext",
        [
            # Server-side enforcement: a client that sends fulltext anyway is
            # trimmed. The boundary must not depend on the client behaving.
            ({}, False),
            # accept_fulltext=True is the operator's call: what the registry STORES.
            ({"accept_fulltext": True}, True),
        ],
        ids=["strips-on-ingest", "accepts-when-operator-opts-in"],
    )
    def test_server_ingest_fulltext_gate(
        self,
        make_api_client,
        push_src: Path,
        push_target: Path,
        client_kwargs,
        expect_stored_fulltext,
    ) -> None:
        """What the server keeps on ingest is the operator's call, not the client's."""
        from researcher_profiles.api.upload import build_profile_archive

        payload = build_profile_archive(push_src, include_fulltext=True).data
        assert "sources/papers/paper-001.pdf" in tar_names(payload)  # client sent it

        http = make_api_client(push_target, **client_kwargs)
        resp = http.put(
            f"/api/v1/profiles/{SLUG}",
            content=payload,
            headers={"Content-Type": "application/gzip"},
        )

        assert resp.status_code == 200
        stored = push_target / SLUG
        pdf = stored / "sources" / "papers" / "paper-001.pdf"
        if expect_stored_fulltext:
            assert pdf.is_file()
        else:
            assert not pdf.exists()
        assert (stored / "profile.jsonld").is_file()
        # Non-fulltext members survive either way.
        assert (stored / "sources" / "papers.jsonld").is_file()


# The jane-doe fixture's extracted paper text, profile-relative.
_JANE_FULLTEXT = ("sources/papers/doe2016example.md", "sources/papers/doe2019methods.md")
_INDEX = ".cache/embeddings.sqlite"


def _put_archive(http, payload: bytes, **params):
    return http.put(
        f"/api/v1/profiles/{SLUG}",
        content=payload,
        headers={"Content-Type": "application/gzip"},
        params=params or None,
    )


def _stored_files(server_root: Path) -> set[str]:
    """Every profile-relative file the server's live directory holds."""
    live = server_root / SLUG
    return {p.relative_to(live).as_posix() for p in live.rglob("*") if p.is_file()}


class TestPushKeepsWithheld:
    """A push is a filtered view of the sender's directory, not the whole of it.

    ``build_profile_archive`` withholds fulltext by default, so absence from
    the tarball must not read as "delete": under the default ``?mode=replace``
    the server keeps the fulltext and index files it already holds when the
    archive carries none of that class. ``?mode=merge`` keeps every omitted
    file; ``?mode=prune`` keeps none.
    """

    @pytest.fixture
    def server_root(self, tmp_path: Path) -> Path:
        # Not ``tmp_path`` itself: ``fixture_profile`` writes the sender's copy
        # there, and the server's live directory must be a different tree.
        root = tmp_path / "server"
        root.mkdir()
        return root

    @pytest.fixture
    def src(self, fixture_profile) -> Path:
        """A local jane-doe carrying fulltext and a (fake) built index."""
        src = fixture_profile(SLUG, drop_index=True)
        cache = src / ".cache"
        cache.mkdir(exist_ok=True)
        (cache / "embeddings.sqlite").write_bytes(b"not really sqlite")
        return src

    @pytest.fixture
    def full_push(self, src: Path) -> bytes:
        """Push A: everything, fulltext and index included."""
        from researcher_profiles.api.upload import build_profile_archive

        payload = build_profile_archive(src, include_fulltext=True).data
        names = tar_names(payload)
        assert set(_JANE_FULLTEXT) <= names and _INDEX in names
        return payload

    @pytest.fixture
    def only_push(self, src: Path) -> bytes:
        """Push C: the ``rp push --only sources/papers.jsonld`` view."""
        from researcher_profiles.api.upload import build_profile_archive

        payload = build_profile_archive(src, only=["sources/papers.jsonld"]).data
        assert tar_names(payload) == {"profile.jsonld", "sources/papers.jsonld"}
        return payload

    @pytest.fixture
    def bare_push(self, src: Path) -> bytes:
        """Push B: the default ``rp push`` view, no fulltext and no index."""
        import shutil

        from researcher_profiles.api.upload import build_profile_archive

        shutil.rmtree(src / ".cache")
        payload = build_profile_archive(src, include_fulltext=False).data
        names = tar_names(payload)
        assert not any(n.startswith("sources/papers/") for n in names)
        assert _INDEX not in names
        return payload

    def test_a_bare_push_keeps_the_live_fulltext_and_index(
        self, make_api_client, server_root, full_push, bare_push
    ):
        http = make_api_client(server_root, accept_fulltext=True)
        first = _put_archive(http, full_push)
        assert first.status_code == 200, first.text
        assert first.json()["kept"] == {}  # a new profile: nothing to keep

        second = _put_archive(http, bare_push)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["kept"] == {"fulltext": len(_JANE_FULLTEXT), "index": 1}
        assert body["mode"] == "replace"
        assert body["indexed"] is True

        stored = server_root / SLUG
        for rel in _JANE_FULLTEXT:
            assert (stored / rel).is_file()
        assert (stored / _INDEX).read_bytes() == b"not really sqlite"
        # The rest of the profile was replaced as before; no staging litter.
        assert (stored / "profile.jsonld").is_file()
        assert [p.name for p in server_root.iterdir() if p.name.startswith(".upload-")] == []

    def test_prune_deletes_what_the_archive_lacks(
        self, make_api_client, server_root, full_push, bare_push
    ):
        http = make_api_client(server_root, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200

        r = _put_archive(http, bare_push, mode="prune")
        assert r.status_code == 200, r.text
        assert r.json()["kept"] == {}
        assert r.json()["mode"] == "prune"
        assert r.json()["indexed"] is False

        stored = server_root / SLUG
        assert not (stored / "sources" / "papers").exists()
        assert not (stored / _INDEX).exists()
        assert (stored / "profile.jsonld").is_file()

    def test_a_class_the_archive_carries_is_authoritative(
        self, make_api_client, server_root, src: Path, full_push
    ):
        """Some fulltext in the archive means the archive decides the fulltext."""
        import shutil

        from researcher_profiles.api.upload import build_profile_archive

        http = make_api_client(server_root, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200

        # Push B carries one of the two fulltext files, and no index.
        (src / _JANE_FULLTEXT[1]).unlink()
        shutil.rmtree(src / ".cache")
        partial = build_profile_archive(src, include_fulltext=True).data
        r = _put_archive(http, partial)
        assert r.status_code == 200, r.text
        assert r.json()["kept"] == {"index": 1}

        stored = server_root / SLUG
        assert (stored / _JANE_FULLTEXT[0]).is_file()
        assert not (stored / _JANE_FULLTEXT[1]).exists()
        assert (stored / _INDEX).is_file()

    def test_merge_keeps_every_file_the_archive_omits(
        self, make_api_client, server_root, full_push, only_push
    ):
        """``rp push --only``: send two files, leave the rest of the copy alone."""
        http = make_api_client(server_root, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200
        live = _stored_files(server_root)

        r = _put_archive(http, only_push, mode="merge")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "merge"
        # Everything the two-member archive left out came back: the withheld
        # classes under their own names, the rest under "other".
        assert set(body["kept"]) == {"fulltext", "index", "other"}
        assert body["kept"]["fulltext"] == len(_JANE_FULLTEXT)
        assert body["kept"]["index"] == 1
        assert sum(body["kept"].values()) == len(live) - 2  # the two it carried
        assert _stored_files(server_root) == live

    def test_replace_deletes_an_unclassed_live_file_the_archive_lacks(
        self, make_api_client, server_root, src: Path
    ):
        """The default is a replacement: only a withheld class survives absence."""
        from researcher_profiles.api.upload import build_profile_archive

        http = make_api_client(server_root, accept_fulltext=True)
        web = src / "sources" / "web"
        web.mkdir(parents=True)
        (web / "x.md").write_text("a scraped page")
        assert _put_archive(http, build_profile_archive(src).data).status_code == 200
        assert (server_root / SLUG / "sources" / "web" / "x.md").is_file()

        (web / "x.md").unlink()
        r = _put_archive(http, build_profile_archive(src).data)
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "replace"
        assert not (server_root / SLUG / "sources" / "web" / "x.md").exists()

    def test_an_unknown_mode_is_a_400(self, make_api_client, server_root, full_push):
        http = make_api_client(server_root)
        r = _put_archive(http, full_push, mode="nonsense")
        assert r.status_code == 400
        assert "invalid mode" in r.json()["detail"]

    def test_a_bare_push_keeps_the_live_fulltext_in_a_sql_store(
        self, make_api_client, full_push, bare_push
    ):
        """No directory to walk: the live copies come from a scratch export."""

        store = SqlProfileStore("sqlite://")
        store.create_all()
        http = make_api_client(store, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200
        for rel in _JANE_FULLTEXT:
            assert store.artifact_bytes(SLUG, rel)

        r = _put_archive(http, bare_push)
        assert r.status_code == 200, r.text
        # The SQL store holds no ``.cache/embeddings.sqlite`` (vectors are rows),
        # so only the fulltext class has anything to carry over.
        assert r.json()["kept"] == {"fulltext": len(_JANE_FULLTEXT)}
        for rel in _JANE_FULLTEXT:
            assert store.artifact_bytes(SLUG, rel)

    def test_merge_keeps_the_live_files_in_a_sql_store(self, make_api_client, full_push, only_push):
        """Same scratch export, but merge has every omitted file to carry."""

        store = SqlProfileStore("sqlite://")
        store.create_all()
        http = make_api_client(store, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200

        r = _put_archive(http, only_push, mode="merge")
        assert r.status_code == 200, r.text
        kept = r.json()["kept"]
        assert kept["fulltext"] == len(_JANE_FULLTEXT)
        assert kept["other"] > 0
        for rel in (*_JANE_FULLTEXT, "personality/SOUL.md"):
            assert store.artifact_bytes(SLUG, rel)

    def _served_manifest(self, http) -> dict[str, dict]:
        """``{contentUrl: entry}`` from ``GET /profiles/{slug}/files``: the committed index."""
        resp = http.get(f"/api/v1/profiles/{SLUG}/files")
        assert resp.status_code == 200, resp.text
        return {e["contentUrl"]: e for e in resp.json()["files"]}

    @staticmethod
    def _drop_from_manifest(payload: bytes, prefix: str) -> bytes:
        """Rebuild ``payload`` with every ``prefix`` entry cut from its manifest.

        What a partial local copy sends: the bytes of the named file, and a
        ``profile.jsonld`` whose index no longer mentions the artifacts the
        server holds. Nothing else in the archive changes.
        """
        import io
        import json as _json
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            members = {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}
        doc = _json.loads(members["profile.jsonld"])
        for slot in ("hasPart", "subjectOf"):
            doc[slot] = [e for e in (doc.get(slot) or []) if not e["contentUrl"].startswith(prefix)]
        members["profile.jsonld"] = _json.dumps(doc).encode("utf-8")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    @pytest.mark.parametrize("backend", ["files", "sql"], ids=["filesystem", "sql"])
    def test_merge_splices_kept_files_into_the_manifest(
        self, make_api_client, server_root, full_push, only_push, backend
    ):
        """A kept file keeps its manifest entry, or it is not kept at all.

        The regression this closes: ``--only`` shipped a ``profile.jsonld``
        whose manifest had lost 53 fulltext entries. The bytes were kept, the
        entries were not, and the SQL store -- which writes rows BY the
        recorded manifest -- persisted nothing for them.
        """

        if backend == "sql":
            store = SqlProfileStore("sqlite://")
            store.create_all()
            target = store
        else:
            target = server_root
        http = make_api_client(target, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200
        before = self._served_manifest(http)
        assert set(_JANE_FULLTEXT) <= set(before)

        partial = self._drop_from_manifest(only_push, "sources/papers/")
        r = _put_archive(http, partial, mode="merge")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["spliced"] == len(_JANE_FULLTEXT)

        after = self._served_manifest(http)
        assert set(_JANE_FULLTEXT) <= set(after), "the kept fulltext lost its manifest entries"
        assert set(after) == set(before)
        assert body["manifest_counts"]["paper_fulltext"] == len(_JANE_FULLTEXT)
        assert sum(body["manifest_counts"].values()) == len(after)

        if backend == "sql":
            for rel in _JANE_FULLTEXT:
                assert target.artifact_bytes(SLUG, rel)
        else:
            for rel in _JANE_FULLTEXT:
                assert (server_root / SLUG / rel).is_file()

    def test_replace_splices_withheld_classes(
        self, make_api_client, server_root, full_push, bare_push
    ):
        """The default mode keeps the withheld classes, entries included."""
        http = make_api_client(server_root, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200
        before = self._served_manifest(http)

        partial = self._drop_from_manifest(bare_push, "sources/papers/")
        r = _put_archive(http, partial)
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "replace"
        assert r.json()["spliced"] == len(_JANE_FULLTEXT)
        assert set(self._served_manifest(http)) == set(before)

    def test_prune_drops_entries_and_reports_counts(
        self, make_api_client, server_root, full_push, bare_push
    ):
        """Nothing is kept, so nothing is spliced, and the counts say so.

        The archive is the whole profile under ``prune``, manifest included,
        so the fulltext entries are cut from the document as well as the
        tarball: a pruning push that left them in the index would leave the
        server advertising files it had just deleted.
        """
        http = make_api_client(server_root, accept_fulltext=True)
        assert _put_archive(http, full_push).status_code == 200
        before = self._served_manifest(http)

        pruning = self._drop_from_manifest(bare_push, "sources/papers/")
        r = _put_archive(http, pruning, mode="prune")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kept"] == {} and body["spliced"] == 0
        counts = body["manifest_counts"]
        assert "paper_fulltext" not in counts
        assert sum(counts.values()) < len(before)
        assert sum(counts.values()) == len(self._served_manifest(http))

    def test_a_new_profile_reports_its_manifest_counts(
        self, make_api_client, server_root, full_push
    ):
        """No live copy to keep anything from, but the counts still describe it."""
        http = make_api_client(server_root, accept_fulltext=True)
        r = _put_archive(http, full_push)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kept"] == {} and body["spliced"] == 0
        assert sum(body["manifest_counts"].values()) == len(self._served_manifest(http))

    def test_capabilities_route_lists_push_modes(self, make_api_client, server_root):
        """The pre-check a client makes before it trusts ``?mode=``."""
        from researcher_profiles.api.upload import PUSH_MODES

        http = make_api_client(server_root)
        r = http.get("/api/v1/capabilities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["push_modes"]) == set(PUSH_MODES)
        assert "manifest_splice" in body["features"]

    def test_index_html_round_trips_through_the_archive(self, fixture_profile):
        """``index.html`` is a manifest part (role ``html``), so it ships."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)
        (src / "index.html").write_text("<html>rendered profile page</html>")
        assert "index.html" in tar_names(build_profile_archive(src).data)
