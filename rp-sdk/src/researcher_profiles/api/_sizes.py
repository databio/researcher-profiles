"""Per-caller size declarations, computed in one pass over the manifest.

A row or a record says, for each deeper part (a paper's summary and full
text, the narrative, the works file), whether *this caller* can fetch it and
how big it is. "Available" means a route will hand it over: the viewer's tier
reaches it (:func:`~._projection.artifact_visible`) and the store holds its
body. Advertising a part that then 404s would make the profile look broken
rather than private.

Sizes come from the manifest's ``bytes`` field plus one
:meth:`ProfileStore.held_artifacts` call, so no bodies are read.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from ..models.api import Size
from ..privacy import ViewerTier, explain_tiers
from ..schema.manifest import _SUMMARY_SUFFIX
from ..store import ProfileStore
from ._projection import artifact_visible

SOUL_URL = "personality/SOUL.md"
EXPERTISE_URL = "personality/expertise.md"
PAPERS_URL = "sources/papers.jsonld"


def summary_url(paper_id: str) -> str:
    """Where a paper's generated summary lives."""
    return f"sources/summaries/{paper_id}{_SUMMARY_SUFFIX}"


def text_url(paper_id: str) -> str:
    """Where a paper's full text lives."""
    return f"sources/papers/{paper_id}.md"


def _available(n: Optional[int]) -> Size:
    return Size(available=True, bytes=n, approx_tokens=(n // 4) if n is not None else None)


class SizeIndex:
    """``{contentUrl: Size}`` for one viewer of one profile.

    Reasons, when a part is not available:

    - ``not_permitted``: in the manifest, and this viewer's tier does not reach
      it. The manifest is tier-invariant, so this discloses nothing the
      ``withheld`` list does not already;
    - ``none``: no such artifact in the manifest;
    - ``not_uploaded``: in the manifest and visible, but the body was never
      pushed to this store.
    """

    def __init__(self, prof, viewer: ViewerTier, store: ProfileStore, ref: str):
        self.viewer = viewer
        md = prof.metadata
        self.explain = explain_tiers(md)
        try:
            held = store.held_artifacts(ref)
        # Boundary: a store that cannot list its bodies; trust the manifest.
        except Exception:  # pragma: no cover - every shipped store answers
            held = None
        self._sizes: dict[str, Size] = {}
        self._withheld_roles: Counter[str] = Counter()
        for part in prof.manifest():
            url = part.content_url
            if not artifact_visible(self.explain, md, url, part.role, viewer):
                self._sizes[url] = Size(available=False, reason="not_permitted")
                self._withheld_roles[part.role or "other"] += 1
            elif held is not None and url not in held:
                self._sizes[url] = Size(available=False, reason="not_uploaded")
            else:
                n = part.bytes if part.bytes is not None else (held or {}).get(url)
                self._sizes[url] = _available(n)
        # The narrative may be held but not yet in the manifest. The content
        # routes serve it on the default tier rule, so its size must show too.
        for url, role, body in (
            (SOUL_URL, "soul", lambda: prof.soul),
            (EXPERTISE_URL, "expertise", lambda: prof.expertise),
        ):
            if url in self._sizes:
                continue
            if not artifact_visible(self.explain, md, url, role, viewer):
                continue
            n = (held or {}).get(url)
            if n is None:
                text = body() or ""
                if not text:
                    continue
                n = len(text.encode("utf-8"))
            self._sizes[url] = _available(n)

    def of(self, content_url: str) -> Size:
        """The size of one artifact for this viewer."""
        return self._sizes.get(content_url) or Size(available=False, reason="none")

    def summary(self, paper_id: str) -> Size:
        return self.of(summary_url(paper_id))

    def text(self, paper_id: str) -> Size:
        return self.of(text_url(paper_id))

    def soul(self) -> Size:
        return self.of(SOUL_URL)

    def expertise(self) -> Size:
        return self.of(EXPERTISE_URL)

    def papers(self) -> Size:
        return self.of(PAPERS_URL)

    def files_withheld(self) -> dict[str, int]:
        """Files this viewer may not read, counted by role."""
        return dict(sorted(self._withheld_roles.items()))


def size_index(
    prof, viewer: ViewerTier, store: ProfileStore, ref: Optional[str] = None
) -> SizeIndex:
    """One pass over ``prof.manifest()``: every part's :class:`Size` for this viewer."""
    return SizeIndex(prof, viewer, store, ref or prof.slug)


__all__ = [
    "EXPERTISE_URL",
    "PAPERS_URL",
    "SOUL_URL",
    "SizeIndex",
    "size_index",
    "summary_url",
    "text_url",
]
