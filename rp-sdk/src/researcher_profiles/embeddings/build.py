"""The thin, importable entry point for building a profile's embedding index.

:func:`build_index` is what the create/update pipeline lazy-imports without a
circular import; the heavy lifting lives on :class:`SqliteEmbeddingIndex`.
"""

from .cache import IndexReport, SqliteEmbeddingIndex


def _index_root(profile):
    """The directory this profile's index lives under.

    ``ArtifactStorage`` does not cover the derived caches under ``.cache/``, so
    this is one of the few places allowed to ask a profile where its
    directory is, and the one place that asks on behalf of the index, for
    both callers that need it: :func:`build_index` here and
    :class:`researcher_profiles.profile.index.IndexManager`. A backend with no
    directory refuses here, once, instead of in three subclass overrides.
    """
    return profile.require_directory("the embedding index")


def build_index(profile, *, force: bool = False, backend=None) -> IndexReport:
    """Build (or refresh) the embedding index for a profile.

    ``profile`` may be either a :class:`ResearcherProfile` or a path-like
    pointing at a profile directory. The function is the thin, importable
    entry point used by the create/update pipeline; the heavy lifting
    lives on :class:`SqliteEmbeddingIndex`.
    """
    from pathlib import Path

    from ..profile import ResearcherProfile

    if isinstance(profile, ResearcherProfile):
        profile_path = _index_root(profile)
        document = profile.metadata
    else:
        profile_path = Path(profile)
        document = None
    idx = SqliteEmbeddingIndex(profile_path, profile_document=document)
    return idx.build_index(force=force, backend=backend)
