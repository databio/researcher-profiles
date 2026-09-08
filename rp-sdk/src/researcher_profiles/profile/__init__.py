"""The ``ResearcherProfile`` object and its per-profile capabilities.

:class:`ResearcherProfile` (in :mod:`.researcher_profile`) is the in-memory
handle for one profile: identity, caches, and the public write surface. The
per-profile capabilities it hands back live beside it, one module each:
:mod:`.cite`, :mod:`.coverage`, :mod:`.topics`, :mod:`.edit`, :mod:`.persona`,
:mod:`.index`, with :mod:`.storage` / :mod:`.write_unit` behind the write path
and :mod:`.export` / :mod:`.payloads` rendering a profile to output. Every public
name is re-exported from its own module; nothing is defined here.
"""

from ..errors import (
    CapabilityUnavailableError,
    ProfileError,
    ProfileValidationError,
    ProfileWriteError,
    WriteHookError,
)
from .researcher_profile import UNSET, ResearcherProfile
from .write_unit import WriteContext, WriteHook, WriteUnit

__all__ = [
    "ResearcherProfile",
    "CapabilityUnavailableError",
    "ProfileError",
    "ProfileValidationError",
    "ProfileWriteError",
    "WriteContext",
    "WriteHook",
    "WriteHookError",
    "WriteUnit",
    "UNSET",
]
