"""The package's exception hierarchy, re-exported from ``researcher_profiles``."""

from typing import Any, Literal


class ProfileError(Exception):
    """Base exception for researcher_profiles."""


class ProfileLoadError(ProfileError):
    """Raised when a stored profile artifact is malformed or unreadable.

    ``location`` is untyped: a ``Path``, a URL, or a table/row reference,
    depending on the backend.
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
    """Raised when a profile artifact cannot be persisted, or the store is read-only.

    ``location`` is untyped, as in :class:`ProfileLoadError`.
    """

    def __init__(self, location: Any, message: str, original: Exception | None = None):
        self.location = location
        self.original = original
        super().__init__(f"{message} ({location})")


class WriteHookError(ProfileError):
    """A registered ``pre_commit_hook`` raised; the write is aborted.

    Deliberately not a :class:`ProfileWriteError`, which the HTTP layer maps to
    400 (the caller's fault). A failing hook is a server-side fault and must
    surface as 500.
    """

    def __init__(self, hook_name: str, original: Exception):
        self.hook_name = hook_name
        self.original = original
        super().__init__(f"pre-commit hook {hook_name} failed: {original}")


class TransactionRequired(ProfileError):
    """A hook maintains state that must be transactional; the backend is not.

    Raised by a hook author, not by the SDK, when handed
    ``WriteContext.atomic=False`` and a non-atomic write is not acceptable.
    """


class CapabilityUnavailableError(ProfileError, NotImplementedError):
    """A capability needs something this backend does not have.

    Both a :class:`ProfileError` and a ``NotImplementedError``, so either
    ``except`` clause catches it.
    """


# ---------------------------------------------------------------------------
# Typed service errors
#
# Raised by ``researcher_profiles.api.service`` and the host hooks it calls.
# Each adapter maps them to its own wire format (``api._errors`` for HTTP).
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

    ``detail`` is the wire dict a host wants passed through as is (for example
    ``{"error": "insufficient_access", "required", "missing", "hint"}``);
    without it the message is the detail.
    """

    def __init__(self, message: str = "forbidden", *, detail: dict | None = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class InsufficientScope(ServiceError):
    """The credential lacks a scope this needs; asking again could help (403).

    ``needed`` is the scope the credential lacks. ``required`` and ``missing``
    name what the write needed and what of it is missing, in the host's terms
    (for example, parts); they default to the scopes.
    """

    def __init__(
        self,
        needed: frozenset[str],
        *,
        missing: list[str] | None = None,
        required: list[str] | None = None,
        hint: str | None = None,
    ):
        self.needed = frozenset(needed)
        self.missing = list(missing) if missing is not None else sorted(self.needed)
        self.required = list(required) if required is not None else sorted(self.needed)
        self.hint = hint
        super().__init__(f"needs scope {', '.join(sorted(self.needed))}")


class Conflict(ServiceError):
    """The write was composed against another version, or the thing exists (409).

    ``kind`` says which version ``current`` is: a profile's content hash, a
    paper's version, or ``"exists"`` for a create that would overwrite.
    ``detail`` is the wire detail a host wants passed through as is (a plain
    sentence its interface matches on); without it the mapper builds one.
    """

    def __init__(
        self,
        message: str,
        *,
        current: str | None = None,
        kind: Literal["profile", "paper", "exists"] = "profile",
        detail: str | dict | None = None,
    ):
        self.message = message
        self.current = current
        self.kind = kind
        self.detail = detail
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
