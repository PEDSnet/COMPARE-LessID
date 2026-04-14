#!/usr/bin/env python3
"""
List all columns that will be remapped by the pipeline, across all sites.

Usage:
    python3 py/list_remap_cols.py                          # all sites
    python3 py/list_remap_cols.py C7LC_compare_deq_q01    # one site
    python3 py/list_remap_cols.py C7LC C7NCH -o cols.csv  # subset + CSV
"""
import os, sys, csv, glob, tempfile, subprocess, shutil, argparse
from collections import defaultdict
from pathlib import Path

# ── Load .env ────────────────────────────────────────────────────────────────
env_file = Path(__file__).parent.parent / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CPT_BASE = os.environ.get('CPT_BASE')

# ── Import rules ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from rules import is_remap_col, mapping_col, prefix_for

# ── Known CDM tables ─────────────────────────────────────────────────────────
KNOWN_TABLES = (
    'DEMOGRAPHIC', 'ENCOUNTER', 'DIAGNOSIS', 'PROCEDURES', 'VITAL',
    'DISPENSING', 'LAB_RESULT_CM', 'PRESCRIBING', 'PCORNET_TRIAL',
    'DEATH', 'MED_ADMIN', 'PROVIDER', 'HARVEST', 'LDS_ADDRESS_HISTORY',
    'EXTERNAL_MEDS',
)


def find_all_sites() -> list[str]:
    """Return all site names found under CPT_BASE."""
    if not CPT_BASE:
        sys.exit("ERROR: CPT_BASE not set — check .env")
    sites = []
    for d in sorted(glob.glob(f"{CPT_BASE}/*/drnoc")):
        sites.append(Path(d).parent.name)
    if not sites:
        sys.exit(f"ERROR: No sites found under {CPT_BASE}")
    return sites


def find_cpt(site_or_path: str) -> str:
    # Direct path to a .cpt file
    if site_or_path.endswith('.cpt') and os.path.isfile(site_or_path):
        return site_or_path
    # Full path to a drnoc/ directory
    if os.path.isdir(site_or_path):
        files = glob.glob(os.path.join(site_or_path, '*.cpt'))
        if not files:
            sys.exit(f"ERROR: No CPT file found in {site_or_path}")
        return sorted(files)[-1]
    # Site name — look up via CPT_BASE
    pattern = f"{CPT_BASE}/{site_or_path}/drnoc/*.cpt"
    files = glob.glob(pattern)
    if not files:
        sys.exit(f"ERROR: No CPT file found matching {pattern}")
    return sorted(files)[-1]


def get_columns_from_cpt(cpt_file: str) -> list[tuple[str, str]]:
    """Cimport the CPT into a temp libname and query dictionary.columns via SAS."""
    tmp = tempfile.mkdtemp()
    sas_prog = tempfile.NamedTemporaryFile(suffix='.sas', delete=False, mode='w')
    sas_log  = sas_prog.name.replace('.sas', '.log')

    tables_in = ', '.join(f"'{t}'" for t in KNOWN_TABLES)

    sas_prog.write(f"""\
libname src "{tmp}";
proc cimport infile="{cpt_file}" library=src; run;

proc sql noprint;
    create table _meta as
    select upcase(memname) as memname, name
    from dictionary.columns
    where libname='SRC'
      and upcase(memname) in ({tables_in})
    order by memname, name;
quit;

data _null_;
    set _meta;
    put 'COLMETA table=' memname +(-1) ' col=' name;
run;

proc datasets library=src kill nolist; run; quit;
libname src clear;
""")
    sas_prog.flush()
    sas_prog.close()

    subprocess.run(
        ['sas', '-nodms', '-log', sas_log, sas_prog.name],
        capture_output=True,
    )

    cols = []
    if os.path.exists(sas_log):
        with open(sas_log) as f:
            for line in f:
                line = line.strip()
                if not line.startswith('COLMETA '):
                    continue
                parts = {}
                for tok in line[len('COLMETA '):].split():
                    k, _, v = tok.partition('=')
                    parts[k] = v
                if 'table' in parts and 'col' in parts:
                    cols.append((parts['table'], parts['col'].lower()))

    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.unlink(sas_prog.name)
        os.unlink(sas_log)
    except OSError:
        pass

    return cols


def main():
    parser = argparse.ArgumentParser(description='List columns remapped by lessid pipeline across all sites.')
    parser.add_argument('sites', nargs='*', help='Site name(s), drnoc/ dir(s), or .cpt path(s). Defaults to all sites.')
    parser.add_argument('-o', '--output', help='Write results to CSV file (in addition to stdout)')
    args = parser.parse_args()

    site_args = args.sites or find_all_sites()
    print(f"Scanning {len(site_args)} site(s)...", file=sys.stderr)

    # ── Collect columns per site ─────────────────────────────────────────────
    # col_sites[(table, column)] = set of site names that have it
    col_sites: dict[tuple[str, str], set[str]] = defaultdict(set)
    # col_meta[(table, column)] = {prefix, mapping_key}
    col_meta: dict[tuple[str, str], dict] = {}

    all_site_names: list[str] = []

    for site_arg in site_args:
        cpt_file = find_cpt(site_arg)
        site_name = Path(cpt_file).parent.parent.name  # drnoc/../ = site dir
        all_site_names.append(site_name)
        print(f"  {site_name}: {cpt_file}", file=sys.stderr)

        all_cols = get_columns_from_cpt(cpt_file)
        for table, col in all_cols:
            if is_remap_col(col):
                key = (table, col)
                col_sites[key].add(site_name)
                if key not in col_meta:
                    col_meta[key] = {
                        'prefix':      prefix_for(col),
                        'mapping_key': mapping_col(col),
                    }

    n_sites = len(all_site_names)
    all_site_set = set(all_site_names)

    # ── Build rows ───────────────────────────────────────────────────────────
    remap_rows = []
    for (table, col), sites_with_col in sorted(col_sites.items()):
        if sites_with_col == all_site_set:
            site_label = 'all'
        elif len(sites_with_col) == 1:
            site_label = next(iter(sites_with_col))
        else:
            site_label = ';'.join(sorted(sites_with_col))

        remap_rows.append({
            'table':       table,
            'column':      col,
            'prefix':      col_meta[(table, col)]['prefix'],
            'mapping_key': col_meta[(table, col)]['mapping_key'],
            'sites':       site_label,
        })

    # ── Print ────────────────────────────────────────────────────────────────
    print()
    print(f"{'TABLE':<28} {'COLUMN':<35} {'PREFIX':<8} {'SITES':<12} MAPPING KEY")
    print('─' * 100)
    cur_table = None
    for r in remap_rows:
        if r['table'] != cur_table:
            if cur_table is not None:
                print()
            cur_table = r['table']
        alias_note = f"  → key: {r['mapping_key']}" if r['mapping_key'] != r['column'] else ''
        print(f"  {r['table']:<26} {r['column']:<35} {r['prefix']:<8} {r['sites']:<12}{alias_note}")

    n_tables = len({r['table'] for r in remap_rows})
    print()
    print(f"Total: {len(remap_rows)} columns across {n_tables} table(s), {n_sites} site(s)")

    # ── CSV output ───────────────────────────────────────────────────────────
    if args.output:
        with open(args.output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['table', 'column', 'prefix', 'mapping_key', 'sites'])
            writer.writeheader()
            writer.writerows(remap_rows)
        print(f"CSV written to {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
