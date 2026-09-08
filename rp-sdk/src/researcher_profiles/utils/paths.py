"""Where a profile's on-disk artifacts live: the two-root layout.

A profile has two roots:

* the content root: the profile directory itself
  (``$RESEARCHER_PROFILES_ROOT/<slug>/``). It holds only durable, publishable
  artifacts: ``profile.jsonld``, ``sources/``, ``personality/``,
  ``embeddings/``, and the profile-adjacent serve-time cache ``.cache/``.
* the build root: ``$RESEARCHER_PROFILES_ROOT/.build/<slug>/``, a sibling tree
  *outside* the content root that holds one build session's disposable
  bookkeeping (``meta/``) and append-only operational logs (``logs/``).

The build root is a pure function of the content root, so no code has to thread
a second path argument around. Every consumer derives one from the other with
:func:`build_dir_for`.

No durable fact about a profile is derived from the build root. ``rm -rf
$RESEARCHER_PROFILES_ROOT/.build/`` loses only resume hints and usage logs: every
profile keeps its state and every cached audit verdict stays valid. See
:func:`researcher_profiles.validate.validate_profile_dir`, which decides a
profile's conformance from durable content-root artifacts and never reads this
tree.

Why the disposable files split three ways:

* build-session bookkeeping (``build_state.json`` and whatever else a build
  tool keeps) is scoped to one build run and lives in the build root
  (:func:`build_meta_dir`).
* append-only operational logs (``llm-usage.jsonl``) are a record, not a
  cache: deleting one loses information. They also live in the build root
  (:func:`build_logs_dir`), outside the content tree, so they are never
  published, pushed, or manifested.
* serve-time derived caches (``embeddings.sqlite``, ``topics.json``,
  ``calibration.json``, ``profile_vec.npz``, ``coverage.json``) are
  regenerated at serve/analytics time, long after any build session ended, by
  code that has no build root. They stay profile-adjacent, in ``.cache/``
  (:func:`cache_dir`), still excluded from a publish.

A ``.cache/`` directory, at either scope (a profile's, or the store-wide one
from :func:`store_cache_dir`), is always safe to delete: everything in it is a
copy or a precomputation of something the store already holds.
"""

from pathlib import Path
from typing import Optional

__all__ = [
    "BUILD_ROOT_DIRNAME",
    "CACHE_DIRNAME",
    "REBUILT_DERIVED_CACHES",
    "STORE_CACHE_DIRNAME",
    "build_dir_for",
    "build_logs_dir",
    "build_meta_dir",
    "cache_dir",
    "derived_cache_path",
    "drop_derived_caches",
    "store_cache_dir",
]

#: Directory (sibling to each profile inside the profiles root) that holds the
#: per-profile build roots. Hidden so ``rp`` listings and syncs skip it.
BUILD_ROOT_DIRNAME = ".build"

#: Profile-adjacent directory for serve-time derived caches.
CACHE_DIRNAME = ".cache"

#: Store-wide directory (at the profiles root) for cross-profile derived
#: caches: the ``rid <-> slug`` index, stacked centroids, the co-authorship
#: graph, the rendered store-wide topic view. Same name as the per-profile
#: directory on purpose: any ``.cache`` is safe to delete.
STORE_CACHE_DIRNAME = ".cache"


def build_dir_for(profile_dir: str | Path) -> Path:
    """The build root for a profile's content directory.

    ``$RESEARCHER_PROFILES_ROOT/<slug>/`` -> ``$RESEARCHER_PROFILES_ROOT/.build/<slug>/``.
    """
    p = Path(profile_dir)
    return p.parent / BUILD_ROOT_DIRNAME / p.name


def build_meta_dir(profile_dir: str | Path) -> Path:
    """Where build-session bookkeeping lives.

    ``$RESEARCHER_PROFILES_ROOT/.build/<slug>/meta/``.
    """
    return build_dir_for(profile_dir) / "meta"


def build_logs_dir(profile_dir: str | Path) -> Path:
    """``$RESEARCHER_PROFILES_ROOT/.build/<slug>/logs/``: append-only operational logs.

    Records, not caches: deleting one loses information. They live in the build
    root, outside the content tree, so they are never published or pushed.
    """
    return build_dir_for(profile_dir) / "logs"


def cache_dir(profile_dir: str | Path) -> Path:
    """Profile-adjacent serve-time derived caches.

    ``$RESEARCHER_PROFILES_ROOT/<slug>/.cache/``.
    """
    return Path(profile_dir) / CACHE_DIRNAME


def store_cache_dir(root: Optional[Path]) -> Optional[Path]:
    """``<root>/.cache/``, created on demand; ``None`` without a root.

    The store-wide sibling of :func:`cache_dir`. Everything under it is a
    write-through memo of something the profiles already contain, so a store
    with no filesystem simply recomputes in process.
    """
    if root is None:
        return None
    d = root / STORE_CACHE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n", encoding="utf-8")
        except OSError:
            pass
    return d


#: Serve-time derived caches a rebuilt embedding index invalidates. Everything
#: here is a pure function of ``embeddings.sqlite`` plus the profile document,
#: so rebuilding the index makes each one stale. ``topics.json`` here means the
#: cluster-method copy under ``.cache/``; LLM-labeled topics are content
#: (``personality/topics.json``) and survive a rebuild.
REBUILT_DERIVED_CACHES = ("profile_vec.npz", "topics.json", "calibration.json")


def derived_cache_path(profile_dir: str | Path, name: str) -> Path:
    """One named serve-time derived cache inside a profile's ``.cache/``."""
    return cache_dir(profile_dir) / name


def drop_derived_caches(
    profile_dir: str | Path,
    names: tuple[str, ...] = REBUILT_DERIVED_CACHES,
) -> list[Path]:
    """Unlink the named derived caches; return the ones actually removed.

    Fail-soft: a cache that cannot be unlinked (read-only tree) is skipped,
    because losing a stale cache is less important than finishing the rebuild.
    """
    removed: list[Path] = []
    for name in names:
        fp = derived_cache_path(profile_dir, name)
        if fp.exists():
            try:
                fp.unlink()
            except OSError:
                continue
            removed.append(fp)
    return removed
