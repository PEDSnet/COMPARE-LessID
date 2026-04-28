#!/usr/bin/env bash
# run_lessid.example.sh — thin launcher for the lessid pipeline.
#
# Copy to run_lessid.sh (gitignored) and set your paths in config/lessid.toml.
#
#   cp run_lessid.example.sh run_lessid.sh
#   cp config/lessid.example.toml config/lessid.toml
#   # edit config/lessid.toml with your host paths, then:
#   ./run_lessid.sh plan
#   ./run_lessid.sh run --yes

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${REPO_DIR}/.venv"
CONFIG="${REPO_DIR}/config/lessid.toml"

if [[ ! -f "${CONFIG}" ]]; then
    echo "ERROR: config/lessid.toml not found." >&2
    echo "       Run: cp config/lessid.example.toml config/lessid.toml" >&2
    echo "       Then edit config/lessid.toml with your host paths." >&2
    exit 1
fi

# Bootstrap venv on first run
if [[ ! -f "${VENV}/bin/python" ]]; then
    echo "Creating venv at ${VENV} ..." >&2
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
fi

# Auto-capture log for run/verify — written to out_base/logs/
if [[ "${1:-}" == "run" || "${1:-}" == "verify" ]]; then
    _OUT_BASE=$("${VENV}/bin/python3" -c \
        "import tomllib; print(tomllib.load(open('${CONFIG}','rb'))['paths']['out_base'])")
    _logdir="${_OUT_BASE}/logs"
    mkdir -p "${_logdir}"
    _logfile="${_logdir}/lessid_${1}_$(date +%Y%m%d_%H%M%S).log"
    echo "Logging to: ${_logfile}" >&2
    exec > >(tee "${_logfile}") 2>&1
fi

exec "${VENV}/bin/python" "${REPO_DIR}/src/pipeline.py" "$@"
