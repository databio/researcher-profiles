"""URL and request-shape helpers every client module shares.

Nothing here opens a connection. ``httpx`` is imported inside the functions
that talk to a server, so this module stays importable on a core-only install.
"""

from typing import Any, Optional
from urllib.parse import urlparse


def _split_profile_url(url: str) -> tuple[str, Optional[str]]:
    """Split a URL like ``http://host:port/api/v1/profiles/<slug>`` into
    ``(http://host:port, <slug>)``.

    If the URL has no ``/api/v1/profiles/<slug>`` suffix, returns
    ``(url_root, None)``.
    """
    u = url.rstrip("/")
    needle = "/api/v1/profiles/"
    if needle in u:
        base, _, slug_part = u.partition(needle)
        slug = slug_part.split("/", 1)[0] or None
        return base, slug
    parsed = urlparse(u)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else u
    return base, None


def _server_base(url: str) -> str:
    """Strip a trailing ``/api/v1/...`` while preserving any mount prefix.

    ``_split_profile_url`` drops the whole path in its no-slug branch, which
    would break a server mounted under a prefix such as ``/profiles``.
    """
    url = url.rstrip("/")
    marker = "/api/v1/"
    if marker in url:
        url = url[: url.index(marker)]
    return url


def auth_headers(token: Optional[str]) -> dict[str, str]:
    """The ``Authorization`` header for ``token``, or no headers when it is ``None``.

    Public so a caller with its own HTTP client sends the same header this
    module does.
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


def _compact(**fields: Any) -> dict[str, Any]:
    """A request body with every ``None``-valued field dropped.

    ``False`` is kept, so ``strict_corpus`` is always sent as a boolean. That
    matches the server default (``AskRequest.strict_corpus`` and
    ``ReviewRequest.strict_corpus`` both default to ``False``), so sending it
    explicitly changes nothing.
    """
    return {k: v for k, v in fields.items() if v is not None}
