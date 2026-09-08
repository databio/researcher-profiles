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

A viewer tier is one of `public` / `internal` / `restricted`, the same three
values an artifact carries, read in the other direction. On an artifact, a
higher tier means fewer people may see it; on a viewer, a higher tier means they
may see more. Anonymous is not a special case: it is the viewer whose tier is
`public`.

Two hooks on `app.state`, both optional, both `None` on a bare app:

```python
app.state.viewer_resolver = my_resolver  # (request, slug | None) -> ViewerTier
app.state.profile_tier_floor = my_floor  # (request, profile, slug) -> TierFloor
```

### `viewer_resolver`

Answers "how much may this caller be shown of this profile". It takes a slug
because the answer depends on it: a grant is held on one profile and not the
rest, so an owner is entitled to their own held-back profile and to nothing
else. A route that walks many profiles calls the resolver once per profile.

Without one, [`deps.resolve_viewer_tier`](../../../rp-sdk/src/researcher_profiles/api/deps.py)
applies in order: an `owner_verifier` that does not raise for this slug gives
`restricted`; a resolved consumer identity gives `restricted` for the operator,
else that key's minted `tier`; a valid operator bearer token gives `restricted`;
otherwise `public`.

That last rule covers open dev mode. A server with no token
configured lets every request through the auth gates, and resolving that to
`restricted` would mean a laptop silently served the tier nothing else does. A
missing credential is not a permissive credential.

### `profile_tier_floor`

Answers "regardless of what this document declares, how far may it go here". A
floor may only narrow. It returns a
[`deps.TierFloor`](../../../rp-sdk/src/researcher_profiles/api/deps.py): the tier and
the sentence explaining it. `TierFloor()` (an empty tier) is "no opinion".

This is where a host puts a consent rule the document cannot know about. A
registry pins a profile whose owner has not published it to `internal`:
claiming a profile does not publish it, and a profile the registry built about
a real person who never signed in has nobody's agreement behind it at all.

The reason travels with the decision rather than being configured beside it,
because the three cases a host distinguishes (never claimed, claimed but never
published, explicitly unpublished) are three different sentences an owner reads
verbatim in the visibility report, and a single static string can only ever
describe one of them.

## What the routes do with it

Two gates, in this order:

1. the profile gate: the profile's own `visibility`, folded with the floor,
   against the viewer tier;
2. the artifact gate: each artifact's effective tier
   (`privacy.explain_tiers`, derivation rule applied) against the viewer tier.

Failing either is a 404, with a body byte-identical to a genuinely
nonexistent profile or an artifact that is not in the manifest. Never a 403: a
403 confirms the thing exists, which is the one fact a held-back profile is
trying not to disclose. The one exception is the hard floors described in
["What's always restricted"](../../../docs/rp-spec/privacy.md#whats-always-restricted)
(`paper_fulltext`, `.cache/`, `.keys/`), which are `403`, because they are
withheld from everyone and no caller learns anything from being told why.

Every response carries `X-RP-Viewer-Tier`, so a client (or a test) can assert
what it was shown *as* without parsing what it was shown.

## Writing a resolver

Nothing in a resolver compares two tiers. It maps an identity onto a tier and
hands it back; `privacy.tier_allows` does the rest, in one place.

```python
def viewer_resolver(request, slug):
    granted = _tier_from_my_grant_table(request, slug)  # None when no grant
    credential = _tier_from_the_presented_key(request)  # 'public' when none
    return max(granted or "public", credential, key=_ORDER.index)
```

Three things that are easy to get wrong:

- Key grants on the profile's identity, not its slug: a slug is a renameable
  display handle, so a grant looked up by it lapses the day someone renames
  their profile.
- Being signed in grants nothing about other people's profiles: an ORCID is
  free to obtain, so "authenticated" is not an access-control boundary. A
  signed-in stranger should see exactly what an anonymous visitor sees.
- Memoize per request: the listing routes call the resolver once per
  profile. Without memoization that is N database round-trips per index request.

## Preview

`?as=anonymous|lab|owner` composes with the resolved tier through
`privacy.narrow_viewer`, which can only narrow. Because it cannot widen, it
needs no authorization of its own and is safe to accept from anyone; an
unrecognized value is a loud `400` rather than a silent pass-through.

That composition is also what makes a preview honest. "What a stranger sees" is
the ordinary handler and the ordinary projection with the tier lowered. It is
reality, so it cannot drift from it.
