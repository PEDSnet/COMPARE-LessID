#!/bin/bash
set -e

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

CPT_BASE="REDACTED:/data/sas_queries/<source_user>/compare_q01"
OUT_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_drnoc"
LOOKUP_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_lookup"
MAP_XLSX="REDACTED:/home/<your_user>/lessid/py/map_xlsx.py"
SUMMARY_FILE="$OUT_BASE/xlsx_summary.txt"

mkdir -p "$OUT_BASE"

{
    echo "lessid XLSX run summary"
    echo "Generated: $(date)"
    echo "Mode: mapping.csv replacement"
    echo ""
    printf "%-35s %8s %10s %12s\n" "Site" "Files" "Mappings" "Status"
    printf "%-35s %8s %10s %12s\n" "----" "-----" "--------" "------"
} > "$SUMMARY_FILE"

for site_drnoc in "$CPT_BASE"/*/drnoc; do
    site_name="$(basename "$(dirname "$site_drnoc")")"
    site_out="$OUT_BASE/$site_name"
    mapping_csv="$LOOKUP_BASE/$site_name/mapping.csv"

    if [ -f "$site_out/_xlsx_completed" ] && [ "$FORCE" -eq 0 ]; then
        echo "[SKIP] $site_name xlsx already done."
        printf "%-35s %8s %10s %12s\n" "$site_name" "-" "-" "SKIPPED" >> "$SUMMARY_FILE"
        continue
    fi

    if [ ! -f "$mapping_csv" ]; then
        echo "[WARN] $site_name has no mapping.csv, skipping xlsx."
        printf "%-35s %8s %10s %12s\n" "$site_name" "-" "-" "NO MAPPING" >> "$SUMMARY_FILE"
        continue
    fi

    mkdir -p "$site_out"

    xlsx_files=("$site_drnoc"/*.xlsx)
    if [ ! -f "${xlsx_files[0]}" ]; then
        echo "[WARN] No xlsx files found for $site_name, skipping."
        printf "%-35s %8s %10s %12s\n" "$site_name" "0" "-" "NO XLSX" >> "$SUMMARY_FILE"
        continue
    fi

    echo ""
    echo "══════════════════════════════════════════════"
    echo " Site: $site_name"
    echo " Mapping: $mapping_csv"
    echo "══════════════════════════════════════════════"

    processed=0
    for xlsx_file in "$site_drnoc"/*.xlsx; do
        [ -f "$xlsx_file" ] || continue
        xlsx_basename="$(basename "$xlsx_file")"
        out_xlsx="$site_out/$xlsx_basename"
        echo "  $xlsx_basename"
        python3 "$MAP_XLSX" "$mapping_csv" "$xlsx_file" "$out_xlsx"
        processed=$((processed + 1))
    done

    mapping_count=$(awk -F, 'NR>1{n++} END{print n+0}' "$mapping_csv")
    touch "$site_out/_xlsx_completed"
    printf "%-35s %8s %10s %12s\n" "$site_name" "$processed" "$mapping_count" "OK" >> "$SUMMARY_FILE"

    echo "  Done -> $site_out"
done

echo ""
echo "══════════════════════════════════════════════"
cat "$SUMMARY_FILE"
echo "══════════════════════════════════════════════"
echo "Summary: $SUMMARY_FILE"
