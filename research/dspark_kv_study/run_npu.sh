#!/usr/bin/env bash
# Use a subshell process; do not modify the user's global CANN environment.
set -eo pipefail
source "${1:?Usage: bash run_npu.sh /path/to/CANN/set_env.sh --output evidence/run.json [options]}"
shift
export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:${LD_LIBRARY_PATH:-}"
cd "$(dirname "$0")"
python3 benchmark_attention.py "$@"
