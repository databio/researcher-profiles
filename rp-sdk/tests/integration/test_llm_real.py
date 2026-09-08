"""Real-API LLM integration tests.

These are doubly gated:
- selected by ``-m integration`` or ``-m llm`` (deselected by default)
- ``RUN_LLM_TESTS=true`` AND ``ANTHROPIC_API_KEY`` set (via
  ``anthropic_required`` fixture)

Each test asserts structure and presence only, never exact wording.
``capsys`` prints the response + usage so CI logs make the cost visible.
"""

import pytest

pytestmark = pytest.mark.llm


def _print_usage(capsys, label, resp):
    # PersonaResponse is a dataclass with required `text` and `usage`; access them
    # directly so a rename raises AttributeError instead of printing nothing.
    print(f"\n[{label}] text (first 200 chars): {resp.text[:200]!r}")
    print(f"[{label}] usage: {resp.usage}")


def test_ask_basic(anthropic_required, built_index_profile, capsys):
    resp = built_index_profile.persona.ask("What is your work on chromatin accessibility?")
    _print_usage(capsys, "ask_basic", resp)
    assert resp.text and len(resp.text) >= 50
    cites = list(resp.citations or [])
    assert len(cites) >= 1
    usage = dict(resp.usage or {})
    assert usage.get("input_tokens", 0) > 0


def test_ask_cache_hit(anthropic_required, built_index_profile, capsys):
    first = built_index_profile.persona.ask(
        "Summarize your contributions to ATAC-seq quality control."
    )
    second = built_index_profile.persona.ask(
        "Summarize your contributions to genome region analysis."
    )
    _print_usage(capsys, "ask_cache_first", first)
    _print_usage(capsys, "ask_cache_second", second)
    # llm.py builds usage["cache_read_input_tokens"] with `getattr(...) or 0`,
    # so it is never None and needs no `is None` branch. A cache miss on the
    # second identical-prefix call is a real regression.
    cache_read = dict(second.usage or {})["cache_read_input_tokens"]
    assert cache_read > 0


def test_innovate(anthropic_required, built_index_profile, capsys):
    ideas = built_index_profile.persona.innovate("ATAC-seq quality control", n=2)
    print(f"\n[innovate] got {len(ideas)} ideas")
    for i, idea in enumerate(ideas):
        print(f"  idea[{i}]: hypothesis={idea.hypothesis[:120]!r}")
    assert len(ideas) >= 1
    first = ideas[0]
    # Idea is a dataclass with four required fields, and generative.py fills every
    # one of them. Attribute access is required: a rename should raise
    # AttributeError here, not silently become "".
    assert first.hypothesis
    assert first.approach
    assert first.rationale
