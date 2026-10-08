# Read hooks: who may see how much

Reference for someone hosting rp-sdk who needs reads to depend on who is
asking (a service with user sessions, grants, or per-application API keys). If
you are serving a directory you intend to serve whole, you need nothing here:
the defaults already do the right thing.

## There is no read flag

Read access is a property of each profile and each caller, not a switch on the
whole read surface. A router-level dependency can only ask "do I recognize this
caller's credential", which cannot express "this profile is public and that one
is not", and would turn away a signed-in owner who presents a session cookie
rather than a bearer token. So every route on the read surface resolves a
viewer tier and projects its own response.

## The viewer tier

A viewer tier is one of `public` / `limited` / `private`, the same three
values an artifact carries, read in the other direction. On an artifact, a
higher tier means fewer people may see it; on a viewer, a higher tier means they
may see more. Anonymous is not a special case: it is the viewer whose tier is
`public`.

The hooks live on `app.state.hooks` (an
[`api.hooks.Hooks`](../../../rp-sdk/src/researcher_profiles/api/hooks.py)), and
every one but the first takes a **caller**, never a request:

```python
app.state.hooks.caller_resolver = my_caller  # (request) -> Caller
app.state.hooks.viewer_resolver = my_resolver  # (caller, slug | None) -> ViewerTier
app.state.hooks.profile_tier_floor = my_floor  # (caller, profile, slug) -> TierFloor
app.state.hooks.store_for = my_view  # (caller, store) -> ProfileStore
app.state.hooks.registry_proofs = my_proofs  # (rid) -> list[Proof]
```

### The caller

A [`Caller`](../../../rp-sdk/src/researcher_profiles/api/caller.py) is who is
asking: `scopes` (the host's vocabulary; rp-sdk never reads them),
`is_operator`, a baseline `tier`, the resolved `consumer` identity, the
`client_ip`, a `?as=` `viewer_cap`, and a `memo` dict that lives as long as the
caller. The HTTP adapter builds one per request through `caller_resolver`
(`deps.get_caller`); a host's MCP server builds one per tool call. Because the
hooks take a caller, the read functions in
[`api.service`](../../../rp-sdk/src/researcher_profiles/api/service.py)
(`get_profile`, `list_papers`, `get_paper`, `read_paper_text`,
`read_profile_text`, the two passage reads) run with no HTTP in the way, and
the routes are thin adapters over them.

The default `caller_resolver`
([`deps.default_caller_resolver`](../../../rp-sdk/src/researcher_profiles/api/deps.py))
gives a resolved consumer identity (the operator, or a key with its minted
`tier`), else the operator for a valid operator bearer token, else an
anonymous caller.

### `viewer_resolver`

Answers "how much may this caller be shown of this profile". It takes a slug
because the answer depends on it: a grant is held on one profile and not the
rest, so an owner is entitled to their own held-back profile and to nothing
else. A route that walks many profiles calls the resolver once per profile.

The default, [`deps.resolve_viewer_tier`](../../../rp-sdk/src/researcher_profiles/api/deps.py),
reads the caller alone: `private` for the operator, a consumer's minted
`tier`, else the caller's own `tier` (`public` for anonymous). Bare rp-sdk has
no per-profile grants; a host that has them (an owner reads their own
held-back profile whole) answers from its roles in its own resolver.

The anonymous default covers open dev mode. A server with no token
configured lets every request through the auth gates, and resolving that to
`private` would mean a laptop silently served the tier nothing else does. A
missing credential is not a permissive credential.

### `profile_tier_floor`

Answers "regardless of what this document declares, how far may it go here". A
floor may only narrow. It returns a
[`deps.TierFloor`](../../../rp-sdk/src/researcher_profiles/api/deps.py): the tier and
the sentence explaining it. `TierFloor()` (an empty tier) is "no opinion".

This is where a host puts a consent rule the document cannot know about. A
registry pins a profile whose owner has not published it to `limited`:
claiming a profile does not publish it, and a profile the registry built about
a real person who never signed in has nobody's agreement behind it at all.

The reason travels with the decision rather than being configured beside it,
because the three cases a host distinguishes (never claimed, claimed but never
published, explicitly unpublished) are three different sentences an owner reads
verbatim in the visibility report, and a single static string can only ever
describe one of them.

### `store_for`

Answers "which document does this caller read". A host whose callers may read
some parts of a profile and not others (which no single tier expresses) hands
back a view of the store whose `get` returns a rewritten document. Reads use
it; edits never do: every edit function loads the stored profile.

## What the routes do with it

Two gates, in this order:

1. the profile gate: the profile's own `visibility`, folded with the floor,
   against the viewer tier;
2. the artifact gate: each artifact's effective tier
   (`privacy.explain_tiers`, derivation rule applied) against the viewer tier.

Failing either is a 404, with a body byte-identical to a genuinely
nonexistent profile or an artifact that is not in the manifest. Never a 403: a
403 confirms the thing exists, which is the one fact a held-back profile is
trying not to disclose. The one exception is the build-local hard floors
described in
["What's private by default"](../../../docs/rp-spec/privacy.md#whats-private-by-default)
(`.cache/`, `.keys/`), which are `403`, because they are withheld from
everyone and no caller learns anything from being told why.

Every response carries `X-RP-Viewer-Tier`, so a client (or a test) can assert
what it was shown *as* without parsing what it was shown.

## Writing a resolver

Nothing in a resolver compares two tiers. It maps an identity onto a tier and
hands it back; `privacy.tier_allows` does the rest, in one place.

```python
def viewer_resolver(caller, slug):
    granted = _tier_from_my_grant_table(caller, slug)  # None when no grant
    return max(granted or "public", caller.tier, key=_ORDER.index)
```

Three things that are easy to get wrong:

- Key grants on the profile's identity, not its slug: a slug is a renameable
  display handle, so a grant looked up by it lapses the day someone renames
  their profile.
- Being signed in grants nothing about other people's profiles: an ORCID is
  free to obtain, so "authenticated" is not an access-control boundary. A
  signed-in stranger should see exactly what an anonymous visitor sees.
- Memoize per caller, on `caller.memo`: the listing routes call the resolver
  once per profile. Without memoization that is N database round-trips per
  index request. A caller lives one request or one tool call, so a changed
  setting applies on the next one.

## Preview

`?as=anonymous|lab|owner` composes with the resolved tier through
`privacy.narrow_viewer`, which can only narrow. Because it cannot widen, it
needs no authorization of its own and is safe to accept from anyone; an
unrecognized value is a loud `400` rather than a silent pass-through.

That composition is also what makes a preview honest. "What a stranger sees" is
the ordinary handler and the ordinary projection with the tier lowered. It is
reality, so it cannot drift from it.
