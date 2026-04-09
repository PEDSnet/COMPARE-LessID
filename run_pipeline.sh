#!/bin/bash
# run_pipeline.sh — runs the full lessid pipeline end to end:
#   Phase 1: CPT  → cimport, collect IDs, build mapping, apply mapping, cport
#   Phase 2: XLSX → apply mapping + verify
#
# Usage:
#   bash run_pipeline.sh [--force|-f] [--cpt-only] [--xlsx-only]
#
# Logs are written to /tmp/ and also tee'd to stdout.

set -e

LESSID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FORCE_FLAG=""
RUN_CPT=1
RUN_XLSX=1

for arg in "$@"; do
    case "$arg" in
        --force|-f)   FORCE_FLAG="--force" ;;
        --cpt-only)   RUN_XLSX=0 ;;
        --xlsx-only)  RUN_CPT=0 ;;
        --help|-h)
            sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

pipeline_start=$(date +%s)
elapsed() {
    local secs=$(( $(date +%s) - pipeline_start ))
    printf '%02d:%02d:%02d' $((secs/3600)) $(( (secs%3600)/60 )) $((secs%60))
}
ts() { printf '[%s] ' "$(date +%H:%M:%S)"; }

LOG_DIR="/tmp/lessid_logs"
mkdir -p "$LOG_DIR"
timestamp=$(date +%Y%m%d_%H%M%S)
CPT_LOG="$LOG_DIR/cpt_${timestamp}.log"
XLSX_LOG="$LOG_DIR/xlsx_${timestamp}.log"

# ── Python venv setup ───────────────────────────────────────────────────────
VENV_DIR="$LESSID_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
REQUIREMENTS="$LESSID_DIR/requirements.txt"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Python venv not found at $VENV_DIR"
    echo "       Create it with: python3 -m venv $VENV_DIR"
    echo "                  then: $VENV_DIR/bin/pip install -r $REQUIREMENTS"
    exit 1
fi

# Check every package listed in requirements.txt is importable
if [ -f "$REQUIREMENTS" ]; then
    missing_pkgs=()
    while IFS='==' read -r pkg _version || [ -n "$pkg" ]; do
        pkg="$(echo "$pkg" | tr -d '[:space:]')"
        [ -z "$pkg" ] || [[ "$pkg" == \#* ]] && continue
        if ! "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
            missing_pkgs+=("$pkg")
        fi
    done < "$REQUIREMENTS"

    if [ "${#missing_pkgs[@]}" -gt 0 ]; then
        echo "ERROR: The following packages from requirements.txt are missing in the venv:"
        for p in "${missing_pkgs[@]}"; do echo "         - $p"; done
        echo "       Run: $VENV_DIR/bin/pip install -r $REQUIREMENTS"
        exit 1
    fi
fi

# Activate for subprocesses
# shellcheck source=venv/bin/activate
source "$VENV_DIR/bin/activate"

echo "╔══════════════════════════════════════════════╗"
echo "║           lessid pipeline starting           ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Started:   $(date '+%Y-%m-%d %H:%M:%S')"
[ -n "$FORCE_FLAG" ] && echo "║  Mode:      FORCE (reprocessing all sites)"
[ -z "$FORCE_FLAG" ] && echo "║  Mode:      resume (skipping completed sites)"
[ "$RUN_CPT"  -eq 1 ] && echo "║  Phase 1:   CPT   → $CPT_LOG"
[ "$RUN_XLSX" -eq 1 ] && echo "║  Phase 2:   XLSX  → $XLSX_LOG"
echo "╚══════════════════════════════════════════════╝"
echo ""

phase_start=$(date +%s)

# ── Phase 1: CPT ────────────────────────────────────────────────────────────
if [ "$RUN_CPT" -eq 1 ]; then
    echo "┌──────────────────────────────────────────────┐"
    echo "│  Phase 1: CPT processing                     │"
    echo "│  Start: $(date '+%H:%M:%S')                              │"
    echo "└──────────────────────────────────────────────┘"

    bash "$LESSID_DIR/run_all_sites.sh" $FORCE_FLAG 2>&1 | tee "$CPT_LOG"
    cpt_exit=${PIPESTATUS[0]}

    cpt_elapsed=$(( $(date +%s) - phase_start ))
    cpt_time=$(printf '%02d:%02d:%02d' $((cpt_elapsed/3600)) $(( (cpt_elapsed%3600)/60 )) $((cpt_elapsed%60)))

    echo ""
    if [ "$cpt_exit" -ne 0 ]; then
        echo "╔══════════════════════════════════════════════╗"
        echo "║  Phase 1 FAILED after ${cpt_time}                    ║"
        echo "║  Failed at: $(date '+%H:%M:%S')                          ║"
        echo "║  Log: $CPT_LOG"
        echo "╚══════════════════════════════════════════════╝"
        exit "$cpt_exit"
    fi
    echo "┌──────────────────────────────────────────────┐"
    echo "│  Phase 1 complete  (${cpt_time})                   │"
    echo "│  Finished: $(date '+%H:%M:%S')                           │"
    echo "└──────────────────────────────────────────────┘"
    echo ""
fi

# ── Phase 2: XLSX + verify ──────────────────────────────────────────────────
if [ "$RUN_XLSX" -eq 1 ]; then
    phase_start=$(date +%s)
    echo "┌──────────────────────────────────────────────┐"
    echo "│  Phase 2: XLSX processing + verification     │"
    echo "│  Start: $(date '+%H:%M:%S')                              │"
    echo "└──────────────────────────────────────────────┘"

    bash "$LESSID_DIR/run_all_xlsx.sh" $FORCE_FLAG 2>&1 | tee "$XLSX_LOG"
    xlsx_exit=${PIPESTATUS[0]}

    xlsx_elapsed=$(( $(date +%s) - phase_start ))
    xlsx_time=$(printf '%02d:%02d:%02d' $((xlsx_elapsed/3600)) $(( (xlsx_elapsed%3600)/60 )) $((xlsx_elapsed%60)))

    echo ""
    if [ "$xlsx_exit" -ne 0 ]; then
        echo "╔══════════════════════════════════════════════╗"
        echo "║  Phase 2 FAILED after ${xlsx_time}                   ║"
        echo "║  Failed at: $(date '+%H:%M:%S')                          ║"
        echo "║  Log: $XLSX_LOG"
        echo "╚══════════════════════════════════════════════╝"
        exit "$xlsx_exit"
    fi
    echo "┌──────────────────────────────────────────────┐"
    echo "│  Phase 2 complete  (${xlsx_time})                   │"
    echo "│  Finished: $(date '+%H:%M:%S')                           │"
    echo "└──────────────────────────────────────────────┘"
    echo ""
fi

# ── Final summary ───────────────────────────────────────────────────────────
total_elapsed=$(( $(date +%s) - pipeline_start ))
total_time=$(printf '%02d:%02d:%02d' $((total_elapsed/3600)) $(( (total_elapsed%3600)/60 )) $((total_elapsed%60)))

echo "╔══════════════════════════════════════════════╗"
echo "║           lessid pipeline complete           ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Finished:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "║  Total time:  ${total_time}"
[ "$RUN_CPT"  -eq 1 ] && echo "║  CPT log:     $CPT_LOG"
[ "$RUN_XLSX" -eq 1 ] && echo "║  XLSX log:    $XLSX_LOG"
echo "╚══════════════════════════════════════════════╝"
