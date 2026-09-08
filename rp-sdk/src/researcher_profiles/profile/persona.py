"""``prof.persona``: the LLM-backed persona surface.

Defines :class:`PersonaManager`, the object ``ResearcherProfile.persona``
hands back. The heavy lifting (prompt building, the LLM client, the ask/
review bodies) stays in :mod:`researcher_profiles.generative.llm`,
:mod:`researcher_profiles.generative` and :mod:`researcher_profiles.generative.chat`;
this module imports and calls them.
"""

from typing import Any, Optional

from ..generative.llm import _ask, _review


class PersonaManager:
    """``prof.persona``: the LLM-backed persona surface.

    Five verbs over one persona: ``ask`` and ``review`` live in
    :mod:`researcher_profiles.generative.llm`, ``innovate`` and ``riff`` in
    :mod:`researcher_profiles.generative`, and ``chat`` in
    :mod:`researcher_profiles.generative.chat`. Their bodies stay in those modules. This
    class imports and calls them, so no single module swallows three others
    while callers still get one object.

    Every method raises ``PersonaUnavailableError`` when
    ``not profile.has_persona`` (a lite profile never synthesized SOUL /
    expertise, and role-playing an empty persona is worse than refusing). The
    HTTP layer maps that to 409.
    """

    def __init__(self, profile: Any):
        self._profile = profile

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PersonaManager(slug={self._profile.slug!r})"

    def ask(
        self,
        question: str,
        k: int = 5,
        *,
        model: Optional[str] = None,
        thinking: Optional[dict] = None,
        strict_corpus: bool = False,
        refusal_threshold: Optional[float] = None,
        history: Optional[list] = None,
        source_types: Optional[list[str]] = None,
        safe_mode: Any = None,
    ):
        """Answer ``question`` AS this researcher, grounded in their corpus."""
        return _ask(
            self._profile,
            question,
            k,
            model=model,
            thinking=thinking,
            strict_corpus=strict_corpus,
            refusal_threshold=refusal_threshold,
            history=history,
            source_types=source_types,
            safe_mode=safe_mode,
        )

    def review(
        self,
        material: str,
        focus: Optional[str] = None,
        k: int = 5,
        *,
        model: Optional[str] = None,
        thinking: Optional[dict] = None,
        strict_corpus: bool = False,
        refusal_threshold: Optional[float] = None,
        source_types: Optional[list[str]] = None,
        safe_mode: Any = None,
    ):
        """Review ``material`` AS this researcher."""
        return _review(
            self._profile,
            material,
            focus,
            k,
            model=model,
            thinking=thinking,
            strict_corpus=strict_corpus,
            refusal_threshold=refusal_threshold,
            source_types=source_types,
            safe_mode=safe_mode,
        )

    def innovate(self, topic: str, n: int = 3, **kwargs: Any):
        """Propose ``n`` novel research directions in ``topic``."""
        from ..generative.core import _innovate

        return _innovate(self._profile, topic, n, **kwargs)

    def riff(self, seed: str, n: int = 5, **kwargs: Any):
        """Generate ``n`` divergent riffs on ``seed``."""
        from ..generative.core import _riff

        return _riff(self._profile, seed, n, **kwargs)

    def chat(self, **kwargs: Any):
        """Open a multi-turn conversation rooted at this profile."""
        from ..generative.chat import Chat

        return Chat(self._profile, **kwargs)


__all__ = ["PersonaManager"]
