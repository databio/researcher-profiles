"""Who is asking: the one value every service function takes.

An adapter builds a :class:`Caller` once per HTTP request (``deps.get_caller``,
through ``Hooks.caller_resolver``) or once per MCP tool call (a host's own
builder), and hands it to the functions in :mod:`researcher_profiles.api.service`.
Nothing below the adapter reads a ``Request``.

rp-sdk never interprets ``scopes``: they are the host's vocabulary, read by the
host's own hooks (``Hooks.write_scope``). A host that needs more on the caller
(its principal, its key) subclasses this dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..privacy import ViewerTier

if TYPE_CHECKING:  # pragma: no cover
    from .deps import ConsumerIdentity


@dataclass(frozen=True)
class Caller:
    """The resolved caller of one request or one tool call."""

    #: Host vocabulary (e.g. ``profiles:read``); rp-sdk never interprets it.
    scopes: frozenset[str] = frozenset()
    #: The operator credential: reads everything, edits on bare rp-sdk.
    is_operator: bool = False
    #: The baseline read tier when no per-profile answer exists.
    tier: ViewerTier = "public"
    #: The consumer identity a ``consumer_verifier`` resolved, if any.
    consumer: Optional["ConsumerIdentity"] = None
    #: The real client address, for host rate limits.
    client_ip: Optional[str] = None
    #: A preview cap (``?as=``): every tier resolved for this caller is narrowed
    #: to it. A cap, never a widening.
    viewer_cap: Optional[ViewerTier] = None
    #: Per-call memo for host hooks (audience, publication state). A caller
    #: lives exactly as long as one request or one tool call, so this is the
    #: per-request memo and nothing longer.
    memo: dict = field(default_factory=dict, compare=False, hash=False, repr=False)


#: The anonymous caller, for direct function calls. Its ``memo`` is shared by
#: everyone who uses this object, so an adapter builds ``Caller()`` per request
#: instead of handing this out.
ANONYMOUS = Caller()


__all__ = ["ANONYMOUS", "Caller"]
