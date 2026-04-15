#!/bin/bash
set -e

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# Load .env from the repo root (values already in the environment take precedence)
_ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"
if [ -f "$_ENV_FILE" ]; then
    set -o allexport
    # shellcheck source=.env
    source "$_ENV_FILE"
    set +o allexport
fi

: "${CPT_BASE:?'.env or environment must set CPT_BASE'}"
: "${OUT_BASE:?'.env or environment must set OUT_BASE'}"
: "${LOOKUP_BASE:?'.env or environment must set LOOKUP_BASE (KEEP RESTRICTED)'}"
: "${LESSID_DIR:?'.env or environment must set LESSID_DIR'}"

MAP_XLSX="$LESSID_DIR/py/map_xlsx.py"
SUMMARY_FILE="$OUT_BASE/xlsx_summary.txt"

mkdir -p "$OUT_BASE"

pipeline_start=$(date +%s)
elapsed() {
    local secs=$(( $(date +%s) - pipeline_start ))
    printf '%02d:%02d:%02d' $((secs/3600)) $(( (secs%3600)/60 )) $((secs%60))
}
step_elapsed() {
    local secs=$(( $(date +%s) - $1 ))
    printf '%dm%02ds' $((secs/60)) $((secs%60))
}
ts() { printf '[%s] ' "$(date +%H:%M:%S)"; }

{
    echo "lessid XLSX run summary"
    echo "Generated: $(date)"
    echo "Mode: mapping.csv replacement"
    echo ""
    printf "%-35s %8s %10s %8s %12s\n" "Site" "Files" "Mappings" "Time" "Status"
    printf "%-35s %8s %10s %8s %12s\n" "----" "-----" "--------" "----" "------"
} > "$SUMMARY_FILE"

for site_drnoc in "$CPT_BASE"/*/drnoc; do
    site_name="$(basename "$(dirname "$site_drnoc")")"
    site_out="$OUT_BASE/$site_name"
    mapping_csv="$LOOKUP_BASE/$site_name/mapping.csv"

    if [ -f "$site_out/_xlsx_completed" ] && [ "$FORCE" -eq 0 ]; then
        echo "[SKIP] $site_name xlsx already done."
        printf "%-35s %8s %10s %8s %12s\n" "$site_name" "-" "-" "-" "SKIPPED" >> "$SUMMARY_FILE"
        continue
    fi

    if [ ! -f "$mapping_csv" ]; then
        echo "[WARN] $site_name has no mapping.csv, skipping xlsx."
        printf "%-35s %8s %10s %8s %12s\n" "$site_name" "-" "-" "-" "NO MAPPING" >> "$SUMMARY_FILE"
        continue
    fi

    mkdir -p "$site_out"

    xlsx_files=("$site_drnoc"/*.xlsx)
    if [ ! -f "${xlsx_files[0]}" ]; then
        echo "[WARN] No xlsx files found for $site_name, skipping."
        printf "%-35s %8s %10s %8s %12s\n" "$site_name" "0" "-" "-" "NO XLSX" >> "$SUMMARY_FILE"
        continue
    fi

    echo ""
    echo "══════════════════════════════════════════════"
    echo " Site: $site_name  [+$(elapsed) total]"
    echo " Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo " Mapping: $mapping_csv"
    echo "══════════════════════════════════════════════"
    site_start=$(date +%s)

    processed=0
    for xlsx_file in "$site_drnoc"/*.xlsx; do
        [ -f "$xlsx_file" ] || continue
        xlsx_basename="$(basename "$xlsx_file")"
        # EDC discrepancy files contain raw EDC data and must not be de-identified
        if [[ "$xlsx_basename" == *edc_discrepancies* ]]; then
            echo "$(ts)  $xlsx_basename  [SKIP — edc_discrepancies]"
            cp "$xlsx_file" "$site_out/$xlsx_basename"
            continue
        fi
        out_xlsx="$site_out/$xlsx_basename"
        echo "$(ts)  $xlsx_basename"
        python3 "$MAP_XLSX" "$mapping_csv" "$xlsx_file" "$out_xlsx"
        processed=$((processed + 1))
    done

    mapping_count=$(awk -F, 'NR>1{n++} END{print n+0}' "$mapping_csv")
    touch "$site_out/_xlsx_completed"
    printf "%-35s %8s %10s %8s %12s\n" "$site_name" "$processed" "$mapping_count" "$(step_elapsed $site_start)" "OK" >> "$SUMMARY_FILE"

    echo "$(ts)  Done -> $site_out  [$(step_elapsed $site_start)]"
done

echo ""
echo "══════════════════════════════════════════════"
cat "$SUMMARY_FILE"
echo "══════════════════════════════════════════════"
echo "Summary: $SUMMARY_FILE"
echo "Total elapsed: $(elapsed)"

# ── Verification pass ──────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo " Verification"
echo "══════════════════════════════════════════════"

VENV_PYTHON="$LESSID_DIR/venv/bin/python3"
VERIFY_SCRIPT="$LESSID_DIR/py/verify.py"
VERIFY_SUMMARY="$OUT_BASE/verify_summary.txt"
verify_pass=0
verify_fail=0

{
    echo "lessid verification summary"
    echo "Generated: $(date)"
    echo ""
    printf "%-35s %12s\n" "Site" "Result"
    printf "%-35s %12s\n" "----" "------"
} > "$VERIFY_SUMMARY"

for site_drnoc in "$CPT_BASE"/*/drnoc; do
    site_name="$(basename "$(dirname "$site_drnoc")")"
    site_out="$OUT_BASE/$site_name"
    if [ ! -f "$site_out/_cpt_completed" ] && [ ! -f "$site_out/_xlsx_completed" ]; then
        continue
    fi
    echo " Verifying $site_name..."
    if "$VENV_PYTHON" "$VERIFY_SCRIPT" "$site_name" 2>/dev/null; then
        verify_pass=$((verify_pass + 1))
        printf "%-35s %12s\n" "$site_name" "PASS" >> "$VERIFY_SUMMARY"
    else
        verify_fail=$((verify_fail + 1))
        printf "%-35s %12s\n" "$site_name" "FAIL" >> "$VERIFY_SUMMARY"
    fi
done

echo ""
echo "══════════════════════════════════════════════"
cat "$VERIFY_SUMMARY"
echo "══════════════════════════════════════════════"
echo "Verify summary: $VERIFY_SUMMARY"
echo ""
if [ "$verify_fail" -eq 0 ]; then
    echo "All $verify_pass site(s) passed verification."
else
    echo "WARNING: $verify_fail site(s) FAILED verification. Check output above."
    exit 1
fi
