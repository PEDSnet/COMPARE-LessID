#!/bin/bash
# scan_raw_cols.sh
#
# For each site, cimports the SOURCE CPT and lists every column ending in 'id'
# that would be remapped by the pipeline.
#
# Categories:
#   ALIAS   — medadmin_providerid, obsgen_providerid, obsclin_providerid,
#             rx_providerid, vx_providerid  (share providerid mapping key)
#   REMAP   — everything else ending in 'id'
#   SKIP    — participant id  (must not be remapped)
#
# Usage:
#   bash scan_raw_cols.sh                            # all sites
#   bash scan_raw_cols.sh C7LC_compare_deq_q01       # one site
#   bash scan_raw_cols.sh -o out.txt                 # all sites → file + stdout
#   bash scan_raw_cols.sh -o out.txt C7LC_compare_deq_q01

set -e

# ── output file option ────────────────────────────────────────────────────────
outfile=""
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            outfile="$2"; shift 2 ;;
        -o*)
            outfile="${1#-o}"; shift ;;
        *)
            args+=("$1"); shift ;;
    esac
done
set -- "${args[@]+"${args[@]}"}"

if [ -n "$outfile" ]; then
    exec > >(tee "$outfile") 2>&1
fi

_ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"
if [ -f "$_ENV_FILE" ]; then
    set -o allexport
    source "$_ENV_FILE"
    set +o allexport
fi

: "${CPT_BASE:?'Set CPT_BASE in .env'}"
: "${LOOKUP_BASE:?'Set LOOKUP_BASE in .env'}"

# ── which sites? ─────────────────────────────────────────────────────────────
if [ $# -ge 1 ]; then
    sites=("$@")
else
    sites=()
    for d in "$CPT_BASE"/*/drnoc; do
        [ -d "$d" ] && sites+=("$(basename "$(dirname "$d")")")
    done
fi

for site_name in "${sites[@]}"; do
    site_drnoc="$CPT_BASE/$site_name/drnoc"
    cpt_file="$(ls "$site_drnoc"/*.cpt 2>/dev/null | head -1 || true)"
    mapping_csv="$LOOKUP_BASE/$site_name/mapping.csv"

    if [ -z "$cpt_file" ]; then
        echo "[$site_name] SKIP — no source CPT found in $site_drnoc"
        continue
    fi

    echo ""
    echo "══════════════════════════════════════════════"
    echo " $site_name"
    echo " CPT: $cpt_file"
    echo "══════════════════════════════════════════════"

    TMP=$(mktemp -d)
    SAS_PROG=$(mktemp --suffix=.sas)
    SAS_LOG="${SAS_PROG%.sas}.log"

    cat > "$SAS_PROG" <<SAS
libname src "$TMP";
proc cimport infile="$cpt_file" library=src; run;

/* Find all columns that rules.py would touch, categorised */
proc sql noprint;
    create table cols_found as
    select
        memname,
        name,
        lowcase(strip(name)) as col_lc length=64,
        type,
        length,
        case
            /* ALIAS: provider-role columns sharing providerid key */
            when lowcase(strip(name)) in (
                    'medadmin_providerid','obsgen_providerid',
                    'obsclin_providerid','rx_providerid','vx_providerid')
                then 'ALIAS'
            /* REMAP: anything ending in id */
            else 'REMAP'
        end as category length=10
    from dictionary.columns
    where libname='SRC'
      and prxmatch('/id$/i', strip(name))
      and lowcase(strip(name)) not in ('participant id');
quit;

%let nfound = 0;
data _null_;
    if 0 then set cols_found nobs=n;
    call symputx('nfound', n);
    stop;
run;
%put NOTE: cols_found total: &nfound;

data _null_;
    set cols_found;
    put 'FOUND_COL cat=' category +(-1) ' table=' memname +(-1) ' col=' name +(-1) ' type=' type +(-1) ' len=' length;
run;

/* Sample values for each found column */
data _null_;
    set cols_found;
    length col_sql $4000;
    col_sql = catx(' ',
        'proc sql noprint;',
        'select distinct cats(', strip(name), ')',
        '  into :_samp separated by "  |  "',
        '  from src.', strip(memname),
        '  where not missing(', strip(name), ')',
        '  having monotonic() <= 5;',
        'quit;',
        '%put FOUND_SAMPLE col=', strip(name), ' vals=%superq(_samp);'
    );
    call execute(col_sql);
run;

proc datasets library=src kill nolist; run; quit;
libname src clear;
SAS

    sas -nodms -log "$SAS_LOG" "$SAS_PROG" 2>/dev/null || true

    if [ ! -f "$SAS_LOG" ]; then
        echo "  [ERROR] SAS log not found — SAS may have failed."
        rm -f "$SAS_PROG"
        rm -rf "$TMP"
        continue
    fi

    found_lines=$(grep '^FOUND_COL '    "$SAS_LOG" || true)
    sample_lines=$(grep '^FOUND_SAMPLE ' "$SAS_LOG" || true)
    nfound=$(grep 'NOTE: cols_found total:' "$SAS_LOG" | grep -oP '\d+$' | tail -1 || true)

    if [ -z "$nfound" ] || [ "$nfound" -eq 0 ]; then
        echo "  [OK] No tracked columns found in any table."
    else
        echo "  Found $nfound column(s):"
        echo ""

        prev_cat=""
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            cat=$(echo "$line"   | grep -oP 'cat=\K\S+'   || true)
            table=$(echo "$line" | grep -oP 'table=\K\S+' || true)
            col=$(echo "$line"   | grep -oP ' col=\K\S+'  || true)
            typ=$(echo "$line"   | grep -oP ' type=\K\S+' || true)
            len=$(echo "$line"   | grep -oP ' len=\K\S+'  || true)

            if [ "$cat" != "$prev_cat" ]; then
                case "$cat" in
                    REMAP)  echo "  ── REMAP  (values replaced with new ID) ──────────" ;;
                    ALIAS)  echo "  ── ALIAS  (remapped under providerid key) ─────────" ;;
                    *)      echo "  ── $cat ────────────────────────────────────────" ;;
                esac
                prev_cat="$cat"
            fi

            printf "  %-32s  table: %-25s %s(%s)\n" "$col" "$table" "$typ" "$len"

            sample_line=$(echo "$sample_lines" | grep " col=${col} " || true)
            vals=$(echo "$sample_line" | sed 's/.*vals=//' | head -1)
            [ -n "$vals" ] && printf "        samples: %s\n" "$vals"

            if [ -f "$mapping_csv" ]; then
                map_key="${col,,}"
                # resolve alias key
                case "$map_key" in
                    medadmin_providerid|obsgen_providerid|obsclin_providerid|rx_providerid|vx_providerid)
                        map_key="providerid" ;;
                esac
                in_map=$(awk -F, -v c="$map_key" 'NR>1 && tolower($1)==c {n++} END{print n+0}' "$mapping_csv" || true)
                if [ -n "$in_map" ] && [ "$in_map" -gt 0 ]; then
                    printf "        mapping.csv: %s entries (key: %s)\n" "$in_map" "$map_key"
                else
                    printf "        mapping.csv: not present\n"
                fi
            fi
            echo ""
        done <<< "$found_lines"
    fi

    rm -f "$SAS_PROG" "$SAS_LOG"
    rm -rf "$TMP"
done

