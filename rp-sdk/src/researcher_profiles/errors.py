"""The package's exception hierarchy.

``researcher_profiles.__init__`` re-exports these, so
``from researcher_profiles import ProfileError`` works.
"""

from typing import Any


class ProfileError(Exception):
    """Base exception for researcher_profiles."""


class ProfileLoadError(ProfileError):
    """Raised when a stored profile artifact is malformed or unreadable.

    ``location`` is untyped, mirroring :class:`ProfileWriteError`:
    the filesystem backend passes a ``Path``, a static host passes a URL, a
    database backend passes a table/row reference.
    """

    def __init__(self, location: Any, message: str, original: Exception | None = None):
        self.location = location
        self.original = original
        super().__init__(f"{message} ({location})")


class ProfileValidationError(ProfileError):
    """Raised when ``from_files(..., validate=True)`` produces a non-ok report."""

    def __init__(self, report: Any):
        self.report = report
        super().__init__(getattr(report, "format", lambda: "validation failed")())


class ProfileWriteError(ProfileError):
    """Raised when a profile artifact cannot be persisted, or when the
    backing store is read-only.

    Mirrors :class:`ProfileLoadError`'s signature, but ``location`` is
    untyped: the filesystem backend passes a ``Path``, a static
    host passes a URL, a database backend passes a table/row reference.
    """

    def __init__(self, location: Any, message: str, original: Exception | None = None):
        self.location = location
        self.original = original
        super().__init__(f"{message} ({location})")


class WriteHookError(ProfileError):
    """A registered ``pre_commit_hook`` raised; the write is aborted.

    Not a :class:`ProfileWriteError`. ``edit.py`` converts
    ``ProfileWriteError`` into an ``EditError`` that the HTTP layer maps to
    **400 Bad Request**, the right answer for a patch the caller got wrong. A
    failing hook is a *server-side* fault, so this class propagates through
    ``edit.py`` uncaught and surfaces as **500**. Do not "fix" that by making
    it a ``ProfileWriteError``.
    """

    def __init__(self, hook_name: str, original: Exception):
        self.hook_name = hook_name
        self.original = original
        super().__init__(f"pre-commit hook {hook_name} failed: {original}")


class TransactionRequired(ProfileError):
    """A hook maintains state that must be transactional; the backend is not.

    Raised *by a hook author*, not by the SDK. See
    :class:`researcher_profiles.profile.write_unit.WriteContext.atomic`: a hook handed
    ``atomic=False`` must consciously choose between raising this and degrading
    to a non-atomic write it has decided is acceptable.
    """


class CapabilityUnavailableError(ProfileError, NotImplementedError):
    """A capability needs something this backend does not have.

    both a :class:`ProfileError` and a ``NotImplementedError``, for the same
    reason ``ProfileNotFoundError`` is both a ``ProfileError`` and a
    ``KeyError``: existing callers catch ``NotImplementedError``, while a
    management host wants to catch it with the package's other errors.
    """


__all__ = [
    "CapabilityUnavailableError",
    "ProfileError",
    "ProfileLoadError",
    "ProfileValidationError",
    "ProfileWriteError",
    "TransactionRequired",
    "WriteHookError",
]
