#!/usr/bin/env python3
from __future__ import annotations
"""
lessid pipeline CLI — replaces run_all_sites.sh / run_all_xlsx.sh.

Commands
--------
  plan    [SITE]          Print what columns would be remapped (no execution)
  run     [--site S]...   Full pipeline: CPT → mapping → XLSX → verify
  verify  [--site S]...   Verification pass only
  spotcheck SITE          Launch interactive spot-check REPL

Common flags
------------
  --force / -f            Reprocess even if .cpt_completed / .xlsx_completed marker exists
  --yes / -y              Skip confirmation prompt before running
  --parallel N            Process N sites concurrently (default: max(2, cpu_count-8))
  --config PATH           Path to lessid.toml  (default: config/lessid.toml in repo root)
"""

import csv
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import click

# Repo root — pipeline.py lives in src/
_REPO_ROOT = Path(__file__).parent.parent

# Default set of PCORNet CDM tables inspected by `pipeline audit`.
# Override via [columns] known_tables in lessid.toml.
KNOWN_TABLES = (
    'DEMOGRAPHIC', 'ENROLLMENT', 'ENCOUNTER', 'DIAGNOSIS', 'PROCEDURES', 'VITAL',
    'DISPENSING', 'LAB_RESULT_CM', 'CONDITION', 'PRO_CM', 'PRESCRIBING',
    'PCORNET_TRIAL', 'DEATH', 'DEATH_CAUSE', 'MED_ADMIN', 'PROVIDER',
    'OBS_CLIN', 'OBS_GEN', 'HASH_TOKEN', 'LDS_ADDRESS_HISTORY', 'IMMUNIZATION',
    'HARVEST', 'LAB_HISTORY', 'PAT_RELATIONSHIP', 'EXTERNAL_MEDS',
)


# ── Config loading ───────────────────────────────────────────────────────────

