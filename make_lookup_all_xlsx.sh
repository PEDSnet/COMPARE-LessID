#!/bin/bash
# make_lookup_all_xlsx.sh
#
# Generates lookup CSVs (original_id -> hashed_id) from the ORIGINAL xlsx
# files.  Run this against the source data; keep the output in a separate
# access-controlled directory (LOOKUP_BASE).
#
# Output structure:
#   $LOOKUP_BASE/<site>/<xlsx_basename>_lookup.csv
set -e

set -a
source REDACTED:/data/sas_queries/<your_user>/.env
set +a

SALT="${SALT:?SALT not set in .env}"

CPT_BASE="REDACTED:/data/sas_queries/<source_user>/compare_q01"
LOOKUP_BASE="REDACTED:/data/sas_queries/<your_user>/lessid_lookup"   # KEEP THIS RESTRICTED
MAKE_LOOKUP="REDACTED:/home/<your_user>/lessid/make_lookup_xlsx.py"

mkdir -p "$LOOKUP_BASE"

for site_drnoc in "$CPT_BASE"/*/drnoc; do
    site_name="$(basename "$(dirname "$site_drnoc")")"
    site_lookup="$LOOKUP_BASE/$site_name"

    if [ -f "$site_lookup/_lookup_completed" ]; then
        echo "[SKIP] $site_name lookup already done."
        continue
    fi

    xlsx_files=("$site_drnoc"/*.xlsx)
    if [ ! -f "${xlsx_files[0]}" ]; then
        echo "[WARN] No xlsx files found for $site_name, skipping."
        continue
    fi

    mkdir -p "$site_lookup"

    echo ""
    echo "══════════════════════════════════════════════"
    echo " Site: $site_name"
    echo "══════════════════════════════════════════════"

    for xlsx_file in "$site_drnoc"/*.xlsx; do
        [ -f "$xlsx_file" ] || continue
        xlsx_basename="$(basename "$xlsx_file")"
        lookup_csv="$site_lookup/${xlsx_basename%.xlsx}_lookup.csv"
        echo "  $xlsx_basename"
        python3 "$MAKE_LOOKUP" "$SALT" "$xlsx_file" "$lookup_csv"
    done

    touch "$site_lookup/_lookup_completed"
    echo "  Done → $site_lookup"
done

echo ""
echo "All xlsx lookup tables generated."
echo "WARNING: $LOOKUP_BASE contains original IDs — restrict access."
