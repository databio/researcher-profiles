"""researcher-profiles: a portable format for a researcher's structured record
of papers, expertise, funding, and career, with tools to read, search, and
serve it, whether it lives as a folder of files or in a database."""

from .build_state import BuildState, PaperBuildState
from .errors import (
    CapabilityUnavailableError,
    Conflict,
    Forbidden,
    InsufficientScope,
    Invalid,
    NotFound,
    ProfileError,
    ProfileLoadError,
    ProfileValidationError,
    ProfileWriteError,
    RateLimited,
    ServiceError,
    TransactionRequired,
    Unauthenticated,
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
    to_foaf,
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
    InterestConcept,
    PaperRecord,
    PapersDocument,
    PaperStats,
    ProfileDocument,
    Provenance,
    ResearchInterest,
    ResearchOutput,
    SummaryFile,
    Training,
    normalize_doi,
)
from .schema.jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI, canonical_dumps

# The SQL store classes need an extra, so they live in ``_LAZY`` below.
from .store import (
    FilesystemProfileStore,
    IngestResult,
    ProfileNotFoundError,
    ProfileStore,
    UploadError,
    build_store,
)
from .store.config import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_CACHE_DIR,
    PROFILES_ROOT_ENV_VAR,
    ConfigError,
    DatabaseUrlNotConfigured,
    resolve_database_url,
    resolve_profiles_root,
)
from .utils.paths import CACHE_DIRNAME, STORE_CACHE_DIRNAME, cache_dir

# Rule for this file: core (stdlib + pydantic + scholarcore) is imported
# eagerly above. Anything behind an optional extra goes in ``_LAZY``, never
# into an eager or try/except-guarded import, so ``import researcher_profiles``
# never pays for an extra it does not use.

#: Public name -> (submodule, extra). Imported on first access and cached into
#: globals(); a missing extra raises an ImportError that names it.
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

    ``__version__`` comes from installed package metadata, imported here
    rather than at module scope to keep the core import cheap. A missing extra
    raises an ImportError naming it, never returns ``None``.
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
    "Conflict",
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
    "Forbidden",
    "GenerativeParseError",
    "GrantRecord",
    "GrantsDocument",
    "Idea",
    "Identifier",
    "IngestResult",
    "InsufficientScope",
    "InterestConcept",
    "Invalid",
    "LLMClient",
    "LLMResponse",
    "Match",
    "MatchEvidence",
    "normalize_doi",
    "NotFound",
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
    "RateLimited",
    "REFUSAL_THRESHOLD",
    "render_export_text",
    "ResearcherProfile",
    "ResearchInterest",
    "ResearchOutput",
    "resolve_database_url",
    "resolve_profiles_root",
    "Riff",
    "select_export_papers",
    "ServiceError",
    "SqlProfileStore",
    "SqlArtifactStorage",
    "StaticArtifactStorage",
    "STORE_CACHE_DIRNAME",
    "SummaryFile",
    "to_foaf",
    "Topic",
    "Training",
    "TransactionRequired",
    "Turn",
    "UploadError",
    "Unauthenticated",
    "WriteContext",
    "WriteHook",
    "WriteHookError",
]
