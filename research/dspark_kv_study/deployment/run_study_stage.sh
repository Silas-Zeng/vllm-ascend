#!/usr/bin/env bash
# Prepend a frozen study/runtime prefix while retaining CANN's Python modules.
set -euo pipefail
stage_import_prefix=${1:?provide the frozen study import prefix}
shift
export PYTHONPATH="$stage_import_prefix${PYTHONPATH:+:$PYTHONPATH}"
exec "$@"
