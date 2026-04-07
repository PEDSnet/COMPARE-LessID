#!/bin/bash
set -e

FORCE=0
STATUS=0
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        --status|-s) STATUS=1 ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

if [ "$STATUS" -eq 1 ]; then
    echo "Site status:"
    for site_drnoc in "$CPT_BASE"/*/drnoc; do
        site_name="$(basename "$(dirname "$site_drnoc")")"
        site_out="$OUT_BASE/$site_name"
        mapping_csv="$LOOKUP_BASE/$site_name/mapping.csv"
        if [ -f "$site_out/_cpt_completed" ] && [ -f "$mapping_csv" ]; then
            mappings=$(awk -F, 'NR>1{n++} END{print n+0}' "$mapping_csv")
            echo "  [DONE]    $site_name  ($mappings mappings)"
        elif [ -d "$site_out" ]; then
            echo "  [PARTIAL] $site_name"
        else
            echo "  [PENDING] $site_name"
        fi
    done
    exit 0
fi

CPT_BASE="REDACTED:/data/sas_queries/<source_user>/compare_q01"
WORK_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_work"
OUT_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_drnoc"
LOOKUP_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_lookup"   # KEEP THIS RESTRICTED
LESSID_DIR="REDACTED:/home/<your_user>/lessid"
SUMMARY_FILE="$OUT_BASE/summary.txt"

mkdir -p "$WORK_BASE" "$OUT_BASE" "$LOOKUP_BASE"

CURRENT_SAS7BDAT_DIR=""
CURRENT_MAPPED_DIR=""

cleanup() {
    echo ""
    echo "[INTERRUPTED] Caught signal, cleaning up..."
    pkill -P $$ sas 2>/dev/null || true
    if [ -n "$CURRENT_SAS7BDAT_DIR" ] && [ -d "$CURRENT_SAS7BDAT_DIR" ]; then
        rm -rf "$CURRENT_SAS7BDAT_DIR"
    fi
    if [ -n "$CURRENT_MAPPED_DIR" ] && [ -d "$CURRENT_MAPPED_DIR" ]; then
        rm -rf "$CURRENT_MAPPED_DIR"
    fi
    echo "[INTERRUPTED] Re-run safely — completed sites will be skipped."
    exit 1
}

trap cleanup SIGINT SIGTERM

{
    echo "lessid CPT run summary"
    echo "Generated: $(date)"
    echo "Mode: deterministic per-site mapped IDs"
    echo ""
    printf "%-35s %8s %10s %10s %12s\n" "Site" "Tables" "Mappings" "CPT Size" "Status"
    printf "%-35s %8s %10s %10s %12s\n" "----" "------" "--------" "--------" "------"
} > "$SUMMARY_FILE"

for site_drnoc in "$CPT_BASE"/*/drnoc; do
    site_dir="$(dirname "$site_drnoc")"
    site_name="$(basename "$site_dir")"
    site_out="$OUT_BASE/$site_name"
    site_lookup="$LOOKUP_BASE/$site_name"
    mapping_csv="$site_lookup/mapping.csv"
    mapping_report="$site_lookup/mapping_report.txt"

    if [ -f "$site_out/_cpt_completed" ] && [ -f "$mapping_csv" ] && [ "$FORCE" -eq 0 ]; then
        echo "[SKIP] $site_name CPT already done and mapping exists."
        printf "%-35s %8s %10s %10s %12s\n" "$site_name" "-" "-" "-" "SKIPPED" >> "$SUMMARY_FILE"
        continue
    fi

    cpt_file="$(ls "$site_drnoc"/*.cpt 2>/dev/null | head -1)"
    if [ -z "$cpt_file" ]; then
        echo "[WARN] No CPT found for $site_name, skipping."
        printf "%-35s %8s %10s %10s %12s\n" "$site_name" "-" "-" "-" "NO CPT" >> "$SUMMARY_FILE"
        continue
    fi

    cpt_basename="$(basename "$cpt_file")"
    cpt_stem="${cpt_basename%.cpt}"

    sas7bdat_dir="$WORK_BASE/$site_name/sas7bdat"
    mapped_dir="$WORK_BASE/$site_name/mapped"
    cpt_ids_csv="$WORK_BASE/$site_name/cpt_id_values.csv"

    CURRENT_SAS7BDAT_DIR="$sas7bdat_dir"
    CURRENT_MAPPED_DIR="$mapped_dir"

    mkdir -p "$sas7bdat_dir" "$mapped_dir" "$site_out" "$site_lookup"

    echo ""
    echo "══════════════════════════════════════════════"
    echo " Site: $site_name"
    echo " CPT:  $cpt_file"
    echo "══════════════════════════════════════════════"

    echo "[1/5] proc cimport..."
    sas -nodms -stdio <<EOF
libname outlib "$sas7bdat_dir";
proc cimport infile="$cpt_file" library=outlib;
run;
EOF

    table_count=$(ls "$sas7bdat_dir"/*.sas7bdat 2>/dev/null | wc -l)
    echo "      Extracted $table_count datasets"

    echo "[2/5] Collecting CPT ID values..."
    sas -print "$mapped_dir/collect_ids.lst" -log "$mapped_dir/collect_ids.log" \
        -initstmt "%let input_lib_path = $sas7bdat_dir; %let output_csv = $cpt_ids_csv;" \
        "$LESSID_DIR/sas/collect_site_ids.sas"

    echo "[3/5] Building per-site mapping (CPT + XLSX)..."
    python3 "$LESSID_DIR/py/build_site_mapping.py" \
        "$site_name" \
        "$cpt_ids_csv" \
        "$site_drnoc" \
        "$mapping_csv" \
        "$mapping_report"

    echo "[4/5] Applying mapping to CPT datasets..."
    pushd "$LESSID_DIR" > /dev/null
    bash run.sh \
        "$sas7bdat_dir/*.sas7bdat" \
        "$mapped_dir" \
        "$mapping_csv" \
        "sas7bdat" \
        "0"
    popd > /dev/null

    echo "[5/5] proc cport -> mapped CPT..."
    out_cpt="$site_out/${cpt_stem}.cpt"
    sas -nodms -stdio <<EOF
libname inlib "$mapped_dir";
filename tranfile "$out_cpt";
proc cport library=inlib file=tranfile memtype=data;
run;
EOF

    mapping_count=$(awk -F, 'NR>1{n++} END{print n+0}' "$mapping_csv")
    cpt_size=$(du -sh "$site_out" | cut -f1)

    printf "%-35s %8s %10s %10s %12s\n" "$site_name" "$table_count" "$mapping_count" "$cpt_size" "OK" >> "$SUMMARY_FILE"

    touch "$site_out/_cpt_completed"

    echo "      Cleaning up intermediates..."
    rm -rf "$WORK_BASE/$site_name"
    CURRENT_SAS7BDAT_DIR=""
    CURRENT_MAPPED_DIR=""

    echo "      Mapping: $mapping_csv"
    echo "      Report:  $mapping_report"
    echo "      Done -> $site_out"
done

echo ""
echo "══════════════════════════════════════════════"
cat "$SUMMARY_FILE"
echo "══════════════════════════════════════════════"
echo "Summary: $SUMMARY_FILE"
