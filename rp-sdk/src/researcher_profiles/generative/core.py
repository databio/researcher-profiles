"""Generative persona verbs: ``innovate`` and ``riff``.

The bodies live here; :class:`researcher_profiles.profile.persona.PersonaManager`
imports and calls them, so ``prof.persona.innovate(...)`` is the caller-facing
name.

These methods reuse the shared :class:`~researcher_profiles.generative.llm.LLMClient`,
the persona builder, and the JSONL usage logger, so the persona prompt
prefix is cache-shared with ``.ask`` / ``.review``.
"""

import json
import re
from typing import Any, Optional

from ..models.results import GenerativeParseError, Idea, PersonaUnavailableError, Riff
from .llm import (
    DEFAULT_MAX_TOKENS,
    _get_llm,
    _log_usage,
    _render_evidence,
    _retrieval_errors,
    _source_type_kwargs,
)
from .persona import build_persona_system_blocks

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Parse a JSON object from ``text``, tolerating prose around it."""
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


def _generate_json(
    self,
    *,
    method: str,
    system_blocks,
    user_text: str,
    parse,
    log_fields: dict,
    source_ids: list,
    model: Optional[str],
    max_tokens: int,
    temperature: float,
    thinking: Optional[dict],
):
    """Run the shared generate-JSON scaffold: retry loop + parse + usage logging.

    ``parse`` maps the extracted JSON object to the return value; any parse
    failure triggers one corrective retry, then a :class:`GenerativeParseError`.
    ``log_fields`` are method-specific fields merged into each usage record.
    """
    messages: list[dict] = [{"role": "user", "content": user_text}]

    client = _get_llm(self)
    last_resp = None
    last_text = ""
    for attempt in range(2):
        resp = client.complete(
            system=system_blocks,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
        )
        last_resp = resp
        last_text = resp.text
        try:
            data = _extract_json(resp.text)
            result = parse(data)
            _log_usage(
                self,
                method=method,
                resp=resp,
                source_ids=source_ids,
                retries=attempt,
                **log_fields,
            )
            return result
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            # Append the bad output and a corrective turn, retry once.
            messages = messages + [
                {"role": "assistant", "content": resp.text},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON matching the "
                        "schema. Return ONLY the JSON object, with no preamble "
                        "or commentary."
                    ),
                },
            ]

    # Both attempts failed.
    if last_resp is not None:
        _log_usage(
            self,
            method=method,
            resp=last_resp,
            source_ids=source_ids,
            error="GenerativeParseError",
            **log_fields,
        )
    raise GenerativeParseError(f"{method}: failed to parse JSON after retry", raw_text=last_text)


def _innovate(
    self,
    topic: str,
    n: int = 3,
    *,
    k: int = 12,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    thinking: Optional[dict] = None,
    source_types: Optional[list[str]] = None,
) -> list[Idea]:
    """Propose ``n`` novel research directions in ``topic`` AS this researcher.

    Returns a list of :class:`Idea` dataclasses. Retries once on a JSON
    parse failure, then raises :class:`GenerativeParseError`.
    """
    if not self.has_persona:
        raise PersonaUnavailableError(self.slug)
    try:
        chunks = self.index.search(topic, k=k, **_source_type_kwargs(source_types))
    except _retrieval_errors():
        # No index on this backend (``CapabilityUnavailableError``), or an
        # unusable one: generate ungrounded rather than refusing.
        chunks = []
    evidence_text, citation_refs = _render_evidence(chunks)
    source_ids = [c.paper_id for c in citation_refs]

    system_blocks = build_persona_system_blocks(
        expertise_md=self.expertise,
        soul_md=self.soul,
        mode="innovate",
    )

    schema_block = (
        f"Produce {n} distinct research directions. Return a single JSON "
        "object with this exact shape:\n\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "hypothesis": "<one-sentence testable claim>",\n'
        '      "approach": "<2-4 sentences: data, method, comparison>",\n'
        '      "rationale": "<2-4 sentences: why this researcher specifically>",\n'
        '      "related_works": ["citation_key_1", "citation_key_2"]\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    parts: list[str] = [f"TOPIC: {topic}"]
    if evidence_text:
        parts.append(
            "RETRIEVED PRIOR WORK (use citation keys when grounding ideas):\n" + evidence_text
        )
    parts.append(schema_block)
    user_text = "\n\n".join(parts)

    def parse(data):
        ideas_data = data.get("ideas", []) if isinstance(data, dict) else []
        return [
            Idea(
                hypothesis=str(d.get("hypothesis", "")),
                approach=str(d.get("approach", "")),
                rationale=str(d.get("rationale", "")),
                related_works=list(d.get("related_works", []) or []),
            )
            for d in ideas_data
        ]

    return _generate_json(
        self,
        method="innovate",
        system_blocks=system_blocks,
        user_text=user_text,
        parse=parse,
        log_fields={"topic": topic[:200], "n": n, "k": k, "temperature": temperature},
        source_ids=source_ids,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking=thinking,
    )


def _riff(
    self,
    seed: str,
    n: int = 5,
    *,
    k: int = 4,
    model: Optional[str] = None,
    temperature: float = 1.0,
    max_tokens: int = 3072,
    thinking: Optional[dict] = None,
    source_types: Optional[list[str]] = None,
) -> list[Riff]:
    """Generate ``n`` divergent riffs on ``seed`` AS this researcher.

    Returns a list of :class:`Riff` dataclasses. Retries once on a JSON
    parse failure, then raises :class:`GenerativeParseError`.
    """
    if not self.has_persona:
        raise PersonaUnavailableError(self.slug)
    try:
        chunks = self.index.search(seed, k=k, **_source_type_kwargs(source_types))
    except _retrieval_errors():
        # See ``_innovate``: retrieval here is flavor, not the answer.
        chunks = []
    evidence_text, citation_refs = _render_evidence(chunks)
    source_ids = [c.paper_id for c in citation_refs]

    system_blocks = build_persona_system_blocks(
        expertise_md=self.expertise,
        soul_md=self.soul,
        mode="riff",
    )

    schema_block = (
        f"Produce {n} riffs. Return a single JSON object with this exact "
        "shape:\n\n"
        "{\n"
        '  "riffs": [\n'
        "    {\n"
        '      "angle": "<short label: contrarian | analogy | missing-abstraction | tooling-take | reframe | yes-and | freeform>",\n'
        '      "text": "<2-5 sentences in the researcher\'s voice>",\n'
        '      "related_work": "<optional citation key, or null>"\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    parts: list[str] = [f"SEED: {seed}"]
    if evidence_text:
        parts.append(
            "A FEW THINGS THIS PERSON HAS THOUGHT ABOUT NEARBY "
            "(sparse, for flavor only):\n" + evidence_text
        )
    parts.append(schema_block)
    user_text = "\n\n".join(parts)

    def parse(data):
        riffs_data = data.get("riffs", []) if isinstance(data, dict) else []
        return [
            Riff(
                angle=str(d.get("angle", "")),
                text=str(d.get("text", "")),
                related_work=(
                    d.get("related_work")
                    if d.get("related_work") not in (None, "", "null")
                    else None
                ),
            )
            for d in riffs_data
        ]

    return _generate_json(
        self,
        method="riff",
        system_blocks=system_blocks,
        user_text=user_text,
        parse=parse,
        log_fields={"seed": seed[:200], "n": n, "k": k, "temperature": temperature},
        source_ids=source_ids,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking=thinking,
    )


__all__ = ["GenerativeParseError", "Idea", "Riff"]
