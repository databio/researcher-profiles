"""Talk to a profile: an LLM answers, chats, and riffs as the researcher.

Given a researcher's profile, this layer produces text in that researcher's
voice. It holds an Anthropic client wrapper (:mod:`.llm`), a multi-turn chat
interface (:mod:`.chat`), a builder for the persona system prompt
(:mod:`.persona`), retrieval calibration helpers (:mod:`.calibration`), and the
generative verbs ``_innovate`` and ``_riff`` (:mod:`.core`). Import each from
its own module.
"""
