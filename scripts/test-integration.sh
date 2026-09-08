#!/usr/bin/env bash
# Run the researcher-profiles integration suite.
#
# Usage:
#   ./scripts/test-integration.sh           # non-LLM integration tests
#   ./scripts/test-integration.sh --llm     # also hit the real Anthropic API
#
# LLM tests cost ~$0.05 per full run; do not enable them in PR CI.
set -euo pipefail

RUN_LLM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --llm) RUN_LLM=1; shift ;;
    *) break ;;
  esac
done

MARKER="integration"
if [[ "$RUN_LLM" -eq 1 ]]; then
  export RUN_LLM_TESTS=true
  : "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY required for --llm}"
  MARKER="integration or llm"
fi

# Cleanup trap: remove any embeddings DB left in /tmp by tests.
cleanup() {
  find /tmp -maxdepth 5 -name 'embeddings.sqlite' -path '*/profile*' -mmin -60 -delete 2>/dev/null || true
}
trap cleanup EXIT

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec pytest "$repo/rp-sdk/tests/integration" -m "$MARKER" -v "$@"
