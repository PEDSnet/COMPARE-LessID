#!/usr/bin/env python3
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
  --force / -f            Reprocess even if _cpt_completed / _xlsx_completed marker exists
  --yes / -y              Skip confirmation prompt before running
  --parallel N            Process N sites concurrently (default: 1, sequential)
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
        'parallel':        int(raw.get('processing', {}).get('parallel', 1)),
        'sites':           raw.get('processing', {}).get('sites', []),
        'date_shift_days': int(raw.get('processing', {}).get('date_shift_days', 0)),
    }
    cfg = {'paths': paths, 'processing': processing, 'columns': raw.get('columns', {})}
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


# ── Timing helpers ───────────────────────────────────────────────────────────

def _fmt_elapsed(secs: float) -> str:
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f'{h}h{m:02d}m{s:02d}s'
    return f'{m}m{s:02d}s'


# ── SAS runner ───────────────────────────────────────────────────────────────

def run_sas(sas_bin: str, initstmt: str, sas_script: str | None = None,
            log_path: str | None = None, lst_path: str | None = None,
            stdin_code: str | None = None) -> None:
    """Run SAS. Raises subprocess.CalledProcessError on non-zero exit."""
    cmd = [sas_bin, '-nodms']
    if sas_script:
        cmd = [sas_bin]
        if log_path:
            cmd += ['-log', log_path]
        if lst_path:
            cmd += ['-print', lst_path]
        cmd += ['-initstmt', initstmt, sas_script]
        subprocess.run(cmd, check=True)
    else:
        # Inline SAS via stdin
        cmd = [sas_bin, '-nodms', '-stdio']
        subprocess.run(cmd, input=stdin_code.encode(), check=True)


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

def run_site_cpt(site_name: str, cfg: dict, force: bool = False) -> dict:
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
    site_lookup  = Path(lookup_base) / site_name
    mapping_csv  = site_lookup / 'mapping.csv'
    mapping_report = site_lookup / 'mapping_report.txt'

    if (site_out / '_cpt_completed').exists() and mapping_csv.exists() and not force:
        return {'site': site_name, 'status': 'SKIPPED', 'tables': '-', 'mappings': '-',
                'elapsed': 0.0, 'error': None}

    cpt_files = sorted(site_drnoc.glob('*.cpt'))
    if not cpt_files:
        return {'site': site_name, 'status': 'NO CPT', 'tables': '-', 'mappings': '-',
                'elapsed': time.time() - t0, 'error': 'No CPT found'}

    cpt_file  = cpt_files[0]
    cpt_stem  = cpt_file.stem

    sas7bdat_dir = Path(work_base) / site_name / 'sas7bdat'
    mapped_dir   = Path(work_base) / site_name / 'mapped'
    cpt_ids_csv  = Path(work_base) / site_name / 'cpt_id_values.csv'

    for d in (sas7bdat_dir, mapped_dir, site_out, site_lookup):
        d.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: proc cimport
        run_sas(sas_bin, '', stdin_code=f'''
libname outlib "{sas7bdat_dir}";
proc cimport infile="{cpt_file}" library=outlib;
run;
''')
        table_count = len(list(sas7bdat_dir.glob('*.sas7bdat')))

        # Step 2: collect IDs
        run_sas(
            sas_bin,
            f'%let input_lib_path = {sas7bdat_dir}; %let output_csv = {cpt_ids_csv};',
            sas_script=str(Path(lessid_dir) / 'sas' / 'collect_site_ids.sas'),
            log_path=str(mapped_dir / 'collect_ids.log'),
            lst_path=str(mapped_dir / 'collect_ids.lst'),
        )

        # Step 3: build mapping
        # Discover XLSX files in site_drnoc
        py_dir = _find_py_dir(lessid_dir)
        subprocess.run([
            sys.executable,
            str(py_dir / 'build_site_mapping.py'),
            site_name,
            str(cpt_ids_csv),
            str(site_drnoc),
            str(mapping_csv),
            str(mapping_report),
        ], check=True)

        # Step 4: apply mapping (lessid.sas) — one file at a time like run.sh
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

        # Step 5: proc cport
        out_cpt = site_out / f'{cpt_stem}.cpt'
        run_sas(sas_bin, '', stdin_code=f'''
libname inlib "{mapped_dir}";
filename tranfile "{out_cpt}";
proc cport library=inlib file=tranfile memtype=data;
run;
''')

        mapping_count = _count_csv_rows(mapping_csv)
        (site_out / '_cpt_completed').touch()

        # Clean up intermediates
        import shutil
        shutil.rmtree(Path(work_base) / site_name, ignore_errors=True)

        return {
            'site': site_name, 'status': 'OK',
            'tables': table_count, 'mappings': mapping_count,
            'elapsed': time.time() - t0, 'error': None,
        }

    except Exception as exc:
        return {
            'site': site_name, 'status': 'FAILED',
            'tables': '-', 'mappings': '-',
            'elapsed': time.time() - t0, 'error': str(exc),
        }


