#!/usr/bin/env bash
# Reuse the supplied image, with a private Python package copy for overlays.
set -e
study_root=${1:?provide the experiment root}
shift
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh --cxx_abi=1
export PATH="/usr/local/python3.12.13/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/python3.12.13/lib:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$study_root/runtime:$study_root/study${PYTHONPATH:+:$PYTHONPATH}"
export OMP_NUM_THREADS=1
exec "$@"
