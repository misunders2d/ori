#!/usr/bin/env bash
# V2 scheduler phase scope guard — pre-commit invocation.
#
# Thin wrapper around scripts/check_phase_scope.py so .githooks/pre-commit
# stays readable. The Python script holds all the logic + allowlist;
# this file just plumbs uv to it.
#
# Exit non-zero blocks the commit. Override (rare, emergency-only) is
# via PHASE_OVERRIDE in the commit message body — handled by the CI
# workflow, not here. The local hook is intentionally strict.
#
# Phase tracking: scripts/check_phase_scope.py reads .v2-current-phase
# at repo root. If the file is absent the guard is inert.

set -e

exec uv run python scripts/check_phase_scope.py --staged