def run_site_xlsx(site_name: str, cfg: dict, force: bool = False) -> dict:
    """Run the XLSX de-identification pass for one site."""
    t0 = time.time()
    paths = cfg['paths']
    cpt_base    = paths['cpt_base']
    out_base    = paths['out_base']
    lookup_base = paths['lookup_base']
    lessid_dir  = paths['lessid_dir']

    site_drnoc  = Path(cpt_base) / site_name / 'drnoc'
    site_out    = Path(out_base) / site_name
    mapping_csv = Path(lookup_base) / site_name / 'mapping.csv'

    if (site_out / '_xlsx_completed').exists() and not force:
        return {'site': site_name, 'status': 'SKIPPED', 'files': '-', 'mappings': '-',
                'elapsed': 0.0, 'error': None}

    if not mapping_csv.exists():
        return {'site': site_name, 'status': 'NO MAPPING', 'files': '-', 'mappings': '-',
                'elapsed': time.time() - t0, 'error': 'No mapping.csv'}

    xlsx_files = sorted(site_drnoc.glob('*.xlsx'))
    if not xlsx_files:
        return {'site': site_name, 'status': 'NO XLSX', 'files': 0, 'mappings': '-',
                'elapsed': time.time() - t0, 'error': None}

    site_out.mkdir(parents=True, exist_ok=True)
    py_dir = _find_py_dir(lessid_dir)

    try:
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
            ], check=True)

        mapping_count = _count_csv_rows(mapping_csv)
        (site_out / '_xlsx_completed').touch()

        return {
            'site': site_name, 'status': 'OK',
            'files': len(xlsx_files), 'mappings': mapping_count,
            'elapsed': time.time() - t0, 'error': None,
        }
    except Exception as exc:
        return {
            'site': site_name, 'status': 'FAILED',
            'files': '-', 'mappings': '-',
            'elapsed': time.time() - t0, 'error': str(exc),
        }


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
    """Return the scripts directory (src/ preferred over py/)."""
    src = Path(lessid_dir) / 'src'
    if src.exists():
        return src
    return Path(lessid_dir) / 'py'


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
              help='Reprocess even if _cpt_completed marker exists')
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
    n_workers = n_parallel if n_parallel is not None else cfg['processing']['parallel']

    click.echo(f'\nlessid pipeline')
    click.echo('=' * 50)
    click.echo(f'  Sites:    {len(sites)}')
    click.echo(f'  Force:    {effective_force}')
    click.echo(f'  Parallel: {n_workers}')
    click.echo(f'  CPT:      {not xlsx_only}')
    click.echo(f'  XLSX:     {not cpt_only}')
    click.echo(f'  Sites:\n' + '\n'.join(f'    • {s}' for s in sites))

    if not yes:
        click.confirm('\nProceed?', abort=True)

    t_total = time.time()

    # ── CPT phase ──
    if not xlsx_only:
        click.echo('\n─── Phase 1: CPT ───────────────────────────────')
        cpt_results = _parallel_run(run_site_cpt, sites, cfg, effective_force, n_workers)
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
        xlsx_results = _parallel_run(run_site_xlsx, xlsx_sites, cfg, effective_force, n_workers)
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
        click.echo(f'  python src/pipeline.py spotcheck {s}')
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


if __name__ == '__main__':
    cli(obj={})
