"""researcher-profiles: a portable format for a researcher's structured record
of papers, expertise, funding, and career, with tools to read, search, and
serve it, whether it lives as a folder of files or in a database."""

from .build_state import BuildState, PaperBuildState
from .errors import (
    CapabilityUnavailableError,
    ProfileError,
    ProfileLoadError,
    ProfileValidationError,
    ProfileWriteError,
    TransactionRequired,
    WriteHookError,
)
from .models.results import (
    Citation,
    CitationRef,
    Coverage,
    GenerativeParseError,
    Idea,
    Match,
    MatchEvidence,
    PersonaResponse,
    PersonaUnavailableError,
    Riff,
    Topic,
)
from .profile import ResearcherProfile

# The knowledge-base export surface. Eager, not guarded: it is core-only
# (stdlib + pydantic + sibling core modules), so there is no extra to fail on
# and no reason for a consumer to discover it through a lazy attribute.
from .profile.export import (
    EXPORT_VERSION,
    ExportError,
    ExportOptions,
    ExportPaperRef,
    ExportVisibilityError,
    ProfileExportBundle,
    build_export_bundle,
    explore_url,
    export_content_hash,
    render_export_text,
    select_export_papers,
)
from .profile.write_unit import WriteContext, WriteHook
from .schema import (
    Anchor,
    ArtifactRef,
    CareerEntry,
    CareerStage,
    GrantRecord,
    GrantsDocument,
    Identifier,
    PaperRecord,
    PapersDocument,
    PaperStats,
    ProfileDocument,
    Provenance,
    ResearchOutput,
    SummaryFile,
    Training,
    normalize_doi,
)
from .schema.jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI, canonical_dumps

# The store interface. ``ProfileStore`` (the protocol), ``FilesystemProfileStore``
# and ``IngestResult`` are core. The filesystem backend needs no extra.
#
# ``SqlArtifactStorage`` / ``SqlProfileStore`` are absent from this list:
# see ``_LAZY`` and ``__getattr__`` below.
from .store import (
    FilesystemProfileStore,
    IngestResult,
    ProfileNotFoundError,
    ProfileStore,
    UploadError,
    build_store,
)

# The settings that come *before* a store exists: which cache root, which
# database URL, and the two environment variables that name them. A consumer
# that has to reach into ``researcher_profiles.store.config`` to answer "where
# is the cache" is using a private path for a public question. Core-only
# (os + pathlib), so eager costs nothing measurable.
from .store.config import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_CACHE_DIR,
    PROFILES_ROOT_ENV_VAR,
    ConfigError,
    DatabaseUrlNotConfigured,
    resolve_database_url,
    resolve_profiles_root,
)

# The cache-layout names that go with them. ``utils.paths`` is already loaded
# eagerly by the modules above, so this adds no import at all.
from .utils.paths import CACHE_DIRNAME, STORE_CACHE_DIRNAME, cache_dir

# The rule for this file: core -- stdlib + pydantic + scholarcore -- is
# imported eagerly, right here. Anything behind an optional extra goes in
# ``_LAZY`` below, never into an eager (or try/except-guarded) import.

#: Public name -> (submodule, extra). Imported on first access and cached into
#: globals(); a missing extra raises an ImportError that names it.
#:
#: Everything here sits behind an optional tier (anthropic, httpx, numpy,
#: SQLAlchemy). Importing any of them eagerly, guarded or not, would make every
#: ``import researcher_profiles`` pay that cost on a machine that has the extra,
#: and a guard only covers absence. The SDK is public and kept
#: dependency-light, so a consumer that only wants PaperRecord /
#: ProfileDocument / the wire models never triggers one of these imports. The
#: capability managers behind ``prof.persona``, ``prof.index`` and friends build
#: themselves lazily too, so nothing here is needed for them to work.
_LAZY: dict[str, tuple[str, str]] = {
    "DEFAULT_MODEL": (".generative.llm", "llm"),
    "LLMClient": (".generative.llm", "llm"),
    "LLMResponse": (".generative.llm", "llm"),
    "REFUSAL_THRESHOLD": (".generative.llm", "llm"),
    "Chat": (".generative.chat", "llm"),
    "Turn": (".generative.chat", "llm"),
    "ApiArtifactStorage": (".client", "client"),
    "StaticArtifactStorage": (".client", "client"),
    "SqlArtifactStorage": (".store.sql", "sql"),
    "SqlProfileStore": (".store.sql", "sql"),
}


