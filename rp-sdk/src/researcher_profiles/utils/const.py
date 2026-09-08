"""Package-wide defaults for researcher_profiles."""

DEFAULT_BACKEND_SPEC = "st:all-MiniLM-L6-v2"
# Version of the embeddings sqlite index schema (.cache/embeddings.sqlite),
# Unrelated to the published profile format (which is gated on the
# `conformsTo` IRI, not an integer) and to the build sidecar's own counter.
INDEX_SCHEMA_VERSION = "1"

# The fixed sentence every publisher embeds so a consumer can verify its local
# model reproduces the publisher's vectors before trusting any cross-origin
# cosine. Changing this string invalidates every published probe.
EMBEDDING_PROBE_TEXT = (
    "A researcher profile describes a scientist's expertise, publications, and research interests."
)
