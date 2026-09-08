# The storage layer and pre-commit hooks

Two audiences, one page. If you are writing a backend, read
[ArtifactStorage](#artifactstorage) first. If you are writing a hook (a
management host that has to keep its own state consistent with a profile
write), skip to [What a hook is](#what-a-hook-is).

## ArtifactStorage

`ResearcherProfile` is a thin aggregate. It owns identity, eight lazy cache
slots, the public `save_*` writers, and six capability managers. It owns no
persistence at all: that is a composed `researcher_profiles.storage.ArtifactStorage`.

```python
from researcher_profiles.storage import DirectoryArtifactStorage, ArtifactStorage
from researcher_profiles import ResearcherProfile

prof = ResearcherProfile(DirectoryArtifactStorage("/profiles/jane-doe"))
prof.storage  # the backend
prof.directory  # Path | None: the only filesystem admission
prof.require_directory("the embedding index")  # or CapabilityUnavailableError
```

A backend implements `ArtifactStorage`. It never subclasses
`ResearcherProfile`. Four backends ship in the SDK:

| Storage | Backing | Extra |
|---|---|---|
| `storage.DirectoryArtifactStorage` | a profile directory | core |
| `store.sql.SqlArtifactStorage` | the `rp_*` rows for one profile | `[sql]` |
| `client.ApiArtifactStorage` | a live `researcher_profiles.api` server | `[client]` |
| `client.StaticArtifactStorage` | a published directory on a dumb static host | `[client]` |

The contract has five groups:

- Identity: `key` (`file:/abs/path`, `db:<url>#<rid>`, `api:<base>/<slug>`,
  `static:<base>`; opaque, compared and never parsed), `slug`, `rid_hint`,
  `directory`, `locate(*parts)`.
- The eight artifacts: `load_document` / `save_document`,
  `load_persisted_document`, `load_expertise` / `save_expertise`, `load_soul` /
  `save_soul`, `load_papers` / `save_papers`, `load_grants` / `save_grants`,
  `load_citations` / `save_citations`, `load_summaries` / `save_summary` /
  `delete_summary`, `load_build_state` / `save_build_state`.
- Derived: `content_hash()`. There is no writer. The filesystem and HTTP
  backends recompute it; the SQL backend reads its column.
- Raw bodies and manifest: `artifact_text`, `artifact_bytes`,
  `collection_envelope`, `build_manifest()`.
- Transaction: `new_write_context`, `refresh_derived`, `commit`,
  `rollback`.

There are also two optional overrides, `persona(profile)` and `index(profile)`,
which return either `None` (build the local manager) or a backend-supplied
manager. `ApiArtifactStorage`
supplies both, because the server owns the model and the index.

It is an abstract base class, not a `Protocol`, for three reasons:
`ReadOnlyArtifactStorage` carries the one refusal message both HTTP backends give;
every backend wants the same no-op `refresh_derived` / `commit` / `rollback`
unless it has a real transaction; and an incomplete backend fails loudly at
construction with a `TypeError` instead of at the first call to the method it
forgot.

Read-only backends refuse every writer with one message naming
`client.install_profile()`, and `new_write_context` raises rather than
returning. A registered pre-commit hook never observes a write that cannot
happen.

## What a hook is

A pre-commit hook is one callable taking one `WriteContext` and returning
nothing:

```python
from researcher_profiles import WriteContext


def mark_dependent_state_stale(ctx: WriteContext) -> None: ...
```

Register it on the store, not on an app. Every
[`ProfileStore`](../../../docs/rp-sdk/reference/python-api.md#profilestore-researcher_profilesstore) has this method, on both shipped
backends:

```python
from researcher_profiles.store import FilesystemProfileStore
from researcher_profiles.store.sql import SqlProfileStore  # [sql]

store = FilesystemProfileStore(profiles_dir)  # or SqlProfileStore(url)
store.add_pre_commit_hook(hook)

create_app(store, pre_commit_hooks=[hook])  # or at composition time
```

A hook registered on an HTTP app fires only for writes that went through a
route that remembered to fire it, so a background writer that bypasses the
routes would skip it. On the store, it fires for API routes, CLI writes, and out-of-process runs alike.

It also reaches profiles the store has already handed out, so registration
order relative to a first `store.get()` does not matter. The two backends get
there differently: the filesystem store walks its LRU, and the SQL store
walks a weak set of live profiles. That difference is not observable.

## What the hook receives

```python
@dataclass(frozen=True)
class WriteContext:
    profile: ResearcherProfile
    slug: str
    rid: str
    kind: str  # "document" | "soul" | "papers" | "create" | ...
    session: Any | None
    atomic: bool
    request: Any | None = None
```

| Field | What to do with it |
|---|---|
| `profile` | Read through it. Reads observe post-write state, including `content_hash()`. |
| `slug` | Display handle, not identity. |
| `rid` | The join key. Key your own tables on this. |
| `kind` | Which artifact(s) this unit is writing. Filter on it if you care; most hooks do not. |
| `session` | The backend's transaction handle, or `None`. If it is not `None`, use it and never open your own connection, or you will deadlock against the row the unit already holds. |
| `atomic` | Whether your writes commit or roll back with this unit. Branch on this, never on `session is None`. The two are not synonyms and will diverge the moment a second backend appears. |
| `request` | The ambient HTTP request, or `None` for CLI, pipeline, and out-of-process writes. A hook that dereferences this unconditionally breaks every non-HTTP write path. Close over what you need at registration time instead of reaching through `ctx.request.app.state`. |

## Ordering

Within one write unit, in this exact order:

1. The artifact bytes/rows are written through the backend's `save_*` method.
2. Store-maintained derived state is refreshed: for a SQL store, the
   `content_hash` column. This happens before the hooks, so a hook calling
   `ctx.profile.content_hash()` observes the new content. A hook that
   hashed pre-write content would mark dependent state stale against the wrong
   digest.
3. Registered hooks run, in registration order, each receiving the same
   `WriteContext`.
4. Only then does the unit commit.

`content_hash` spans two artifacts: the canonical profile document bytes
and the SOUL text, NUL-separated. A soul-only write refreshes it too. That
is why derived state is refreshed by the write unit and not by the document
writer.

In-memory cache slots on the profile (`metadata`, `soul`, ...) are assigned
after the unit exits cleanly, so a rolled-back write leaves the in-memory
profile matching the store rather than matching a write that never landed.

## Failure

A hook that raises aborts the write. The unit does not commit, a
transactional backend rolls back, and the exception propagates wrapped in
`WriteHookError`, which names the hook and carries the original as `__cause__`.
Nothing is swallowed and nothing is logged-and-continued.

Hooks run in registration order, and the first raise stops the rest. A
partial run followed by a rollback is fine; a partial run followed by a commit
is not.

`WriteHookError` is not a `ProfileWriteError`. `edit.py`
converts `ProfileWriteError` into an `EditError` that the HTTP layer maps to
**400 Bad Request**, which is right for a patch the caller got wrong. A failing
hook is a server-side fault, so `WriteHookError` propagates uncaught and
surfaces as **500**. Do not "fix" this by changing the base class.

## Which backend gives you which

| Store | Storage | `session` | `atomic` |
|---|---|---|---|
| `FilesystemProfileStore` | `DirectoryArtifactStorage` | `None` | `False` |
| `SqlProfileStore` ([`[sql]`](../../../docs/rp-sdk/how-to/sql-layer.md)) | `SqlArtifactStorage` | the SQLAlchemy `Session` | `True` |
| none | `ApiArtifactStorage` / `StaticArtifactStorage` | none | none (writes refused before a context exists) |

On the SQL store the artifact rows, the refreshed `content_hash` column, and
every hook commit or roll back together, so a hook that uses `ctx.session` gets
real atomicity. Branch on `ctx.atomic` and it keeps working on both.

## Non-transactional backends

The filesystem backend has no session and no transaction. It still runs every
registered hook, at the same logical point, so the *observable* contract is
identical across backends. Only atomicity differs, and `ctx.atomic` is how your
hook is told which it is getting. Concretely, on the filesystem backend:

- `session is None` and `atomic is False`.
- A raising hook does not undo the write. The bytes are already on disk. The
  write unit makes one compensating write (for the profile document only,
  since `save_profile` already read its predecessor for the `dateModified`
  stamp) and then re-raises either way. This is a compensating write, not a
  transaction. It does not cover a unit that touched several artifacts, it does
  not cover soul/papers/summaries, and it can itself fail (it logs loudly and
  the original exception still propagates).

A hook written against a transactional store must branch on `ctx.atomic` and
choose explicitly:

```python
def mark_stale(db, ctx: WriteContext) -> None:
    new_hash = ctx.profile.content_hash()
    if ctx.atomic:
        db.mark_stale(ctx.rid, new_hash, session=ctx.session)
    else:
        logger.warning(
            "marking %s stale without a transaction (backend is non-atomic); "
            "a rolled-back write will leave this spuriously stale",
            ctx.rid,
        )
        db.mark_stale(ctx.rid, new_hash)
```

Either degrade consciously, as above, or raise `TransactionRequired`: "this
hook maintains state that must be transactional; the active backend is not".
Do not pass `ctx.session` (which is `None` there) into a
call that quietly opens its own autocommitting connection and then reports
success.

Which way to lean depends on the asymmetry of the failure. Spuriously-stale
dependent state is usually recoverable; wrongly-fresh dependent state is silent
corruption nobody notices. When that asymmetry holds, degrade rather than
refuse, and log which branch ran.

## Nesting

`write_unit` joins an ambient unit if one is already open, so nesting is safe
and produces one commit and one hook run:

```python
with prof.write_unit("create"):
    prof.save_profile(doc)
    prof.save_soul(soul)
    accounts.grant_owner_role(rid, user, session=...)
```

That is what lets a caller make several writes plus its own dependent write into
a single atomic operation.
