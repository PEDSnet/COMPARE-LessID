# LessID_v2

A Python + SAS pipeline for de-identifying CDM (Common Data Model) datasets for the COMPARE study. Raw patient, encounter, provider, and facility IDs are replaced with deterministic, site-scoped surrogate IDs. XLSX reports receive the same replacements so the two outputs stay cross-consistent.

---

## Overview

```
CPT (SAS transport)  ──┐
                        ├─► collect IDs ──► build mapping ──► apply mapping ──► de-identified CPT
XLSX reports       ──┘                                    └──► de-identified XLSX
```

Each site gets its own `mapping.csv` so surrogate IDs are stable across re-runs and never collide across sites.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **SAS 9.4** | Must be installed and licensed on the host machine. The pipeline calls the `sas` binary via subprocess. |
| **Podman 4+** | Primary runtime. Python is only required for local development (see below). |

---

## Podman

SAS 9.4 cannot be included in the image (licensing). The image is Python-only; SAS is accessed via a bind-mount of the host SAS installation.

### Build

```bash
# With sudo (simplest — no subuid/subgid setup required):
sudo podman build -t lessid .

# Rootless also works if subuid/subgid are configured:
#   sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER
#   podman system migrate
podman build -t lessid .
```

### Configure

Copy the example config and fill in your paths:

```bash
cp config/lessid.example.toml config/lessid.toml
# then edit config/lessid.toml
```

`config/lessid.toml` is **gitignored** — it contains absolute paths and must never be committed.

Set `sas_bin` to the path **inside the container** where SAS will be bind-mounted:

```toml
[paths]
cpt_base    = "/data/sas_queries/<owner>/<study>"
out_base    = "/data/sas_queries/<you>/lessid_drnoc"
lookup_base = "/data/sas_queries/<you>/lessid_lookup"
work_base   = "/data/sas_queries/<you>/lessid_work"
lessid_dir  = "/home/<you>/lessid"
sas_bin     = "/host_sas/SASFoundation/9.4/bin/sas_u8"

[processing]
date_shift_days = 0       # set >0 to enable per-patient date perturbation (future projects)
sites           = []      # leave empty to auto-discover all sites
# parallel omitted → auto: max(2, cpu_count-8); on pedsdb08 (32 CPUs) = 24
```

### Run

```bash
sudo podman run --rm \
  -v /path/to/config/lessid.toml:/app/config/lessid.toml:ro \
  -v "$CPT_BASE":/data/cpt:ro \
  -v "$OUT_BASE":/data/output \
  -v "$LOOKUP_BASE":/data/lookup \
  -v "$WORK_BASE":/data/work \
  -v "${SAS_HOME:-/usr/local/SAS}":/host_sas:ro \
  lessid run --yes
```

### Subcommands

| Command | Description |
|---|---|
| `lessid plan [SITE]` | Preview which columns will be remapped — no execution |
| `lessid run [--site S] [--yes] [--parallel N] [--cpt-only] [--xlsx-only] [--force]` | Full pipeline: CPT → mapping → XLSX → verify |
| `lessid verify [--site S]` | Re-run verification checks only |
| `lessid spotcheck SITE` | Interactive REPL: look up mappings, sample pairs, check XLSX output |
| `lessid audit [SITE...] [-o FILE]` | List all ID columns that will be remapped; optionally export to CSV |

The pipeline prints the resolved worker count and a summary table at each phase:

```
lessid pipeline
==================================================
  Sites:    12
  Force:    False
  Parallel: 24 worker(s)  (cpu_count=32)
  ...
```

---

## Local development

For development without Podman, install dependencies directly:

```bash
git clone <repo-url> lessid
cd lessid

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Then run pipeline commands via Python:

```bash
# plan
python src/pipeline.py plan
python src/pipeline.py plan C7LC_compare_deq_q01

# run
python src/pipeline.py run --yes
python src/pipeline.py run --site C7LC_compare_deq_q01 --yes --parallel 4
python src/pipeline.py run --xlsx-only --yes

# verify
python src/pipeline.py verify

# spotcheck
python src/pipeline.py spotcheck C7LC_compare_deq_q01

# audit
python src/pipeline.py audit
python src/pipeline.py audit C7LC_compare_deq_q01 -o remap_cols.csv
```

If `config/lessid.toml` does not exist the pipeline falls back to a `.env` file (or exported environment variables) for backwards compatibility with the original shell-script workflow.

---

## Configuration reference

### `[columns]`

| Setting | Effect |
|---|---|
| `remap_never` | Always skip these columns even though they end in `id` |
| `remap_exclude` | Per-run additional exclusions |
| `remap_extra` | Columns that don't end in `id` but should still be remapped |
| `[columns.aliases]` | Multiple column names that share the same mapping key (e.g. all `*_providerid` variants) |

### `[processing]`

| Setting | Default | Notes |
|---|---|---|
| `parallel` | `max(2, cpu_count-8)` | Omit to use the auto-default; override with an integer or `"max"` |
| `date_shift_days` | `0` | Set to a positive integer to enable per-patient date perturbation |
| `sites` | `[]` | Leave empty to auto-discover all sites under `cpt_base` |
| `force` | `false` | Reprocess sites that already have a `_cpt_completed` marker |

---

## ID prefix assignment

| Prefix | Columns |
|---|---|
| `PAT` | `patid`, `person_id`, `org_patid` |
| `ENC` | `encounterid`, `visit_id` |
| `PRV` | `providerid`, `*_providerid` |
| `FAC` | `facilityid`, `lab_facilityid`, `trial_siteid`, `site` |
| `ID` | everything else |

Surrogate ID format: `{PREFIX}_{SITECODE}_{8DIGITS}` — e.g. `PAT_C7LC_00000118`.

---

## Output structure

```
lessid_drnoc/
└── C7LC_compare_deq_q01/
    ├── 20240601_compare_deq_q01.cpt   ← de-identified CPT (SAS transport)
    ├── 20240601_compare_deq_q01.xlsx  ← de-identified XLSX
    ├── _cpt_completed                 ← marker: CPT phase done
    └── _xlsx_completed                ← marker: XLSX phase done

lessid_lookup/                         ← KEEP RESTRICTED (contains raw IDs)
└── C7LC_compare_deq_q01/
    ├── mapping.csv                    ← (column, original_value, new_id)
    └── mapping_report.txt
```

---

## Legacy shell scripts

The original shell scripts are retained for reference:

| Script | Replaced by |
|---|---|
| `run_all_sites.sh` | `pipeline run --cpt-only` |
| `run_all_xlsx.sh` | `pipeline run --xlsx-only` |
| `run_pipeline.sh` | `pipeline run` |

They still work and continue to read from `.env`. Use `src/pipeline.py` (or the Podman image) for new runs.
