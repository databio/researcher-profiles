"""The package's exception hierarchy.

``researcher_profiles.__init__`` re-exports these, so
``from researcher_profiles import ProfileError`` works.
"""

from typing import Any, Literal


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


# ---------------------------------------------------------------------------
# Typed service errors
#
# Raised by the service functions (``researcher_profiles.api.service``) and by
# the host hooks they call. Each adapter maps them to its own wire format once:
# ``api._errors`` to HTTP status codes, a host's MCP server to tool error codes.
# ---------------------------------------------------------------------------


class ServiceError(ProfileError):
    """Base of the typed errors a service function raises."""


class NotFound(ServiceError):
    """Nothing with this ref exists, or this caller may not see it (404).

    ``what`` is the whole sentence the caller is told, e.g.
    ``"profile 'x' not found"``.
    """

    def __init__(self, what: str):
        self.what = what
        super().__init__(what)


class Unauthenticated(ServiceError):
    """No credential, or one that resolves to nobody (401)."""

    def __init__(self, message: str = "login required"):
        self.message = message
        super().__init__(message)


class Forbidden(ServiceError):
    """Object-level refusal: the caller is known and may not do this (403).

    ``detail`` is the wire dict a host wants passed through as is (prosopia's
    ``{"error": "insufficient_access", "required", "missing", "hint"}``);
    without it the message is the detail.
    """

    def __init__(self, message: str = "forbidden", *, detail: dict | None = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class InsufficientScope(ServiceError):
    """The credential lacks a scope this needs; asking again could help (403)."""

    def __init__(
        self,
        needed: frozenset[str],
        *,
        missing: list[str] | None = None,
        hint: str | None = None,
    ):
        self.needed = frozenset(needed)
        self.missing = list(missing) if missing is not None else sorted(self.needed)
        self.hint = hint
        super().__init__(f"needs scope {', '.join(sorted(self.needed))}")


class Conflict(ServiceError):
    """The write was composed against another version, or the thing exists (409).

    ``kind`` says which version ``current`` is: a profile's content hash, a
    paper's version, or ``"exists"`` for a create that would overwrite.
    """

    def __init__(
        self,
        message: str,
        *,
        current: str | None = None,
        kind: Literal["profile", "paper", "exists"] = "profile",
    ):
        self.message = message
        self.current = current
        self.kind = kind
        super().__init__(message)


class Invalid(ServiceError):
    """The caller sent a value the function refuses (400).

    ``code`` is a stable machine code (``unknown_section``,
    ``cursor_mismatch``), ``valid`` the values that would have worked.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        valid: list[str] | None = None,
        hint: str | None = None,
    ):
        self.message = message
        self.code = code
        self.valid = valid
        self.hint = hint
        super().__init__(message)


class RateLimited(ServiceError):
    """Too many calls; retry after ``retry_after_s`` seconds (429)."""

    def __init__(self, retry_after_s: int, message: str = "too many requests"):
        self.retry_after_s = int(retry_after_s)
        self.message = message
        super().__init__(message)


__all__ = [
    "CapabilityUnavailableError",
    "Conflict",
    "Forbidden",
    "InsufficientScope",
    "Invalid",
    "NotFound",
    "ProfileError",
    "ProfileLoadError",
    "ProfileValidationError",
    "ProfileWriteError",
    "RateLimited",
    "ServiceError",
    "TransactionRequired",
    "Unauthenticated",
    "WriteHookError",
]
