#!/usr/bin/env bash
# Run the Python test suites in this repo.
#
# Two packages have one: rp-sdk (the profile SDK) and scholarcore (the shared
# academic vocabulary). CI (.github/workflows/tests.yml) invokes this same
# script, so local and CI cannot drift.
#
# Usage: ./scripts/test-all.sh [rp-sdk|scholarcore|all] [extra pytest args]
#
# The suite selector defaults to `all`, which is what a developer with both
# packages installed wants. CI passes an explicit one because each job installs
# only its own package, and running the other suite there would fail on imports,
# not on a real defect.
#
# With `all`, both suites run even if the first fails, so one run reports every
# problem.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rc=0

suite=all
case "${1-}" in
    rp-sdk | scholarcore | all)
        suite="$1"
        shift
        ;;
esac

if [[ "$suite" == "rp-sdk" || "$suite" == "all" ]]; then
    echo "==> rp-sdk suite"
    pytest "$repo/rp-sdk/tests" "$@" || rc=1
fi

if [[ "$suite" == "scholarcore" || "$suite" == "all" ]]; then
    echo "==> scholarcore suite"
    pytest "$repo/scholarcore/tests" "$@" || rc=1
fi

exit "$rc"
