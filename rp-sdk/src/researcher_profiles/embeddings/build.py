"""Entry point for building a profile's embedding index, cheap to lazy-import."""

from .cache import IndexReport, SqliteEmbeddingIndex


def _index_root(profile):
    """The directory this profile's index lives under.

    ``ArtifactStorage`` does not cover ``.cache/``, so the index needs the
    profile's directory; a backend without one refuses here.
    """
    return profile.require_directory("the embedding index")


def build_index(
    profile, *, force: bool = False, backend=None, corpus_only: bool = False
) -> IndexReport:
    """Build (or refresh) the embedding index for a profile.

    ``profile`` is a :class:`ResearcherProfile` or a profile directory path.
    ``corpus_only`` leaves out summaries of papers not in
    ``sources/papers.jsonld``.
    """
    from pathlib import Path

    from ..profile import ResearcherProfile

    if isinstance(profile, ResearcherProfile):
        profile_path = _index_root(profile)
        document = profile.metadata
    else:
        profile_path = Path(profile)
        document = None
    idx = SqliteEmbeddingIndex(profile_path, profile_document=document, corpus_only=corpus_only)
    return idx.build_index(force=force, backend=backend)
