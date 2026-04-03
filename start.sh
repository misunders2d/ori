#!/usr/bin/env bash
# DEPRECATED: Use ./launcher.sh instead.
# This script exists for backwards compatibility only.
echo ":: start.sh is deprecated. Redirecting to launcher.sh..."
exec "$(dirname "${BASH_SOURCE[0]}")/launcher.sh" "$@"
