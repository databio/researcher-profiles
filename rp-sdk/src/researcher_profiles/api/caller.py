"""Who is asking: the one value every service function takes.

Built once per HTTP request or tool call; nothing below the adapter reads a
``Request``. A host that needs more on the caller subclasses this dataclass.
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
    #: Per-call memo for host hooks. Lives exactly as long as one request.
    memo: dict = field(default_factory=dict, compare=False, hash=False, repr=False)


#: The anonymous caller, for direct function calls. Its ``memo`` is shared by
#: everyone who uses this object, so an adapter builds ``Caller()`` per request
#: instead of handing this out.
ANONYMOUS = Caller()


__all__ = ["ANONYMOUS", "Caller"]