def __getattr__(name: str):
    """PEP 562 hook: ``__version__`` and the lazy optional names.

    ``__version__`` is resolved from installed distribution metadata on first
    access and cached, so ``pyproject.toml`` stays the single declaration; the
    ``importlib.metadata`` import lives in the function body rather than at
    module scope because the core import must stay cheap (see
    ``tests/test_guardrails.py``), the same reason ``cli._version`` defers it.

    A name in :data:`_LAZY` is imported here, on first access, and cached into
    ``globals()`` so the cost is paid once. When its extra is not installed the
    import fails and the ImportError raised names the extra to install, so a
    core install that does ``from researcher_profiles import LLMClient`` fails
    loudly with an actionable message instead of handing back ``None``.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            resolved = version("researcher-profiles")
        except PackageNotFoundError:  # pragma: no cover - only when run from a tree
            resolved = "0+unknown"
        globals()["__version__"] = resolved  # bind once; __getattr__ never runs again
        return resolved

    lazy = _LAZY.get(name)
    if lazy is not None:
        from importlib import import_module

        submodule, extra = lazy
        try:
            module = import_module(submodule, __name__)
        except ImportError as e:
            raise ImportError(
                f"{name!r} requires the {extra!r} extra; see the install instructions in the README"
            ) from e
        value = getattr(module, name)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "Anchor",
    "ApiArtifactStorage",
    "ArtifactRef",
    "build_export_bundle",
    "build_store",
    "BuildState",
    "cache_dir",
    "CACHE_DIRNAME",
    "canonical_dumps",
    "CapabilityUnavailableError",
    "CareerEntry",
    "CareerStage",
    "Chat",
    "Citation",
    "CitationRef",
    "ConfigError",
    "CONTEXT_URL",
    "Coverage",
    "DATABASE_URL_ENV_VAR",
    "DatabaseUrlNotConfigured",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_MODEL",
    "explore_url",
    "export_content_hash",
    "EXPORT_VERSION",
    "ExportError",
    "ExportOptions",
    "ExportPaperRef",
    "ExportVisibilityError",
    "FilesystemProfileStore",
    "GenerativeParseError",
    "GrantRecord",
    "GrantsDocument",
    "Idea",
    "Identifier",
    "IngestResult",
    "LLMClient",
    "LLMResponse",
    "Match",
    "MatchEvidence",
    "normalize_doi",
    "PaperBuildState",
    "PaperRecord",
    "PapersDocument",
    "PaperStats",
    "PersonaResponse",
    "PersonaUnavailableError",
    "PROFILE_FORMAT_IRI",
    "ProfileDocument",
    "ProfileError",
    "ProfileExportBundle",
    "ProfileLoadError",
    "ProfileNotFoundError",
    "PROFILES_ROOT_ENV_VAR",
    "ProfileStore",
    "ProfileValidationError",
    "ProfileWriteError",
    "Provenance",
    "REFUSAL_THRESHOLD",
    "render_export_text",
    "ResearcherProfile",
    "ResearchOutput",
    "resolve_database_url",
    "resolve_profiles_root",
    "Riff",
    "select_export_papers",
    "SqlProfileStore",
    "SqlArtifactStorage",
    "StaticArtifactStorage",
    "STORE_CACHE_DIRNAME",
    "SummaryFile",
    "Topic",
    "Training",
    "TransactionRequired",
    "Turn",
    "UploadError",
    "WriteContext",
    "WriteHook",
    "WriteHookError",
]
