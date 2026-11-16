"""The v2 profile format (draft): migration from v1 and a v2 validator.

v2 is specified in researcher-profiles issues #4-#7. This package is the first
implementation, kept beside the v1 code so v1 readers and writers are untouched:

- ``sections``: level-2 sections of the prose files, keyed by heading text, and their hashes.
- ``provenance``: read and write ``provenance.json``, with inheritance down the record tree.
- ``stamp``: write section records for a prose file without an LLM.
- ``migrate``: ``migrate_profile`` rewrites a v1 profile directory as v2, offline.
- ``validate``: ``validate_v2`` checks a v2 profile directory.
"""

from .common import V2_CONTEXT
from .migrate import MigrationError, migrate_profile
from .validate import Finding, validate_v2

__all__ = ["V2_CONTEXT", "Finding", "MigrationError", "migrate_profile", "validate_v2"]
