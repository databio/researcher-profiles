"""Everything that calls a model.

The persona blocks a request is built from, the evidence rendered into it, the
client that wraps the SDK, and the four methods that go through all of it:
``.ask``, ``.review``, ``.innovate``, ``.riff``, plus stateful ``Chat``.
Every test here stubs the model; the ones that hit the real API live in
``tests/integration/test_llm_real.py`` under the ``llm`` marker, which
``addopts`` deselects by default.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from researcher_profiles import (
    Chat,
    GenerativeParseError,
    Idea,
    LLMClient,
    LLMResponse,
    PersonaResponse,
    Riff,
    Turn,
)
from researcher_profiles.generative import (
    llm as llm_mod,
)
from researcher_profiles.generative.persona import PERSONA_PREAMBLE, build_persona_system_blocks
from researcher_profiles.utils.paths import build_logs_dir

from .factories import fake_llm_response, stub_llm

# --------------------------------------------------------------------------
# Persona blocks, evidence, .ask / .review, LLMClient
# --------------------------------------------------------------------------


class _Chunk:
    def __init__(self, source_id, text, year=None, score=0.9, source_type="paper_summary"):
        self.source_id = source_id
        self.source_type = source_type
        self.text = text
        self.score = score
        self.meta = {"year": year} if year else {}


class TestPersonaBlocks:
    """The persona block builder: what every request carries as its system prompt."""

    @pytest.mark.parametrize(
        "mode, expected_marker",
        [
            ("ask", "MODE: ASK"),
            ("review", "MODE: REVIEW"),
            ("innovate", "MODE: INNOVATE"),
            ("riff", "MODE: RIFF"),
        ],
        ids=["ask", "review", "innovate", "riff"],
    )
    def test_persona_blocks_shape_and_mode(self, mode, expected_marker):
        blocks = build_persona_system_blocks(expertise_md="EXP", soul_md="SOUL", mode=mode)
        assert isinstance(blocks, list)
        assert blocks[0]["type"] == "text"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        # Persona content present.
        assert "EXPERTISE" in blocks[0]["text"]
        assert "SOUL" in blocks[0]["text"]
        assert PERSONA_PREAMBLE.split("\n")[0] in blocks[0]["text"]
        # Mode instructions appear AFTER the cache boundary.
        assert "cache_control" not in blocks[1]
        assert expected_marker in blocks[1]["text"]

    def test_persona_blocks_unknown_mode(self):
        with pytest.raises(ValueError):
            build_persona_system_blocks(
                expertise_md="EXP",
                soul_md="SOUL",
                mode="bogus",  # type: ignore[arg-type]
            )

    def test_persona_blocks_extra_instructions_appended(self):
        blocks = build_persona_system_blocks(
            expertise_md="E",
            soul_md="S",
            mode="ask",
            extra_instructions="Keep it short.",
        )
        assert blocks[-1]["text"] == "Keep it short."
        assert "cache_control" not in blocks[-1]


class TestEvidenceRendering:
    """``_render_evidence``: retrieved chunks turned into prompt text."""

    def test_render_evidence_empty(self):
        text, refs = llm_mod._render_evidence([])
        assert text == ""
        assert refs == []

    def test_render_evidence_dedups_source_ids(self):
        chunks = [
            _Chunk("paperA", "alpha", year=2020, score=0.8),
            _Chunk("paperA", "alpha-2", year=2020, score=0.95),
            _Chunk("paperB", "beta", year=2021, score=0.7),
        ]
        text, refs = llm_mod._render_evidence(chunks)
        ids = [r.paper_id for r in refs]
        assert set(ids) == {"paperA", "paperB"}
        # highest-score first
        assert ids[0] == "paperA"
        # dedupe keeps higher score for paperA
        paperA = [r for r in refs if r.paper_id == "paperA"][0]
        assert paperA.relevance == 0.95
        assert "paperA" in text
        assert "paperB" in text
        assert "EVIDENCE" in text


class TestAsk:
    """``.ask`` on a real profile, with a mocked LLM."""

    def test_ask_calls_llm_with_persona_system_blocks(self, jane_doe):
        fake = stub_llm(jane_doe, "an answer")

        resp = jane_doe.persona.ask("what is ExampleOverlap?")

        assert isinstance(resp, PersonaResponse)
        assert resp.text == "an answer"
        assert resp.citations == []
        assert resp.model == "claude-sonnet-4-6"
        assert resp.usage["cache_creation_input_tokens"] == 80

        fake.complete.assert_called_once()
        kwargs = fake.complete.call_args.kwargs
        system = kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "MODE: ASK" in system[1]["text"]
        messages = kwargs["messages"]
        assert messages[0]["role"] == "user"
        assert "QUESTION: what is ExampleOverlap?" in messages[0]["content"]

    def test_ask_threads_chunks_into_user_message(self, jane_doe):
        fake = stub_llm(jane_doe, "hello world")
        chunks = [
            _Chunk("doe2016example", "ExampleOverlap does enrichment.", year=2016),
            _Chunk("doe2019methods", "Union vs intersection.", year=2024),
        ]
        jane_doe.index.search = MagicMock(return_value=chunks)  # type: ignore[method-assign]

        resp = jane_doe.persona.ask("consensus regions?", k=3)

        assert sorted(c.paper_id for c in resp.citations) == sorted(
            ["doe2016example", "doe2019methods"]
        )
        kwargs = fake.complete.call_args.kwargs
        user_text = kwargs["messages"][0]["content"]
        assert "RETRIEVED EVIDENCE" in user_text
        assert "doe2016example" in user_text
        assert "QUESTION: consensus regions?" in user_text
        jane_doe.index.search.assert_called_once_with("consensus regions?", k=3)

    @pytest.mark.parametrize(
        "method, arg, call_kwargs, expected_fields, forbidden_substring",
        [
            ("ask", "hello?", {}, {"question": "hello?", "k": 5}, None),
            (
                "review",
                "TOPSECRET grant draft content",
                {"focus": "aims"},
                {"focus": "aims"},
                "TOPSECRET",
            ),
        ],
        ids=["ask", "review"],
    )
    def test_usage_log_records_the_call(
        self, jane_doe, method, arg, call_kwargs, expected_fields, forbidden_substring
    ):
        """One line per call, and ``.review`` never leaks the material into it."""
        stub_llm(jane_doe, "hello world")

        getattr(jane_doe.persona, method)(arg, **call_kwargs)

        log_path = build_logs_dir(jane_doe.directory) / "llm-usage.jsonl"
        assert log_path.is_file()
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["method"] == method
        assert rec["profile"] == "jane-doe"
        assert rec["model"] == "claude-sonnet-4-6"
        assert rec["request_id"] == "req_test_123"
        assert rec["usage"]["input_tokens"] == 100
        for key, value in expected_fields.items():
            assert rec[key] == value
        # Asserted on the raw line, not the parsed dict: the material must not
        # appear anywhere in it.
        if forbidden_substring is not None:
            assert forbidden_substring not in lines[0]

    def test_ask_truncates_long_question_in_log(self, jane_doe):
        stub_llm(jane_doe, "hello world")
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        long_q = "x" * 2000
        jane_doe.persona.ask(long_q)

        log_path = build_logs_dir(jane_doe.directory) / "llm-usage.jsonl"
        rec = json.loads(log_path.read_text().strip().splitlines()[-1])
        assert len(rec["question"]) <= 500


class TestReview:
    """``.review`` on a real profile, with a mocked LLM."""

    def test_review_basic(self, jane_doe):
        fake = stub_llm(jane_doe, "review text")
        chunks = [_Chunk("doe2019methods", "union vs intersection", year=2024)]
        jane_doe.index.search = MagicMock(return_value=chunks)  # type: ignore[method-assign]

        material = "We will take the union of all peak calls."
        resp = jane_doe.persona.review(material, focus="methods")

        assert isinstance(resp, PersonaResponse)
        assert resp.text == "review text"
        assert [c.paper_id for c in resp.citations] == ["doe2019methods"]

        kwargs = fake.complete.call_args.kwargs
        assert "MODE: REVIEW" in kwargs["system"][1]["text"]
        user_text = kwargs["messages"][0]["content"]
        assert "MATERIAL TO REVIEW" in user_text
        assert material in user_text
        assert "FOCUS: Pay particular attention to methods" in user_text
        # search should have used the focus string
        jane_doe.index.search.assert_called_once_with("methods", k=5)

    def test_review_without_focus_uses_material_prefix(self, jane_doe):
        stub_llm(jane_doe, "hello world")
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        material = "A short grant draft about chromatin accessibility."
        jane_doe.persona.review(material)

        # search should be called with a prefix of the material
        call_args = jane_doe.index.search.call_args
        assert call_args is not None
        assert material.startswith(call_args.args[0])


def _fake_anthropic_sdk(*, text="hi", model="claude-sonnet-4-6", request_id=None, **usage):
    """A stand-in ``anthropic`` module returning one text block.

    Returns ``(fake_anthropic, fake_sdk_client)``: patch the first into
    ``sys.modules`` and assert against the second's recorded ``messages.create``
    call. ``request_id=None`` leaves ``_request_id`` unset on the message.
    """
    counts = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 0,
    }
    counts.update(usage)

    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(type="text", text=text)]
    fake_msg.model = model
    fake_msg.stop_reason = "end_turn"
    for name, value in counts.items():
        setattr(fake_msg.usage, name, value)
    if request_id is not None:
        fake_msg._request_id = request_id

    fake_sdk_client = MagicMock()
    fake_sdk_client.messages.create.return_value = fake_msg

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_sdk_client
    return fake_anthropic, fake_sdk_client


class TestLLMClient:
    """``LLMClient``: lazy SDK import and ``complete()`` wrapping."""

    def test_llm_client_wraps_anthropic_response(self):
        fake_anthropic, fake_sdk_client = _fake_anthropic_sdk(request_id="req_xyz")

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            client = LLMClient(api_key="sk-test")
            resp = client.complete(
                system=[{"type": "text", "text": "sys"}],
                messages=[{"role": "user", "content": "hi"}],
            )

        assert isinstance(resp, LLMResponse)
        assert resp.text == "hi"
        assert resp.model == "claude-sonnet-4-6"
        assert resp.usage == {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 0,
        }
        assert resp.request_id == "req_xyz"
        assert fake_sdk_client.messages.create.call_args.kwargs["max_tokens"] == 4096

    @pytest.mark.parametrize(
        "complete_kwargs, expected_model, expected_thinking",
        [
            ({}, "claude-sonnet-4-6", None),
            (
                {"model": "claude-opus-4-7", "thinking": {"type": "adaptive"}},
                "claude-opus-4-7",
                {"type": "adaptive"},
            ),
        ],
        ids=["defaults", "model-and-thinking"],
    )
    def test_llm_client_threads_kwargs_to_the_sdk(
        self, complete_kwargs, expected_model, expected_thinking
    ):
        """``expected_thinking=None`` means the key must be absent, not null."""
        fake_anthropic, fake_sdk_client = _fake_anthropic_sdk()

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            client = LLMClient(api_key="sk-test")
            client.complete(
                system=[{"type": "text", "text": "s"}],
                messages=[{"role": "user", "content": "u"}],
                **complete_kwargs,
            )

        kwargs = fake_sdk_client.messages.create.call_args.kwargs
        assert kwargs["model"] == expected_model
        if expected_thinking is None:
            assert "thinking" not in kwargs
        else:
            assert kwargs["thinking"] == expected_thinking


# --------------------------------------------------------------------------
# The generative module: .innovate / .riff
# --------------------------------------------------------------------------


_GOOD_IDEAS_JSON = json.dumps(
    {
        "ideas": [
            {
                "hypothesis": "H1",
                "approach": "A1",
                "rationale": "R1",
                "related_works": ["doe2016example"],
            },
            {
                "hypothesis": "H2",
                "approach": "A2",
                "rationale": "R2",
                "related_works": [],
            },
            {
                "hypothesis": "H3",
                "approach": "A3",
                "rationale": "R3",
                "related_works": ["foo2020bar", "baz2021qux"],
            },
        ]
    }
)


_GOOD_RIFFS_JSON = json.dumps(
    {
        "riffs": [
            {"angle": "contrarian", "text": "T1", "related_work": "doe2016example"},
            {"angle": "analogy", "text": "T2", "related_work": None},
            {"angle": "tooling-take", "text": "T3"},
            {"angle": "reframe", "text": "T4", "related_work": "null"},
            {"angle": "yes-and", "text": "T5", "related_work": ""},
        ]
    }
)

# The good response each generative method parses, keyed by method name, so the
# innovate/riff mirror tests can look it up instead of carrying it as a column.
_GENERATIVE_RESPONSE = {"innovate": _GOOD_IDEAS_JSON, "riff": _GOOD_RIFFS_JSON}


class TestGenerativePersona:
    """The persona the generative modes share, and where they diverge."""

    def test_persona_block_identical_across_modes(self):
        """Persona prefix must match across ask/review/innovate/riff for cache hits."""
        ask = build_persona_system_blocks("E", "S", mode="ask")
        review = build_persona_system_blocks("E", "S", mode="review")
        innovate = build_persona_system_blocks("E", "S", mode="innovate")
        riff = build_persona_system_blocks("E", "S", mode="riff")
        assert ask[0]["text"] == review[0]["text"] == innovate[0]["text"] == riff[0]["text"]
        assert all(
            b[0]["cache_control"] == {"type": "ephemeral"} for b in (ask, review, innovate, riff)
        )

    # ----------------------------------------------------------------------
    # defaults / contract: .innovate and .riff mirrored
    # ----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "method, arg, expected_k, expected_temp, mode_tail",
        [
            ("innovate", "topic", 12, 0.7, "MODE: INNOVATE"),
            ("riff", "seed", 4, 1.0, "MODE: RIFF"),
        ],
        ids=["innovate", "riff"],
    )
    def test_generative_defaults(self, jane_doe, method, arg, expected_k, expected_temp, mode_tail):
        fake = stub_llm(jane_doe, _GENERATIVE_RESPONSE[method])

        getattr(jane_doe.persona, method)(arg)

        jane_doe.index.search.assert_called_once_with(arg, k=expected_k)
        kwargs = fake.complete.call_args.kwargs
        assert kwargs["temperature"] == expected_temp
        assert mode_tail in kwargs["system"][1]["text"]

    @pytest.mark.parametrize(
        "method, arg, arg_key, n, expected_k, expected_temp",
        [
            ("innovate", "a topic", "topic", 3, 12, 0.7),
            ("riff", "a seed", "seed", 5, 4, 1.0),
        ],
        ids=["innovate", "riff"],
    )
    def test_generative_writes_usage_jsonl(
        self, jane_doe, method, arg, arg_key, n, expected_k, expected_temp
    ):
        stub_llm(jane_doe, _GENERATIVE_RESPONSE[method])

        getattr(jane_doe.persona, method)(arg, n=n)

        log_path = build_logs_dir(jane_doe.directory) / "llm-usage.jsonl"
        rec = json.loads(log_path.read_text().strip().splitlines()[-1])
        assert rec["method"] == method
        assert rec["n"] == n
        assert rec["k"] == expected_k
        assert rec["temperature"] == expected_temp
        assert rec[arg_key] == arg

    @pytest.mark.parametrize(
        "method, arg, bad_text",
        [
            ("innovate", "topic", "garbage"),
            ("riff", "seed", "not json"),
        ],
        ids=["innovate", "riff"],
    )
    def test_generative_raises_on_persistent_bad_json(self, jane_doe, method, arg, bad_text):
        stub_llm(jane_doe, bad_text)

        with pytest.raises(GenerativeParseError) as excinfo:
            getattr(jane_doe.persona, method)(arg)
        assert bad_text in excinfo.value.raw_text


class TestInnovate:
    """``.innovate``: structured ideas, retried until the JSON parses."""

    def test_innovate_returns_n_ideas(self, jane_doe):
        fake = stub_llm(jane_doe, _GOOD_IDEAS_JSON)
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        ideas = jane_doe.persona.innovate("chromatin accessibility", n=3)

        assert len(ideas) == 3
        assert all(isinstance(i, Idea) for i in ideas)
        assert ideas[0].hypothesis == "H1"
        assert ideas[0].related_works == ["doe2016example"]
        assert ideas[1].related_works == []

        kwargs = fake.complete.call_args.kwargs
        assert kwargs["temperature"] == 0.7
        assert "MODE: INNOVATE" in kwargs["system"][1]["text"]
        assert "TOPIC: chromatin accessibility" in kwargs["messages"][0]["content"]

    def test_innovate_retries_on_bad_json(self, jane_doe):
        fake = stub_llm(jane_doe)
        fake.complete.side_effect = [
            fake_llm_response("not json at all"),
            fake_llm_response(_GOOD_IDEAS_JSON),
        ]
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        ideas = jane_doe.persona.innovate("topic", n=3)
        assert len(ideas) == 3
        assert fake.complete.call_count == 2
        # Second call should include the corrective turn
        second_call_messages = fake.complete.call_args_list[1].kwargs["messages"]
        assert any(
            "not valid JSON" in m["content"] for m in second_call_messages if m["role"] == "user"
        )

    def test_innovate_extracts_json_from_fenced_block(self, jane_doe):
        fenced = f"Sure, here you go:\n```json\n{_GOOD_IDEAS_JSON}\n```\n"
        stub_llm(jane_doe, fenced)
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        ideas = jane_doe.persona.innovate("topic")
        assert len(ideas) == 3

    def test_idea_resolve_citations(self, jane_doe):
        idea = Idea(
            hypothesis="h",
            approach="a",
            rationale="r",
            related_works=["__nonexistent_key__"],
        )
        out = idea.resolve_citations(jane_doe)
        assert "__nonexistent_key__" in out
        assert out["__nonexistent_key__"] is None


class TestRiff:
    """``.riff``: short reactions, optionally citing a paper."""

    def test_riff_returns_n_riffs_with_optional_citation(self, jane_doe):
        stub_llm(jane_doe, _GOOD_RIFFS_JSON)
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        riffs = jane_doe.persona.riff("seed concept", n=5)
        assert len(riffs) == 5
        assert all(isinstance(r, Riff) for r in riffs)
        assert riffs[0].related_work == "doe2016example"
        assert riffs[1].related_work is None
        assert riffs[2].related_work is None  # missing key -> None
        assert riffs[3].related_work is None  # "null" string -> None
        assert riffs[4].related_work is None  # "" -> None


# --------------------------------------------------------------------------
# Chat: a stateful conversation over a profile
# --------------------------------------------------------------------------


class TestChat:
    """Tests for the Chat module."""

    @pytest.fixture
    def stub_profile(self, jane_doe):
        """``jane-doe`` with a stubbed LLM answering "hi"; yields (profile, mock)."""
        return jane_doe, stub_llm(jane_doe, "hi")

    def test_chat_send_records_turns(self, stub_profile):
        prof, _fake = stub_profile
        chat = prof.persona.chat()
        chat.send("hello")
        assert len(chat.history) == 2
        assert chat.history[0].role == "user"
        assert chat.history[0].content == "hello"
        assert chat.history[1].role == "assistant"
        assert chat.history[1].content == "hi"

    def test_to_dict_from_dict_roundtrip(self, stub_profile):
        prof, _ = stub_profile
        chat = prof.persona.chat(k=3, strict_corpus=False, model="m1")
        chat.send("first")
        chat.send("second")
        data = chat.to_dict()
        assert data["slug"] == prof.slug
        assert data["k"] == 3
        assert data["model"] == "m1"
        assert len(data["history"]) == 4

        restored = Chat.from_dict(data, prof)
        assert restored.k == 3
        assert restored.model == "m1"
        assert len(restored.history) == 4
        assert all(isinstance(t, Turn) for t in restored.history)
        assert restored.history[0].content == "first"

    def test_from_dict_raises_on_slug_mismatch(self, stub_profile):
        prof, _ = stub_profile
        data = {"slug": "other-slug", "k": 5, "history": []}
        with pytest.raises(ValueError):
            Chat.from_dict(data, prof)

    def test_long_history_trimmed(self, stub_profile):
        prof, fake = stub_profile
        chat = prof.persona.chat(max_history_chars=100)
        # Inject huge prior turns directly:
        chat.history = [
            Turn(role="user", content="x" * 500, citations=[]),
            Turn(role="assistant", content="y" * 50, citations=[]),
        ]
        chat.send("now")
        # The huge user turn must have been trimmed before the LLM call.
        msgs = fake.complete.call_args.kwargs["messages"]
        contents = [m["content"] for m in msgs]
        assert not any(c == "x" * 500 for c in contents)

    def test_strict_corpus_propagated(self, stub_profile):
        prof, fake = stub_profile

        # Force a low-score hit so strict_corpus refusal triggers without calling LLM.
        class _Hit:
            source_id = "a"
            source_type = "paper_summary"
            text = "x"
            score = 0.0
            meta = {}

        prof.index.search = MagicMock(return_value=[_Hit()])  # type: ignore[method-assign]
        chat = prof.persona.chat(strict_corpus=True)
        resp = chat.send("out of scope?")
        assert resp.refused is True
        # No LLM call should have happened
        fake.complete.assert_not_called()
