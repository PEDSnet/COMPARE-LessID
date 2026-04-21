#!/usr/bin/env bash
# run_lessid.example.sh — wrapper to invoke the lessid container.
#
# Copy this file to run_lessid.sh and fill in your host paths.
# run_lessid.sh is gitignored (it contains site-specific host paths).
#
#   cp run_lessid.example.sh run_lessid.sh
#   chmod +x run_lessid.sh
#   # edit HOST_* variables below, then:
#   ./run_lessid.sh plan
#   ./run_lessid.sh run --xlsx-only --yes

set -euo pipefail

# ── Sudo guard ───────────────────────────────────────────────────────────────
# Running this wrapper under sudo causes podman to write root-owned files into
# your rootless runtime dir (/run/user/$UID/containers/), which breaks future
# rootless executions until those files are manually removed.
# Run as yourself: ./run_lessid.sh plan   (not: sudo ./run_lessid.sh plan)
if [[ -n "${SUDO_USER:-}" ]]; then
    _real_uid=$(id -u "${SUDO_USER}")
    echo "" >&2
    echo "WARNING: Running under sudo as ${SUDO_USER}." >&2
    echo "         root-owned files will be written to /run/user/${_real_uid}/containers/" >&2
    echo "         and will block future rootless podman runs for ${SUDO_USER}." >&2
    echo "         To fix afterwards: sudo rm -rf /run/user/${_real_uid}/containers/" >&2
    echo "         Run as yourself instead: ./run_lessid.sh plan" >&2
    echo "" >&2
    read -r -p "Continue anyway? [y/N] " _ans </dev/tty
    if [[ ! "${_ans}" =~ ^[Yy]$ ]]; then
        echo "Aborted." >&2
        exit 1
    fi
fi

# ── Host-side paths (edit these) ────────────────────────────────────────────

# Source CPT data (read-only)
HOST_SOURCE="/data/sas_queries/<study_owner>/<study>"

# De-identified output directory
HOST_OUTPUT="/data/sas_queries/<your_user>/lessid_drnoc"

# Mapping lookup directory (contains raw IDs — keep restricted)
HOST_LOOKUP="/data/sas_queries/<your_user>/lessid_lookup"

# Working directory for intermediate SAS datasets
HOST_WORK="/data/sas_queries/<your_user>/lessid_work"

# SAS binary on this host (auto-detected; override if needed)
HOST_SAS=$(which sas 2>/dev/null) || { echo "ERROR: sas not found on PATH. Install SAS or set HOST_SAS manually."; exit 1; }
HOST_SAS=$(readlink -f "${HOST_SAS}")  # resolve any wrapper symlink

# Path to lessid.toml (real config — uses generic container paths)
HOST_CONFIG="$(dirname "$0")/config/lessid.toml"

# ── Container invocation ─────────────────────────────────────────────────────

[[ "${1:-}" == "spotcheck" ]] && TTY_FLAGS="-it" || TTY_FLAGS=""

# Auto-capture log for run/verify — written to HOST_OUTPUT/logs/ alongside the data
if [[ "${1:-}" == "run" || "${1:-}" == "verify" ]]; then
    _logdir="${HOST_OUTPUT}/logs"
    mkdir -p "${_logdir}"
    _logfile="${_logdir}/lessid_${1}_$(date +%Y%m%d_%H%M%S).log"
    echo "Logging to: ${_logfile}" >&2
    exec > >(tee "${_logfile}") 2>&1
fi

podman run --rm ${TTY_FLAGS} \
    -v "${HOST_CONFIG}:/app/config/lessid.toml:ro" \
    -v "${HOST_SOURCE}:/data/source:ro" \
    -v "${HOST_OUTPUT}:/data/output" \
    -v "${HOST_LOOKUP}:/data/lookup" \
    -v "${HOST_WORK}:/data/work" \
    -v "${HOST_SAS}:/host_sas:ro" \
    lessid "$@"
