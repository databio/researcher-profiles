"""The three shared ``APIRouter`` objects the route modules attach to.

Declared once, in one place, so the ``/api/v1`` prefix and ``edit_router``'s
router-level ``Depends(require_owner)`` cannot drift between the route modules
that hang endpoints on them. Importing a route module registers its endpoints
on these.

- ``public_router`` carries the tier-projected read surface. "public" is not a
  property of the surface but of each artifact and each caller: an anonymous
  request is the viewer whose tier is ``public``.
- ``router`` gates every write / heavy / LLM endpoint per-endpoint by consumer
  scope (push, archive -> ``push``; search, match, rank-works -> ``match``;
  ask/review/innovate/riff -> ``persona``; identity/resolve -> ``resolve``).
  ``require_scope`` falls back to the operator token when no
  ``consumer_verifier`` is installed (bare rp-sdk), so single-token semantics
  are byte-identical there.
- ``edit_router`` is the owner-scoped interactive edit surface, gated by
  ``require_owner`` (the hook a management host fills with user sessions; falls
  back to the operator token on bare rp-sdk; see ``deps.require_owner``). Kept
  off ``router`` so these routes are owner-gated, not token-gated, when a
  session layer is present.
"""

from fastapi import APIRouter, Depends

from ..deps import require_owner

public_router = APIRouter(prefix="/api/v1")

router = APIRouter(prefix="/api/v1")

edit_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_owner)])
