# How to use the persona methods

The `[llm]` extra exposes four persona methods on `prof.persona`
(`profile.persona.PersonaManager`, built lazily on first access). Each one
role-plays as the researcher, injecting their `expertise.md` and `SOUL.md`
into the system prompt and grounding the response in retrieved evidence from their
corpus.

!!! info "Prerequisites"
    - `researcher-profiles[llm,vectors,st]`
    - `ANTHROPIC_API_KEY` environment variable
    - A persona-ready profile (`full`/`deep` with `expertise.md` and `SOUL.md`)
    - Built embedding index (optional but recommended)

!!! caution "Anthropic-only"
    The persona methods use the Anthropic API exclusively. They require an
    `ANTHROPIC_API_KEY` and call Claude models. There is no provider
    abstraction. Switching to another LLM provider would require code changes
    in `researcher_profiles.llm`.

Loading a profile is enough to reach the four methods. Retrieval uses the profile's `search`, so build the index first for
grounded responses.

## ask (question answering in the researcher's voice)

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_files("path/to/jane-doe")
resp = p.persona.ask("What is your approach to region-set analysis?", k=5)
print(resp.text)
for c in resp.citations:
    print(c.paper_id, c.relevance)
print("grounded:", resp.grounded)
```

`ask` returns a `PersonaResponse` with `text`, `citations`, `model`, `usage`,
`request_id`, `refused`, `refusal_reason`, and `grounded`. Set `strict_corpus=True`
to refuse (no LLM call) when the top retrieval score is below `refusal_threshold`
(default `0.4`). Pass `history=[{"role": ..., "content": ...}]` for chat-style
turns.

## review (critique material from the researcher's perspective)

```python
resp = p.persona.review(
    "Aim 1: We will profile chromatin accessibility in 200 tumors using ATAC-seq...",
    focus="novelty and feasibility",
    k=5,
)
print(resp.text)
```

`review` returns the same `PersonaResponse`. `focus` seeds
retrieval and shapes the review; when omitted, retrieval is seeded from the first
lines of `material`. It supports the same `strict_corpus` / `refusal_threshold`
refusal gate as `ask`.

## innovate (propose novel research directions)

```python
ideas = p.persona.innovate("spatial chromatin organization in cancer", n=3, k=12)
for idea in ideas:
    print(idea.hypothesis)
    print(idea.approach)
    print(idea.related_works)
```

`innovate` returns a list of `Idea` dataclasses (`hypothesis`, `approach`,
`rationale`, `related_works`). The model is constrained to return JSON and retries
once on a parse failure, then raises `GenerativeParseError`. Temperature defaults
to `0.7`.

`related_works` are whatever the model emits and may not match real `paper_id`s.
Resolve them against the profile's papers:

```python
resolved = ideas[0].resolve_citations(p)  # {citation_key: PaperRecord | None}
```

## riff (divergent brainstorm fragments)

```python
riffs = p.persona.riff("every tool needs a config file", n=5)
for r in riffs:
    print(r.angle, "-", r.text)
```

`riff` returns a list of `Riff` dataclasses (`angle`, `text`, `related_work`).
Temperature defaults to `1.0` (higher than `innovate`) to encourage divergence.
Like `innovate`, it is JSON-constrained with one retry.

## When a profile has no persona

A persona method only works on a profile that has a synthesized persona:
non-empty `expertise.md` and `SOUL.md`. The
[`has_persona`](../reference/python-api.md#researcherprofile) property reports
this, and it is `True` only for a `full`/`deep`
[profile](../profile-format.md#profile-depth-levels) with both documents present.

Calling `ask`, `review`, `innovate`, or `riff` when `has_persona` is `False`
raises [`PersonaUnavailableError`](../reference/python-api.md#researcherprofile)
(a subclass of `ProfileError`) immediately, before any LLM call is made. The
common cause is a `lite` profile, which never synthesizes SOUL/expertise; the same
happens for any profile missing either document.

Guard the call by checking first:

```python
if p.has_persona:
    resp = p.persona.ask("What is your approach to region-set analysis?")
else:
    print(f"{p.slug} has no persona; skipping")
```

or by catching the error:

```python
from researcher_profiles.models.results import PersonaUnavailableError

try:
    resp = p.persona.ask("What is your approach to region-set analysis?")
except PersonaUnavailableError as e:
    print(f"no persona for {e.slug}")
```

## Over the API

The same four methods are exposed as `POST /api/v1/profiles/{slug}/{ask,review,innovate,riff}`
and are available on an `ApiArtifactStorage`-backed profile with identical signatures. See the
[API reference](../reference/api.md#post-apiv1profilesslugask) and
[How to serve a profile API](serve-a-profile-api.md).

When the profile has no synthesized persona (for example a `lite` profile, or one
missing SOUL/expertise), all four endpoints return 409 with
`{"detail": "..."}` and make no LLM call. This is distinct from the `500`/`502`
failure modes, which mean an LLM or parse failure on a persona-ready profile. The
HTTP client surfaces the 409 as a generic client error (any HTTP status `>= 400`
becomes a `RuntimeError`), not a typed `PersonaUnavailableError`. That typed
exception is raised only by the in-process methods. See the
[persona-409 note in the API reference](../reference/api.md#the-four-persona-endpoints).

## Persona obligations

When generating text in the subject's voice, consumers must follow these rules:

1. Provenance check: only present text as the subject's own statement if
   `provenance` is `orcid_verified` or `self_published`. Otherwise label the
   output as compiled or generated.
2. Grounding: ground all claims in profile content. Do not fabricate
   expertise or publications.
3. Staleness: note `dateModified` and the corpus end year if they suggest
   the profile may be out of date.
4. Authoritative constraints: honor `not_interests`. Do not improvise
   around them or suggest topics the researcher has explicitly excluded.

## Notes

- Every successful call makes at least one Anthropic API call; `innovate` and
  `riff` may make two on a JSON parse retry.
- The persona prefix (`expertise.md` + `SOUL.md`) is sent with prompt caching, so
  repeated calls against the same profile and mode reuse the cache. Check
  `usage.cache_read_input_tokens` to confirm cache hits.
- `strict_corpus` only applies to `ask` and `review`; `innovate` and `riff`
  always call the model.
- A missing `ANTHROPIC_API_KEY` surfaces only when a method is called, as an
  error from the Anthropic SDK.

