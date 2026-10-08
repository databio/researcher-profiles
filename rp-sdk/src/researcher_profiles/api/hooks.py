"""The host seams, one field per seam, kept on ``app.state.hooks``.

Every seam but the first takes a :class:`~researcher_profiles.api.caller.Caller`,
never a ``Request``, so a service function can run them with no HTTP in the
way (a host's MCP server calls the same functions the routes do). A bare
rp-sdk server leaves them at their defaults:

``caller_resolver(request) -> Caller``
    The HTTP adapter's one job: who is this request. Default:
    :func:`researcher_profiles.api.deps.default_caller_resolver` (operator
    token, else a consumer identity, else anonymous).
``viewer_resolver(caller, slug | None) -> ViewerTier``
    The most permissive tier this caller may be shown for that profile.
    Default: :func:`researcher_profiles.api.deps.resolve_viewer_tier`.
``profile_tier_floor(caller, profile, slug | None) -> TierFloor``
    A host ceiling on one profile, with its reason. Default: no floor.
``edit_gate(caller, profile, *, read_ok=False) -> None``
    May this caller edit this profile at all (``read_ok``: or read its owner
    tooling, the visibility report). Raises ``Unauthenticated``, ``Forbidden``
    or ``NotFound``. ``None``: the operator credential (or open mode, no token
    configured) edits, nobody else.
``write_scope(caller, profile, action, detail) -> None``
    May this caller make this specific write. Raises ``InsufficientScope`` or
    ``Forbidden``. ``None``: every write the edit gate admitted is allowed.
``record_edit(caller, profile, action, fields, content_hash) -> None``
    Called by every edit function after its write commits; where a host
    writes its audit row.
``registry_proofs(rid) -> list[Proof]``
    The registry-issued proofs to attach to a served document.
``store_for(caller, store) -> ProfileStore``
    The per-caller read view of the store. ``None``: the store itself. Never
    used by an edit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from ..privacy import ViewerTier
from .caller import Caller
from .deps import TierFloor, default_caller_resolver, resolve_viewer_tier


def _no_floor(_caller: Caller, _prof: Any, _slug: Optional[str]) -> TierFloor:
    return TierFloor()


@dataclass
class Hooks:
    """The host seams. Mutable: a host assigns the fields it fills."""

    caller_resolver: Callable[[Any], Caller] = field(default=default_caller_resolver)
    viewer_resolver: Callable[[Caller, Optional[str]], ViewerTier] = field(
        default=resolve_viewer_tier
    )
    profile_tier_floor: Callable[[Caller, Any, Optional[str]], TierFloor] = field(default=_no_floor)
    edit_gate: Optional[Callable[..., None]] = None
    write_scope: Optional[Callable[[Caller, Any, str, dict], None]] = None
    record_edit: Optional[Callable[[Caller, Any, str, list[str], Optional[str]], None]] = None
    registry_proofs: Optional[Callable[[str], list]] = None
    store_for: Optional[Callable[[Caller, Any], Any]] = None


__all__ = ["Hooks"]
