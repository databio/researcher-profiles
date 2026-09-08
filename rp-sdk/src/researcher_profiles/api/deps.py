"""Dependency injection helpers for the API.

The auth hooks (bearer token, owner verifier, consumer scopes) and the two
lookups every route starts from: the app's :class:`ProfileStore` and one
profile out of it.

The store itself is not defined here. It is
:class:`researcher_profiles.store.FilesystemProfileStore`, one implementation
of :class:`researcher_profiles.store.ProfileStore` among several, and this
module only fetches whichever one the app was built with.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Header, HTTPException, Query, Request

from ..privacy import ViewerTier, narrow_viewer
from ..profile import ResearcherProfile
from ..schema import Visibility
from ..store import ProfileNotFoundError, ProfileStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependencies used by routes
# ---------------------------------------------------------------------------


def get_store(request: Request) -> ProfileStore:
    """The store this app was built over. Any ``ProfileStore``, not a directory."""
    return request.app.state.store


def require_token(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """Bearer-token gate.

    The expected token is read from ``app.state.token`` (set by
    ``create_app``). If unset/empty, the server is in open mode and
    we allow the request through.
    """
    expected = getattr(request.app.state, "token", None)
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
        )


def require_owner(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """Owner gate for the interactive edit endpoints.

    This is the hook a management host plugs into. rp-sdk knows nothing
    about user sessions; a host that adds them (a management host) sets a verifier on
    ``app.state.owner_verifier``: a callable ``(request, slug) -> None`` that
    resolves the session cookie to a user, checks the ownership table for
    ``slug``, and raises ``HTTPException(401)`` (not logged in) or
    ``HTTPException(403)`` (logged in, not the owner) as appropriate.

    When no verifier is configured (bare rp-sdk), the edit endpoints fall back
    to the operator bearer token, so a stand-alone reference server can still
    edit with the operator credential, and the endpoints are never accidentally
    open. This is the "mounted either operator-gated (bare rp-sdk) or
    owner-gated (under a management host)" behavior the split calls for.
    """
    verifier = getattr(request.app.state, "owner_verifier", None)
    if verifier is None:
        # No session layer present: gate on the operator token, exactly like
        # every other write endpoint.
        require_token(request, authorization)
        return
    verifier(request, slug)


@dataclass(frozen=True)
class ConsumerIdentity:
    """The resolved identity of a calling consumer (an application).

    This is the return contract of a ``consumer_verifier``: rp-sdk carries it on
    ``request.state.consumer`` without knowing anything about how a host resolves
    a credential to it. ``is_operator`` marks the operator/superuser short-circuit
    (holding every scope); a real consumer key resolves to ``is_operator=False``
    with the concrete scopes minted for it.
    """

    id: str
    name: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    is_operator: bool = False
    #: The most permissive privacy tier this consumer may be shown. Minted per
    #: key by the host, so a lab integration reads ``internal`` content without
    #: the whole registry being open. Defaults to ``public``: a key that never
    #: said otherwise gets the narrowest reading.
    tier: Visibility = "public"


def require_scope(scope: str):
    """Build a dependency enforcing that the caller may use capability ``scope``.

    This is the consumer-axis hook, parallel to ``require_owner``. A host
    (a management host) installs ``app.state.consumer_verifier``: a callable
    ``(request, scope) -> ConsumerIdentity`` that resolves the inbound credential
    to a consumer identity for ``scope``, raising ``HTTPException(401)`` for a
    missing/unknown/revoked credential and ``HTTPException(403)`` for a known
    consumer that lacks ``scope``.

    When no verifier is installed (bare rp-sdk), there is no consumer concept, so
    the dependency falls back to the operator bearer token: the single-token
    semantics are identical to gating the endpoint with ``require_token``.
    """

    def dep(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> None:
        verifier = getattr(request.app.state, "consumer_verifier", None)
        if verifier is None:
            # Bare rp-sdk: no consumer layer. Preserve today's single-token
            # semantics exactly: operator token (or open dev mode).
            require_token(request, authorization)
            return
        identity = verifier(request, scope)
        request.state.consumer = identity

    return dep


def check_write_scope(request: Request, action: str, detail: dict) -> None:
    """Write-scope gate: may this credential make this change?

    Parallel to ``require_owner`` (which answers "may you edit at all") and
    ``require_scope`` (which answers "may this app use this capability").
    This hook answers "may this credential make this specific write", so
    scope-gated agents can be refused field-by-field.

    A host installs ``app.state.write_scope_verifier``: a callable
    ``(request, action, detail) -> None`` that raises ``HTTPException(403)``
    when the credential lacks the required scope. When no verifier is
    installed (bare rp-sdk), this is a no-op: all writes are allowed if
    ``require_owner`` passed.
    """
    verifier = getattr(request.app.state, "write_scope_verifier", None)
    if verifier is None:
        return
    verifier(request, action, detail)


#: How ``?as=`` names a viewer, in the words a person uses rather than tier
#: tokens. A cap, never a widening; see :func:`get_viewer_tier`.
PREVIEW_VIEWERS: dict[str, ViewerTier] = {
    "anonymous": "public",
    "lab": "internal",
    "owner": "restricted",
}


def resolve_viewer_tier(request: Request, slug: str | None) -> ViewerTier:
    """The bare-SDK default: the most permissive tier this caller may be shown.

    Evaluated in order, short-circuiting on the first hit:

    1. an installed ``owner_verifier`` that does not raise for ``slug``:
       the caller can edit this profile, so they may read all of it;
    2. a consumer identity already resolved onto ``request.state.consumer``
       by :func:`require_scope`: ``restricted`` for the operator, else the
       tier minted on that consumer's key;
    3. a valid operator bearer token;
    4. otherwise ``public``: anonymous callers and open dev mode alike.

    Rule 4 covers open dev mode. A server with no token configured
    lets every request through the auth gates, and resolving that to
    ``restricted`` would mean a developer's laptop silently served the tier
    nothing else does. A missing credential is not a permissive credential.

    A host replaces this wholesale via ``app.state.viewer_resolver``.
    """
    verifier = getattr(request.app.state, "owner_verifier", None)
    if verifier is not None and slug:
        # Boundary: a host-installed verifier. Anything it raises means "not the
        # owner", but this is an authorization fallthrough and must not be silent.
        try:
            verifier(request, slug)
        except Exception:
            logger.warning(
                "owner_verifier raised for slug %r; treating as not-owner", slug, exc_info=True
            )
        else:
            return "restricted"

    consumer = getattr(request.state, "consumer", None)
    if consumer is None:
        # The read surface has no scope dependency, so nothing has resolved the
        # caller's key yet. Ask the installed verifier ourselves, for the read
        # capability. A credential it rejects is not an error here; it is
        # not a consumer, and falls through to the operator/anonymous rules.
        verifier = getattr(request.app.state, "consumer_verifier", None)
        if verifier is not None and (request.headers.get("authorization") or ""):
            # Boundary: a host-installed verifier. A credential it rejects is
            # not an error, but a verifier that broke is, and this is the
            # authorization fallthrough where that difference disappears.
            try:
                consumer = verifier(request, "read")
            except Exception:
                logger.warning(
                    "consumer_verifier raised; treating the caller as anonymous", exc_info=True
                )
                consumer = None
    if consumer is not None:
        if getattr(consumer, "is_operator", False):
            return "restricted"
        return getattr(consumer, "tier", "public") or "public"

    expected = getattr(request.app.state, "token", None)
    if expected and request.headers.get("authorization") == f"Bearer {expected}":
        return "restricted"

    return "public"


def viewer_tier_for(request: Request, slug: str | None) -> ViewerTier:
    """This caller's viewer tier for one profile, preview cap applied.

    The viewer tier is a function of the caller *and* the profile: an owner is
    entitled to their own held-back profile and to nothing else. A route that
    walks many profiles (the profile listing, ``/match``) therefore asks this
    per profile rather than resolving one tier for the whole request, or an
    owner's own ``internal`` profile would vanish from a list that then showed
    it happily at its own URL.
    """
    resolver = getattr(request.app.state, "viewer_resolver", None) or resolve_viewer_tier
    tier: ViewerTier = resolver(request, slug)
    cap = getattr(request.state, "viewer_cap", None)
    if cap is not None:
        tier = narrow_viewer(tier, cap)
    return tier


def get_viewer_tier(
    request: Request,
    as_: Optional[str] = Query(
        None,
        alias="as",
        description="Preview as another viewer: anonymous | lab | owner. a cap, never a widening.",
    ),
) -> ViewerTier:
    """FastAPI dependency: this request's viewer tier, preview cap applied.

    ``slug`` is read from the route's path params rather than declared as an
    argument, so a route without one does not grow a phantom ``?slug=`` query
    parameter it would then have to ignore.

    ``?as=`` is composed with :func:`~researcher_profiles.privacy.narrow_viewer`,
    which can only narrow, so it needs no authorization of its own and is safe
    to accept from anyone. That is also what makes a preview honest: "what a
    stranger sees" is this same handler and this same projection with the
    viewer tier lowered, never a second implementation that can drift.

    An unrecognized ``?as=`` is a 400. Ignoring it would silently show an owner
    their own view while they believed they were looking at a stranger's.
    """
    request.state.viewer_cap = None
    if as_ is not None:
        requested = PREVIEW_VIEWERS.get(as_)
        if requested is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown viewer {as_!r} (anonymous | lab | owner)",
            )
        request.state.viewer_cap = requested
    resolved = viewer_tier_for(request, request.path_params.get("slug"))
    # Stashed so the response stamp (see ``create_app``) can report the tier a
    # response was projected through without every handler remembering to. On a
    # route that walks many profiles this is the caller's baseline tier; the
    # listing itself is computed per profile by ``viewer_tier_for``.
    request.state.viewer_tier = resolved
    return resolved


@dataclass(frozen=True)
class TierFloor:
    """A host-imposed ceiling on one profile, and the sentence explaining it.

    The decision and its explanation travel together, so an interface never
    shows a reason that does not match the rule that ran.

    ``tier=None`` is "no opinion": the document's own declaration is the whole
    story. ``reason`` is meaningful only alongside a tier.
    """

    tier: Visibility | None = None
    reason: str | None = None


def get_profile_tier_floor(request: Request, prof, slug: str | None = None) -> TierFloor:
    """The host's floor for this profile, and why: never ``None``.

    ``app.state.profile_tier_floor`` is how a host says "regardless of what this
    document declares, it may not go above X *here*". A management host pins a
    profile whose owner has not published it. The hook must return a :class:`TierFloor`;
    it owns both the decision and its wording. Left unset on bare rp-sdk, where
    an empty ``TierFloor()`` means a profile's own declaration governs.
    """
    hook = getattr(request.app.state, "profile_tier_floor", None)
    if hook is None:
        return TierFloor()
    return hook(request, prof, slug)


def get_profile(ref: str, store: ProfileStore) -> ResearcherProfile:
    """Resolve a slug or a rid to a profile, or raise 404."""
    try:
        return store.get(ref)
    except (ProfileNotFoundError, KeyError) as e:
        raise HTTPException(status_code=404, detail=f"profile {ref!r} not found") from e


def get_match_store(request: Request):
    """The app's store, proved able to rank, for ``/api/v1/match``.

    Ranking is embedding-backed, so it requires the ``vectors``/``st`` extras
    and built per-profile indexes. Each way that can be missing is a 503 rather
    than a 500, so callers degrade gracefully:

    1. The analytics package will not import at all (a core-only install).
    2. The store cannot serve vectors
       (:class:`~researcher_profiles.store.VectorStore`).
    3. Profiles exist but not one of them is indexed, so every ranking would
       come back empty and read like "nobody matched".

    Nothing is cached on ``app.state``: the managers cache on the store, which
    already lives at ``app.state.store``, and they drop their snapshot whenever
    the store's write generation moves. That is what replaced the registry
    object this dependency used to build and ``invalidate_after_write`` used to
    null out.

    Construction writes nothing, so the ``rid <-> slug`` lookup index is
    refreshed here explicitly: the server owns a writable root and is the
    natural place to keep that file current for shell callers.
    """
    try:
        from ..store._analytics import require_vector_store
    except ImportError as e:  # numpy / embeddings missing (core-only install)
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: this is a core-only install missing the "
                f"vectors/embeddings extra ({e}). Install the 'vectors' and 'st' extras."
            ),
        ) from e

    store = request.app.state.store

    try:
        from ..errors import CapabilityUnavailableError

        vstore = require_vector_store(store)
        # Touching the accessor is what actually imports the analytics: they
        # are lazy on the store precisely so ``rp list`` never pays for them.
        _ = vstore.match
    except CapabilityUnavailableError as e:
        raise HTTPException(status_code=503, detail=f"matching unavailable: {e}") from e
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: this is a core-only install missing the "
                f"vectors/embeddings extra ({e}). Install the 'vectors' and 'st' extras."
            ),
        ) from e

    try:
        slugs = store.list_slugs()
        indexed = any(store.has_vector_index(slug) for slug in slugs)
    # Boundary: enumerating the store and probing every profile's index.
    except Exception as e:
        logger.exception("failed to survey the store for matching")
        raise HTTPException(
            status_code=503,
            detail=f"matching unavailable: the profiles store could not be read: {e}",
        ) from e

    # Profiles exist and not one is indexed: an empty ranking here would be
    # indistinguishable from "nobody matched". A deployment whose profiles were
    # ingested without a built index lands here, and this is what tells it so.
    if slugs and not indexed:
        raise HTTPException(
            status_code=503,
            detail=(
                f"matching unavailable: {len(slugs)} profile(s) exist in "
                f"{store.location} but none has a usable embedding index"
            ),
        )

    # Nothing writes the lookup file implicitly; refresh it ourselves.
    store.write_lookup_index()
    return vstore


def materialize_store_to_tempdir(request: Request, store: ProfileStore):
    """Export every profile from a rootless store to a temp directory.

    One caller: :func:`get_graph`. The graph is the last feature that still
    needs a directory, because ``graph.sqlite`` is a regenerable handle
    ``ArtifactStorage`` does not cover and that has no interface of its own.
    The export carries no embeddings; the graph needs none.

    The temp directory is kept alive on ``app.state._registry_tempdir`` and
    reused across requests; ``_invalidate_after_write`` drops it so the next
    graph build reflects the write.

    Export failures stay per-profile: one malformed row should not cost the
    whole graph.
    """
    import tempfile
    from pathlib import Path

    existing = getattr(request.app.state, "_registry_tempdir", None)
    if existing is not None:
        return Path(existing.name)

    tmpdir = tempfile.TemporaryDirectory(prefix="rp-registry-")
    request.app.state._registry_tempdir = tmpdir
    root = Path(tmpdir.name)

    slugs = store.list_slugs()
    for slug in slugs:
        try:
            store.export_directory(slug, root / slug)
        # Boundary: one profile's export; the rest of the corpus still materializes.
        except Exception:
            logger.warning("failed to export profile %s for the graph", slug, exc_info=True)

    logger.info("materialized %d profiles to %s for the graph", len(slugs), root)
    return root


def _directory_root(request: Request, feature: str):
    """The store's filesystem root, or a 503 naming what needs one.

    Not for ranking: vectors go through
    :class:`~researcher_profiles.store.VectorStore` now, and a store without
    that capability is told so by ``store._analytics.require_vector_store``.
    What is left here is ``graph.sqlite``, a regenerable sqlite handle
    ``ArtifactStorage`` does not cover and that has no interface of its own. A
    store that is not a directory says so with ``root is None``, and this turns
    that into an actionable 503 instead of a crash on a synthetic path. See
    :mod:`researcher_profiles.store`.
    """
    store = request.app.state.store
    root = store.root
    if root is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{feature} needs a filesystem profiles root; this server is backed by "
                f"{store.location}. Export profiles to a directory and serve those, or "
                "run this feature against a directory-backed deployment."
            ),
        )
    return root


def get_graph(request: Request):
    """Lazily build (and cache) the profile graph over the app's store.

    Returns ``app.state.graph`` when present, else
    ``ProfileGraph.from_store`` (which loads ``<root>/.cache/graph.sqlite`` or builds
    and persists it), caching the result on ``app.state.graph``. Unlike
    ranking, the graph needs no heavy ML deps (it is pure bibliometric
    transformation), so its 503 degradation is rare, reached only if the store
    itself is unreadable.

    For SQL-backed stores, uses the same temp directory ``get_graph`` caches.
    """
    graph = getattr(request.app.state, "graph", None)
    if graph is not None:
        return graph

    try:
        from ..graph import ProfileGraph
    except ImportError as e:  # pragma: no cover - graph is core-only, should import
        raise HTTPException(
            status_code=503,
            detail="graph unavailable: could not import the graph subpackage",
        ) from e

    store = request.app.state.store
    root = store.root

    # SQL-backed store: use materialized temp directory
    if root is None:
        root = materialize_store_to_tempdir(request, store)

    try:
        graph = ProfileGraph.from_store(root)
    # Boundary: building the graph reads every profile in the store.
    except Exception as e:
        logger.exception("failed to build ProfileGraph")
        raise HTTPException(
            status_code=503,
            detail="graph unavailable: the profiles store could not be read",
        ) from e
    request.app.state.graph = graph
    return graph


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def get_token_from_env() -> Optional[str]:
    return os.environ.get("RESEARCHER_PROFILES_TOKEN") or None