def _load_toml(cfg_path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
        with open(cfg_path, 'rb') as f:
            return tomllib.load(f)
    else:
        try:
            import tomli  # type: ignore[import]
            with open(cfg_path, 'rb') as f:
                return tomli.load(f)
        except ImportError:
            click.echo("ERROR: Python <3.11 requires the 'tomli' package. Run: pip install tomli", err=True)
            sys.exit(1)


def _load_env(env_path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file."""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            result[k] = v
    return result


def _default_parallel() -> int:
    n = os.cpu_count()
    if n <= 2:
        return 1
    if n <= 4:
        return max(2, n - 1)
    if n <= 8:
        return max(2, n - 2)
    return max(2, n - 8)


def _resolve_parallel(value) -> int:
    """Resolve the parallel setting — accepts int or the string 'max'."""
    if str(value).strip().lower() == 'max' or value is None:
        return _default_parallel()
    return int(value)


def load_config(cfg_path: str | None = None) -> dict:
    """
    Return a unified config dict with keys:
      paths: {cpt_base, out_base, lookup_base, work_base, lessid_dir, sas_bin}
      processing: {force, parallel, sites, date_shift_days}
      columns: {remap_never, remap_extra, remap_exclude, aliases}

    Priority: environment variables > config/lessid.toml > .env defaults.
    """
    default_cfg_path = _REPO_ROOT / 'config' / 'lessid.toml'
    toml_path = Path(cfg_path) if cfg_path else default_cfg_path

    raw: dict = {}
    if toml_path.exists():
        raw = _load_toml(toml_path)
    else:
        # Fall back to .env for backwards compatibility
        env_vars = _load_env(_REPO_ROOT / '.env')
        for k, v in env_vars.items():
            os.environ.setdefault(k, v)

    # Helper: env var overrides toml
    def p(section: str, key: str, env_key: str, default=None):
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val
        return raw.get(section, {}).get(key, default)

    paths = {
        'cpt_base':    p('paths', 'cpt_base',    'CPT_BASE',    ''),
        'out_base':    p('paths', 'out_base',     'OUT_BASE',    ''),
        'lookup_base': p('paths', 'lookup_base',  'LOOKUP_BASE', ''),
        'work_base':   p('paths', 'work_base',    'WORK_BASE',   ''),
        'lessid_dir':  p('paths', 'lessid_dir',   'LESSID_DIR',  str(_REPO_ROOT)),
        'sas_bin':     p('paths', 'sas_bin',      'SAS_BIN',     'sas'),
    }
    processing = {
        'force':           raw.get('processing', {}).get('force', False),
        'parallel':        _resolve_parallel(raw.get('processing', {}).get('parallel')),
        'sites':           raw.get('processing', {}).get('sites', []),
        'date_shift_days': int(raw.get('processing', {}).get('date_shift_days', 0)),
        'query_version':   int(raw.get('processing', {}).get('query_version', 1)),
    }
    raw_cols = dict(raw.get('columns', {}))
    if 'known_tables' not in raw_cols:
        raw_cols['known_tables'] = list(KNOWN_TABLES)
    cfg = {'paths': paths, 'processing': processing, 'columns': raw_cols}
    return cfg


def _validate_paths(paths: dict) -> None:
    missing = [k for k in ('cpt_base', 'out_base', 'lookup_base', 'work_base') if not paths[k]]
    if missing:
        click.echo(
            f"ERROR: Missing required path(s): {', '.join(missing)}.\n"
            "  → Copy config/lessid.example.toml to config/lessid.toml and fill in the paths,\n"
            "    or set CPT_BASE / OUT_BASE / LOOKUP_BASE / WORK_BASE environment variables.",
            err=True,
        )
        sys.exit(1)


# ── Site discovery ───────────────────────────────────────────────────────────
def discover_sites(cpt_base: str) -> list[str]:
    base = Path(cpt_base)
    sites = []
    for drnoc in sorted(base.glob('*/drnoc')):
        sites.append(drnoc.parent.name)
    return sites


def resolve_sites(cfg: dict, site_args: tuple[str, ...]) -> list[str]:
    paths = cfg['paths']
    cpt_base = paths['cpt_base']
    if site_args:
        return list(site_args)
    if cfg['processing']['sites']:
        return list(cfg['processing']['sites'])
    return discover_sites(cpt_base)


def _parse_site_query(site_name: str) -> tuple[str, str]:
    """Split 'C7LC_compare_deq_q01' → ('C7LC', 'q01').  Raises ValueError on bad format."""
    import re
    m = re.match(r'^(?P<site>.+?)_compare.*?_(q\d+)$', site_name, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"Cannot parse site/query from folder name: {site_name!r}\n"
            "  Expected pattern: SITE_compare[...]_qNN  (e.g. C7LC_compare_deq_q01)"
        )
    return m.group('site').upper(), m.group(2)


# ── Timing helpers ───────────────────────────────────────────────────────────

def _fmt_elapsed(secs: float) -> str:
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f'{h}h{m:02d}m{s:02d}s'
    return f'{m}m{s:02d}s'


def _ts(t0: float) -> str:
    """Elapsed since t0 as e.g. t+1m12s."""
    return f"t+{_fmt_elapsed(time.time() - t0)}"


def _log_step(msg: str, q=None, site: str = '') -> None:
    """Emit a step message: push to live queue if available, else print directly."""
    if q is not None:
        try:
            q.put_nowait({'event': 'step', 'site': site, 'msg': msg})
        except Exception:
            pass
    else:
        print(msg, flush=True)


# ── SAS runner ───────────────────────────────────────────────────────────────

def run_sas(sas_bin: str, initstmt: str, sas_script: str | None = None,
            log_path: str | None = None, lst_path: str | None = None,
            stdin_code: str | None = None) -> None:
    """Run SAS. Raises subprocess.CalledProcessError on non-zero exit."""
    if sas_script:
        cmd = [sas_bin]
        if log_path:
            cmd += ['-log', log_path]
        if lst_path:
            cmd += ['-print', lst_path]
        cmd += ['-initstmt', initstmt, sas_script]
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        # Inline SAS via stdin
        cmd = [sas_bin, '-nodms', '-stdio']
        subprocess.run(cmd, input=stdin_code.encode(), check=True, capture_output=True)


# ── Plan display ─────────────────────────────────────────────────────────────

def _show_plan(site_name: str, cfg: dict, cpt_path: str | None = None) -> None:
    """Print the remap plan for a site (no execution)."""
    import importlib.util, importlib

    # Import rules from src/ or py/ depending on where we're running from
    rules_path = Path(__file__).parent / 'rules.py'
    if not rules_path.exists():
        rules_path = _REPO_ROOT / 'py' / 'rules.py'

    spec = importlib.util.spec_from_file_location('rules', rules_path)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)

    click.echo(f"\n  Site:          {site_name}")
    click.echo(f"  REMAP_NEVER:   {sorted(rules.REMAP_NEVER)}")
    click.echo(f"  REMAP_EXCLUDE: {sorted(rules.REMAP_EXCLUDE) or '(none)'}")
    click.echo(f"  REMAP_EXTRA:   {sorted(rules.REMAP_EXTRA) or '(none)'}")
    if rules.ALIAS_TO_CANONICAL:
        click.echo(f"  Aliases:       {rules.ALIAS_TO_CANONICAL}")
    else:
        click.echo("  Aliases:       (none — providerid variants not configured)")

    if cpt_path:
        click.echo(f"  CPT file:      {cpt_path}")


# ── Per-site CPT pipeline ────────────────────────────────────────────────────

def run_site_cpt(site_name: str, cfg: dict, force: bool = False, q=None) -> dict:
    """
    Run the full CPT pipeline for one site:
      1. proc cimport
      2. collect IDs
      3. build mapping
      4. apply mapping (lessid.sas)
      5. proc cport

    Returns a result dict with keys: site, status, tables, mappings, elapsed, error.
    """
    t0 = time.time()
    paths = cfg['paths']
    cpt_base    = paths['cpt_base']
    out_base    = paths['out_base']
    lookup_base = paths['lookup_base']
    work_base   = paths['work_base']
    lessid_dir  = paths['lessid_dir']
    sas_bin     = paths['sas_bin']

    site_drnoc   = Path(cpt_base) / site_name / 'drnoc'
    site_out     = Path(out_base) / site_name
    site_code, _ = _parse_site_query(site_name)
    site_lookup  = Path(lookup_base) / site_code
    mapping_csv  = site_lookup / f'{site_code}_mapping.csv'
    mapping_report = site_lookup / f'{site_code}_mapping_report.txt'

    if (site_out / '.cpt_completed').exists() and mapping_csv.exists() and not force:
        result = {'site': site_name, 'status': 'SKIPPED', 'tables': '-', 'mappings': '-',
                  'elapsed': 0.0, 'error': None}
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    cpt_files = sorted(site_drnoc.glob('*.cpt'))
    if not cpt_files:
        result = {'site': site_name, 'status': 'NO CPT', 'tables': '-', 'mappings': '-',
                  'elapsed': time.time() - t0, 'error': 'No CPT found'}
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    cpt_file  = cpt_files[0]
    cpt_stem  = cpt_file.stem

    sas7bdat_dir = Path(work_base) / site_name / 'sas7bdat'
    mapped_dir   = Path(work_base) / site_name / 'mapped'
    cpt_ids_csv  = Path(work_base) / site_name / 'cpt_id_values.csv'

    for d in (sas7bdat_dir, mapped_dir, site_out, site_lookup):
        d.mkdir(parents=True, exist_ok=True)

    if q is not None:
        q.put_nowait({'event': 'start', 'site': site_name, 't0': t0})

    pre_redacted_str = ':'.join(cfg['columns'].get(
        'pre_redacted_values', ['xxx', 'redacted', 'n/a', 'na', 'unknown', 'unk', '-', '--', 'masked']
    ))

    try:
        # Step 1: proc cimport
        _step_t = time.time()
        _log_step(f"  [{site_name}] step 1/5: cimport {cpt_file.name}  ({_ts(t0)})", q, site_name)
        run_sas(sas_bin, '', stdin_code=f'''
libname outlib "{sas7bdat_dir}";
proc cimport infile="{cpt_file}" library=outlib;
run;
''')
        table_count = len(list(sas7bdat_dir.glob('*.sas7bdat')))
        _log_step(f"  [{site_name}] step 1/5: cimport done \u2014 {table_count} tables  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        # Step 2: collect IDs
        site_meta_csv = site_lookup / 'site_meta.csv'
        _step_t = time.time()
        _log_step(f"  [{site_name}] step 2/5: collect IDs  ({_ts(t0)})", q, site_name)
        run_sas(
            sas_bin,
            (f'%let input_lib_path = {sas7bdat_dir}; '
             f'%let output_csv = {cpt_ids_csv}; '
             f'%let output_meta_csv = {site_meta_csv};'),
            sas_script=str(Path(lessid_dir) / 'sas' / 'collect_site_ids.sas'),
            log_path=str(mapped_dir / 'collect_ids.log'),
            lst_path=str(mapped_dir / 'collect_ids.lst'),
        )
        _log_step(f"  [{site_name}] step 2/5: collect IDs done  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        # Step 3: build mapping
        py_dir = _find_py_dir(lessid_dir)
        _step_t = time.time()
        _log_step(f"  [{site_name}] step 3/5: build mapping  ({_ts(t0)})", q, site_name)
        subprocess.run([
            sys.executable,
            str(py_dir / 'build_site_mapping.py'),
            site_name,
            str(cpt_ids_csv),
            str(site_drnoc),
            str(mapping_csv),
            str(mapping_report),
            str(site_meta_csv),
            str(cfg['processing']['query_version']),
            pre_redacted_str,
        ], check=True, capture_output=True)
        _log_step(f"  [{site_name}] step 3/5: build mapping done  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        # Step 4: apply mapping (lessid.sas) — one file at a time
        tables_ordered = [p.stem for p in sorted(sas7bdat_dir.glob('*.sas7bdat'))]
        _log_step(
            f"  [{site_name}] step 4/5: apply mapping \u2014 {len(tables_ordered)} table(s): "
            f"{', '.join(tables_ordered)}  ({_ts(t0)})",
            q, site_name,
        )
        _step_t = time.time()
        for sas7bdat in sorted(sas7bdat_dir.glob('*.sas7bdat')):
            stem = sas7bdat.stem
            out_path = mapped_dir / sas7bdat.name
            log_path = mapped_dir / f'log_{stem}.log'
            lst_path  = mapped_dir / f'lst_{stem}.lst'
            initstmt = (
                f'%let mapping_path = {mapping_csv}; '
                f'%let input_path = {sas7bdat}; '
                f'%let output_path = {out_path}; '
                f'%let output_type = sas7bdat; '
                f'%let date_shift_days = {cfg["processing"]["date_shift_days"]};'
            )
            run_sas(
                sas_bin,
                initstmt,
                sas_script=str(Path(lessid_dir) / 'sas' / 'lessid.sas'),
                log_path=str(log_path),
                lst_path=str(lst_path),
            )
        _log_step(f"  [{site_name}] step 4/5: apply mapping done  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        # Step 5: proc cport
        out_cpt = site_out / f'{cpt_stem}.cpt'
        _step_t = time.time()
        _log_step(f"  [{site_name}] step 5/5: cport \u2192 {out_cpt.name}  ({_ts(t0)})", q, site_name)
        run_sas(sas_bin, '', stdin_code=f'''
libname inlib "{mapped_dir}";
filename tranfile "{out_cpt}";
proc cport library=inlib file=tranfile memtype=data;
run;
''')
        _log_step(f"  [{site_name}] step 5/5: cport done  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        mapping_count = _count_csv_rows(mapping_csv)
        (site_out / '.cpt_completed').write_text(
            'This file is a lessid pipeline checkpoint and is not part of the study deliverable.\n'
            'If you received this file from PEDSnet, you are free to delete it.\n'
        )

        # Clean up intermediates
        import shutil
        shutil.rmtree(Path(work_base) / site_name, ignore_errors=True)

        result = {
            'site': site_name, 'status': 'OK',
            'tables': table_count, 'mappings': mapping_count,
            'elapsed': time.time() - t0, 'error': None,
        }
        _log_step(
            f"  [{site_name}] CPT DONE \u2014 {table_count} tables, {mapping_count} mappings  "
            f"(total {_fmt_elapsed(time.time()-t0)})",
            q, site_name,
        )
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    except Exception as exc:
        result = {
            'site': site_name, 'status': 'FAILED',
            'tables': '-', 'mappings': '-',
            'elapsed': time.time() - t0, 'error': str(exc),
        }
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result


def run_site_xlsx(site_name: str, cfg: dict, force: bool = False, q=None) -> dict:
    """Run the XLSX de-identification pass for one site."""
    t0 = time.time()
    paths = cfg['paths']
    cpt_base    = paths['cpt_base']
    out_base    = paths['out_base']
    lookup_base = paths['lookup_base']
    lessid_dir  = paths['lessid_dir']

    site_drnoc  = Path(cpt_base) / site_name / 'drnoc'
    site_out    = Path(out_base) / site_name
    site_code, _ = _parse_site_query(site_name)
    mapping_csv = Path(lookup_base) / site_code / f'{site_code}_mapping.csv'

    if (site_out / '.xlsx_completed').exists() and not force:
        result = {'site': site_name, 'status': 'SKIPPED', 'files': '-', 'mappings': '-',
                  'elapsed': 0.0, 'error': None}
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    if not mapping_csv.exists():
        result = {'site': site_name, 'status': 'NO MAPPING', 'files': '-', 'mappings': '-',
                  'elapsed': time.time() - t0, 'error': 'No mapping.csv'}
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    xlsx_files = sorted(site_drnoc.glob('*.xlsx'))
    if not xlsx_files:
        result = {'site': site_name, 'status': 'NO XLSX', 'files': 0, 'mappings': '-',
                  'elapsed': time.time() - t0, 'error': None}
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    site_out.mkdir(parents=True, exist_ok=True)
    py_dir = _find_py_dir(lessid_dir)

    if q is not None:
        q.put_nowait({'event': 'start', 'site': site_name, 't0': t0})

    pre_redacted_str = ':'.join(cfg['columns'].get(
        'pre_redacted_values', ['xxx', 'redacted', 'n/a', 'na', 'unknown', 'unk', '-', '--', 'masked']
    ))
    remap_count = sum(1 for f in xlsx_files if 'edc_discrepancies' not in f.name.lower())

    try:
        _step_t = time.time()
        _log_step(
            f"  [{site_name}] XLSX: remapping {remap_count} file(s) "
            f"({len(xlsx_files) - remap_count} verbatim copy)  ({_ts(t0)})",
            q, site_name,
        )
        for xlsx_file in xlsx_files:
            out_xlsx = site_out / xlsx_file.name
            # EDC discrepancy files contain raw EDC data and must not be de-identified
            if 'edc_discrepancies' in xlsx_file.name.lower():
                import shutil
                shutil.copy2(xlsx_file, out_xlsx)
                continue
            subprocess.run([
                sys.executable,
                str(py_dir / 'map_xlsx.py'),
                str(mapping_csv),
                str(xlsx_file),
                str(out_xlsx),
                pre_redacted_str,
            ], check=True, capture_output=True)
        _log_step(f"  [{site_name}] XLSX done  "
                  f"({_fmt_elapsed(time.time()-_step_t)}, {_ts(t0)})", q, site_name)

        # Copy logs and PDFs from source drnoc folder alongside the XLSX outputs
        import shutil as _shutil
        for ext in ('*.log', '*.pdf'):
            for src_file in sorted(site_drnoc.glob(ext)):
                _shutil.copy2(src_file, site_out / src_file.name)

        mapping_count = _count_csv_rows(mapping_csv)
        (site_out / '.xlsx_completed').write_text(
            'This file is a lessid pipeline checkpoint and is not part of the study deliverable.\n'
            'If you received this file from PEDSnet, you are free to delete it.\n'
        )

        result = {
            'site': site_name, 'status': 'OK',
            'files': len(xlsx_files), 'mappings': mapping_count,
            'elapsed': time.time() - t0, 'error': None,
        }
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result

    except Exception as exc:
        result = {
            'site': site_name, 'status': 'FAILED',
            'files': '-', 'mappings': '-',
            'elapsed': time.time() - t0, 'error': str(exc),
        }
        if q is not None:
            q.put_nowait({'event': 'done', 'site': site_name, 'result': result})
        return result


def run_site_verify(site_name: str, cfg: dict) -> dict:
    """Run verify.py for one site. Returns {site, status, error}."""
    t0 = time.time()
    paths = cfg['paths']
    lessid_dir = paths['lessid_dir']
    py_dir = _find_py_dir(lessid_dir)

    # Pass all required env vars so verify.py can find files via .env / env
    env = dict(os.environ)
    for k, v in {
        'CPT_BASE':    paths['cpt_base'],
        'OUT_BASE':    paths['out_base'],
        'LOOKUP_BASE': paths['lookup_base'],
        'WORK_BASE':   paths['work_base'],
        'LESSID_DIR':  paths['lessid_dir'],
    }.items():
        env.setdefault(k, v)

    result = subprocess.run(
        [sys.executable, str(py_dir / 'verify.py'), site_name],
        env=env,
    )
    status = 'OK' if result.returncode == 0 else 'FAILED'
    return {'site': site_name, 'status': status,
            'elapsed': time.time() - t0, 'error': None}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_py_dir(lessid_dir: str) -> Path:
    """Return the src/ scripts directory."""
    return Path(lessid_dir) / 'src'


def _count_csv_rows(path: Path) -> int:
    try:
        with open(path, newline='') as f:
            return max(0, sum(1 for _ in csv.reader(f)) - 1)
    except Exception:
        return 0


def _print_summary_table(headers: list[str], rows: list[list], title: str) -> None:
    widths = [max(len(h), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    bar = '─' * (sum(widths) + len(widths) * 3 + 1)
    click.echo(f'\n{bar}')
    click.echo(f' {title}')
    click.echo(bar)
    header_line = ' │ '.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    click.echo(f' {header_line}')
    click.echo(bar)
    for row in rows:
        line = ' │ '.join(str(row[i]).ljust(widths[i]) for i in range(len(headers)))
        click.echo(f' {line}')
    click.echo(bar)


def _parallel_run(fn, sites, cfg, force, n_workers):
    """Run fn(site, cfg, force) in parallel with ProcessPoolExecutor."""
    results = []
    if n_workers <= 1:
        for s in sites:
            results.append(fn(s, cfg, force))
        return results

    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(fn, s, cfg, force): s for s in sites}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def _run_phase_live(fn, sites: list, cfg: dict, force: bool, n_workers: int,
                    phase_title: str, extra_result_keys: list) -> list:
    """
    Run fn(site, cfg, force, q) in parallel.

    Displays a rich live table on /dev/tty (bypassing tee) when available.
    Falls back to plain per-step print() output if /dev/tty or rich is unavailable.
    After the live context exits, prints a plain-text summary to stdout for the log.
    """
    if n_workers <= 1:
        results = []
        for s in sites:
            results.append(fn(s, cfg, force))
        return results

    # Try to open /dev/tty for the live display (bypasses run_lessid.sh tee)
    live_console = None
    _tty_file = None
    try:
        from rich.console import Console
        _tty_file = open('/dev/tty', 'w')
        live_console = Console(file=_tty_file, force_terminal=True)
    except (OSError, ImportError):
        pass

    import multiprocessing as _mp
    mgr = _mp.Manager()
    q = mgr.Queue()

    site_status: dict = {s: 'queued' for s in sites}
    site_step:   dict = {s: '' for s in sites}
    site_t0:     dict = {}
    site_result: dict = {}

    def _make_rich_table():
        from rich.table import Table
        t = Table(title=phase_title, show_header=True, expand=True)
        t.add_column('Site', no_wrap=True)
        t.add_column('Status', width=12)
        t.add_column('Step', ratio=2)
        for k in extra_result_keys:
            t.add_column(k.title(), justify='right', width=9)
        t.add_column('Time', justify='right', width=9)
        for s in sites:
            st = site_status[s]
            step = site_step[s]
            extras = [str(site_result.get(s, {}).get(k, '-')) for k in extra_result_keys]
            if st == 'running':
                elapsed_str = _fmt_elapsed(time.time() - site_t0[s])
                t.add_row(s, '[yellow]running\u2026[/]', step, *extras, elapsed_str)
            elif st in ('OK', 'SKIPPED'):
                elapsed_str = _fmt_elapsed(site_result[s]['elapsed'])
                t.add_row(s, '[green]\u2713 ' + st + '[/]', '', *extras, elapsed_str)
            elif st == 'queued':
                t.add_row(s, '[dim]queued[/]', '', *extras, '-')
            else:
                elapsed_str = _fmt_elapsed(site_result.get(s, {}).get('elapsed', 0))
                t.add_row(s, '[red]\u2717 ' + st + '[/]', '', *extras, elapsed_str)
        return t

    def _drain_queue():
        while True:
            try:
                ev = q.get_nowait()
            except Exception:
                break
            s = ev.get('site', '')
            evt = ev.get('event', '')
            if evt == 'start':
                site_status[s] = 'running'
                site_t0[s] = ev.get('t0', time.time())
            elif evt == 'step':
                if s:
                    site_step[s] = ev.get('msg', '').strip()
                if live_console is None:
                    print(ev.get('msg', ''), flush=True)
            elif evt == 'done':
                r = ev.get('result', {})
                site_result[s] = r
                site_status[s] = r.get('status', 'FAILED')
                site_step[s] = ''

    results = []
    try:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            futures = {ex.submit(fn, s, cfg, force, q): s for s in sites}
            pending = set(futures)

            if live_console is not None:
                from rich.live import Live
                with Live(_make_rich_table(), console=live_console,
                          refresh_per_second=4) as live:
                    while pending:
                        _drain_queue()
                        live.update(_make_rich_table())
                        done_futs = {f for f in pending if f.done()}
                        for f in done_futs:
                            results.append(f.result())
                        pending -= done_futs
                        if pending:
                            time.sleep(0.15)
                    _drain_queue()
                    live.update(_make_rich_table())
            else:
                for fut in as_completed(futures):
                    _drain_queue()
                    results.append(fut.result())
                _drain_queue()

        # Print plain-text summary to stdout so it ends up in the log file
        if live_console is not None:
            from rich.console import Console as _PlainConsole
            from rich.table import Table as _PlainTable
            plain = _PlainConsole(no_color=True, highlight=False)
            plain.print(_make_rich_table())

    finally:
        mgr.shutdown()
        if _tty_file:
            try:
                _tty_file.close()
            except Exception:
                pass

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

@click.group()
@click.option('--config', 'cfg_path', default=None, metavar='PATH',
              help='Path to lessid.toml (default: config/lessid.toml)')
@click.pass_context
def cli(ctx, cfg_path):
    ctx.ensure_object(dict)
    ctx.obj['cfg'] = load_config(cfg_path)


# ── plan ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('site', required=False)
@click.pass_context
def plan(ctx, site):
    """Print the remap/alias plan for SITE (or all sites) without running anything."""
    cfg = ctx.obj['cfg']
    paths = cfg['paths']
    _validate_paths(paths)

    sites = resolve_sites(cfg, (site,) if site else ())
    click.echo(f'\nRemap plan  ({len(sites)} site(s))')
    click.echo('=' * 50)
    for s in sites:
        cpt_drnoc = Path(paths['cpt_base']) / s / 'drnoc'
        cpt_files = list(cpt_drnoc.glob('*.cpt'))
        cpt_path  = str(cpt_files[0]) if cpt_files else None
        _show_plan(s, cfg, cpt_path=cpt_path)
    click.echo()


# ── run ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--site', 'sites_opt', multiple=True, metavar='SITE',
              help='Site(s) to process (default: all)')
@click.option('--force', '-f', is_flag=True,
              help='Reprocess even if .cpt_completed marker exists')
@click.option('--yes', '-y', is_flag=True,
              help='Skip confirmation prompt')
@click.option('--parallel', '-j', 'n_parallel', default=None, type=int, metavar='N',
              help='Number of sites to run in parallel (overrides config)')
@click.option('--cpt-only', is_flag=True, help='Run CPT phase only (skip XLSX + verify)')
@click.option('--xlsx-only', is_flag=True, help='Run XLSX + verify only (skip CPT)')
@click.pass_context
def run(ctx, sites_opt, force, yes, n_parallel, cpt_only, xlsx_only):
    """Run the full pipeline for one or all sites."""
    cfg = ctx.obj['cfg']
    paths = cfg['paths']
    _validate_paths(paths)

    sites = resolve_sites(cfg, sites_opt)
    if not sites:
        click.echo('No sites found. Check CPT_BASE or config [paths].cpt_base.', err=True)
        sys.exit(1)

    effective_force = force or cfg['processing']['force']
    n_workers = _resolve_parallel(n_parallel) if n_parallel is not None else cfg['processing']['parallel']
    n_workers = min(n_workers, len(sites))

    click.echo(f'\nlessid pipeline')
    click.echo('=' * 50)
    click.echo(f'  Sites:    {len(sites)}')
    click.echo(f'  Force:    {effective_force}')
    click.echo(f'  Parallel: {n_workers} worker(s)  (cpu_count={os.cpu_count()})')
    click.echo(f'  CPT:      {not xlsx_only}')
    click.echo(f'  XLSX:     {not cpt_only}')
    click.echo(f'  Sites:\n' + '\n'.join(f'    • {s}' for s in sites))

    if not yes:
        click.confirm('\nProceed?', abort=True)

    t_total = time.time()

    # ── CPT phase ──
    if not xlsx_only:
        click.echo('\n─── Phase 1: CPT ───────────────────────────────')
        cpt_results = _run_phase_live(
            run_site_cpt, sites, cfg, effective_force, n_workers,
            'Phase 1: CPT', ['tables', 'mappings'],
        )
        _print_summary_table(
            ['Site', 'Tables', 'Mappings', 'Time', 'Status'],
            [[r['site'], r['tables'], r['mappings'], _fmt_elapsed(r['elapsed']), r['status']]
             for r in cpt_results],
            'CPT results',
        )
        failed = [r for r in cpt_results if r['status'] == 'FAILED']
        if failed:
            click.echo(f'\n{len(failed)} site(s) failed CPT phase. Aborting.', err=True)
            for r in failed:
                click.echo(f'  {r["site"]}: {r["error"]}', err=True)
            sys.exit(1)

    # ── XLSX phase ──
    if not cpt_only:
        click.echo('\n─── Phase 2: XLSX ──────────────────────────────')
        # Only process sites that completed CPT (or were already done)
        xlsx_sites = sites if xlsx_only else [
            r['site'] for r in cpt_results if r['status'] in ('OK', 'SKIPPED')
        ]
        xlsx_results = _run_phase_live(
            run_site_xlsx, xlsx_sites, cfg, effective_force, n_workers,
            'Phase 2: XLSX', ['files', 'mappings'],
        )
        _print_summary_table(
            ['Site', 'Files', 'Mappings', 'Time', 'Status'],
            [[r['site'], r['files'], r['mappings'], _fmt_elapsed(r['elapsed']), r['status']]
             for r in xlsx_results],
            'XLSX results',
        )

        # ── Verify phase ──
        click.echo('\n─── Phase 3: Verify ────────────────────────────')
        verify_sites = [r['site'] for r in xlsx_results if r['status'] in ('OK', 'SKIPPED')]
        verify_results = []
        for s in verify_sites:
            click.echo(f'  verifying {s}...')
            verify_results.append(run_site_verify(s, cfg))

        verify_rows = [[r['site'], r['status']] for r in verify_results]
        _print_summary_table(['Site', 'Status'], verify_rows, 'Verify results')

    click.echo(f'\nTotal elapsed: {_fmt_elapsed(time.time() - t_total)}')

    # ── Spot-check hints ──
    click.echo('\nSpot-check commands:')
    for s in sites:
        click.echo(f'  ./run_lessid.sh spotcheck {s}')
    click.echo()


# ── verify ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--site', 'sites_opt', multiple=True, metavar='SITE',
              help='Site(s) to verify (default: all)')
@click.pass_context
def verify(ctx, sites_opt):
    """Run the verification pass for one or all sites."""
    cfg = ctx.obj['cfg']
    paths = cfg['paths']
    _validate_paths(paths)

    sites = resolve_sites(cfg, sites_opt)
    if not sites:
        click.echo('No sites found.', err=True)
        sys.exit(1)

    results = []
    for s in sites:
        click.echo(f'  verifying {s}...')
        results.append(run_site_verify(s, cfg))

    _print_summary_table(
        ['Site', 'Status'],
        [[r['site'], r['status']] for r in results],
        'Verify results',
    )

    failed = [r for r in results if r['status'] == 'FAILED']
    if failed:
        sys.exit(1)


# ── spotcheck ────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('site')
@click.pass_context
def spotcheck(ctx, site):
    """Launch the interactive spot-check REPL for SITE."""
    cfg = ctx.obj['cfg']
    paths = cfg['paths']
    lessid_dir = paths['lessid_dir']
    py_dir = _find_py_dir(lessid_dir)

    spotcheck_script = py_dir / 'spotcheck.py'
    if not spotcheck_script.exists():
        click.echo(f'ERROR: spotcheck.py not found at {spotcheck_script}', err=True)
        sys.exit(1)

    env = dict(os.environ)
    for k, v in {
        'CPT_BASE':    paths['cpt_base'],
        'OUT_BASE':    paths['out_base'],
        'LOOKUP_BASE': paths['lookup_base'],
        'WORK_BASE':   paths['work_base'],
        'LESSID_DIR':  paths['lessid_dir'],
    }.items():
        env.setdefault(k, v)

    os.execve(
        sys.executable,
        [sys.executable, str(spotcheck_script), site],
        env,
    )


# ── audit ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('sites', nargs=-1, metavar='[SITE]...')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Write results to a CSV file in addition to stdout')
@click.pass_context
def audit(ctx, sites, output):
    """List all ID columns that will be remapped, across all (or selected) sites.

    Reads each site's CPT file via SAS to discover column names.
    Useful for auditing which columns are affected before running the pipeline.
    """
    import importlib.util
    import glob
    import shutil
    import tempfile
    import subprocess as _sp
    from collections import defaultdict

    cfg = ctx.obj['cfg']
    paths = cfg['paths']
    _validate_paths(paths)

    # Load rules
    rules_path = Path(__file__).parent / 'rules.py'
    if not rules_path.exists():
        rules_path = _REPO_ROOT / 'py' / 'rules.py'
    spec = importlib.util.spec_from_file_location('rules', rules_path)
    rules_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules_mod)

    cpt_base = paths['cpt_base']
    sas_bin  = paths['sas_bin']
    known_tables = [t.upper() for t in cfg['columns']['known_tables']]

    def _find_cpt(site_name: str) -> str | None:
        pattern = f"{cpt_base}/{site_name}/drnoc/*.cpt"
        files = sorted(glob.glob(pattern))
        return files[-1] if files else None

    def _cols_from_cpt(cpt_file: str) -> list[tuple[str, str]]:
        tmp = tempfile.mkdtemp()
        tables_in = ', '.join(f"'{t}'" for t in known_tables)
        sas_prog = tempfile.NamedTemporaryFile(suffix='.sas', delete=False, mode='w')
        sas_log  = sas_prog.name.replace('.sas', '.log')
        sas_prog.write(f"""\
libname src "{tmp}";
proc cimport infile="{cpt_file}" library=src; run;
proc sql noprint;
    create table _meta as
    select upcase(memname) as memname, name
    from dictionary.columns
    where libname='SRC' and upcase(memname) in ({tables_in})
    order by memname, name;
quit;
data _null_; set _meta; put 'COLMETA table=' memname +(-1) ' col=' name; run;
proc datasets library=src kill nolist; run; quit;
libname src clear;
""")
        sas_prog.flush()
        sas_prog.close()
        _sp.run([sas_bin, '-nodms', '-log', sas_log, sas_prog.name], capture_output=True)
        found_tables: set[str] = set()
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
                        found_tables.add(parts['table'])
                        cols.append((parts['table'], parts['col'].lower()))
        missing_tables = [t for t in known_tables if t not in found_tables]
        if missing_tables:
            click.echo(f"    (not in CPT: {', '.join(missing_tables)})", err=True)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.unlink(sas_prog.name)
            os.unlink(sas_log)
        except OSError:
            pass
        return cols

    site_list = list(sites) or resolve_sites(cfg, ())
    click.echo(f"Scanning {len(site_list)} site(s)...", err=True)

    col_sites: dict[tuple[str, str], set[str]] = defaultdict(set)
    col_meta:  dict[tuple[str, str], dict]     = {}
    scanned: list[str] = []

    for site_name in site_list:
        cpt_file = _find_cpt(site_name)
        if not cpt_file:
            click.echo(f"  SKIP {site_name}: no CPT file found", err=True)
            continue
        click.echo(f"  {site_name}: {cpt_file}", err=True)
        scanned.append(site_name)
        for table, col in _cols_from_cpt(cpt_file):
            if rules_mod.is_remap_col(col):
                key = (table, col)
                col_sites[key].add(site_name)
                if key not in col_meta:
                    col_meta[key] = {
                        'prefix':      rules_mod.prefix_for(col),
                        'mapping_key': rules_mod.mapping_col(col),
                    }

    all_site_set = set(scanned)
    remap_rows = []
    for (table, col), sites_with in sorted(col_sites.items()):
        if sites_with == all_site_set:
            site_label = 'all'
        elif len(sites_with) == 1:
            site_label = next(iter(sites_with))
        else:
            site_label = ';'.join(sorted(sites_with))
        remap_rows.append({
            'table':       table,
            'column':      col,
            'prefix':      col_meta[(table, col)]['prefix'],
            'mapping_key': col_meta[(table, col)]['mapping_key'],
            'sites':       site_label,
        })

    click.echo()
    click.echo(f"{'TABLE':<28} {'COLUMN':<35} {'PREFIX':<8} {'SITES':<12} MAPPING KEY")
    click.echo('─' * 100)
    cur_table = None
    for r in remap_rows:
        if r['table'] != cur_table:
            if cur_table is not None:
                click.echo()
            cur_table = r['table']
        alias_note = f"  → key: {r['mapping_key']}" if r['mapping_key'] != r['column'] else ''
        click.echo(f"  {r['table']:<26} {r['column']:<35} {r['prefix']:<8} {r['sites']:<12}{alias_note}")

    n_tables = len({r['table'] for r in remap_rows})
    click.echo()
    click.echo(f"Total: {len(remap_rows)} columns across {n_tables} table(s), {len(scanned)} site(s)")

    if output:
        with open(output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['table', 'column', 'prefix', 'mapping_key', 'sites'])
            writer.writeheader()
            writer.writerows(remap_rows)
        click.echo(f"CSV written to {output}", err=True)


if __name__ == '__main__':
    cli(obj={})
