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

# ── Host-side paths (edit these) ────────────────────────────────────────────

# Source CPT data (read-only)
HOST_SOURCE="/data/sas_queries/<study_owner>/<study>"

# De-identified output directory
HOST_OUTPUT="/data/sas_queries/<your_user>/lessid_drnoc"

# Mapping lookup directory (contains raw IDs — keep restricted)
HOST_LOOKUP="/data/sas_queries/<your_user>/lessid_lookup"

# Working directory for intermediate SAS datasets
HOST_WORK="/data/sas_queries/<your_user>/lessid_work"

# SAS installation root on this host
HOST_SAS="/data/sas_home/SASFoundation/9.4"

# Path to lessid.toml (real config with these host paths filled in)
HOST_CONFIG="$(dirname "$0")/config/lessid.toml"

# ── Container invocation ─────────────────────────────────────────────────────

sudo podman run --rm \
    -v "${HOST_CONFIG}:/app/config/lessid.toml:ro" \
    -v "${HOST_SOURCE}:/data/source:ro" \
    -v "${HOST_OUTPUT}:/data/output" \
    -v "${HOST_LOOKUP}:/data/lookup" \
    -v "${HOST_WORK}:/data/work" \
    -v "${HOST_SAS}:/host_sas:ro" \
    lessid "$@"
