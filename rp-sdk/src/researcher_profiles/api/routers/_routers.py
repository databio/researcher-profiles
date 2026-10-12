"""The three shared ``APIRouter`` objects, so the ``/api/v1`` prefix lives in one place.

- ``public_router``: the tier-projected read surface.
- ``router``: write, heavy and LLM endpoints, each gated by a consumer scope.
- ``edit_router``: owner-gated edits (``hooks.edit_gate``), kept off ``router``
  so a session layer can gate them by owner and ``deps.get_store`` never hands
  them a per-caller view.
"""

from fastapi import APIRouter

public_router = APIRouter(prefix="/api/v1")

router = APIRouter(prefix="/api/v1")

edit_router = APIRouter(prefix="/api/v1")
