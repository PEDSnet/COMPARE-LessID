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
| **Python 3.11+** | Or Python 3.9–3.10 with `tomli` installed (see below). |

---

## Installation

```bash
git clone <repo-url> lessid
cd lessid

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Copy the example config and fill in your paths:

```bash
cp config/lessid.example.toml config/lessid.toml
# then edit config/lessid.toml
```

`config/lessid.toml` is **gitignored** — it contains absolute paths and must never be committed.

### Key config fields

```toml
[paths]
cpt_base    = "/data/sas_queries/<owner>/<study>"   # source CPT folder (one subdir per site)
out_base    = "/data/sas_queries/<you>/lessid_drnoc" # de-identified output
lookup_base = "/data/sas_queries/<you>/lessid_lookup" # RESTRICTED: mapping CSVs
work_base   = "/data/sas_queries/<you>/lessid_work"   # temporary SAS datasets
lessid_dir  = "/home/<you>/lessid"                    # this repo root
sas_bin     = "sas"                                   # or absolute path to SAS binary

[processing]
parallel        = 4       # sites processed concurrently
date_shift_days = 0       # set >0 to enable date shifting
sites           = []      # leave empty to auto-discover all sites

[columns]
remap_never = ["participantid", "datamartid"]  # never remap these
remap_extra = []        # remap columns that don't match id$ pattern
remap_exclude = []      # suppress id$-matching columns from remapping

[columns.aliases]
# providerid = ["medadmin_providerid", "rx_providerid"]  # share mapping key
```

### Environment variable fallback

If `config/lessid.toml` does not exist the pipeline falls back to a `.env` file (or already-exported environment variables). This preserves backwards compatibility with the original shell-script workflow.

---

## Running

All commands are run through `src/pipeline.py`:

```bash
source venv/bin/activate
```

### `plan` — preview what will be remapped

```bash
python src/pipeline.py plan
python src/pipeline.py plan C7LC_compare_deq_q01   # single site
```

Prints the remap/alias/exclude column rules without executing anything.

### `run` — full pipeline

```bash
# Process all sites (prompts for confirmation)
python src/pipeline.py run

# Single site, skip prompt, 4 parallel workers
python src/pipeline.py run --site C7LC_compare_deq_q01 --yes --parallel 4

# Force reprocess even if already done
python src/pipeline.py run --force --yes

# CPT phase only (skip XLSX)
python src/pipeline.py run --cpt-only --yes

# XLSX + verify only (mapping already built)
python src/pipeline.py run --xlsx-only --yes
```

The pipeline prints a summary table at each phase and lists spot-check commands on completion.

### `verify` — verification pass only

```bash
python src/pipeline.py verify
python src/pipeline.py verify --site C7LC_compare_deq_q01
```

Checks:
- `[2]` No raw IDs leak into de-identified CPT
- `[3]` All remap columns are present in the mapping
- `[4]` XLSX outputs contain de-identified IDs (cross-consistency)
- `[5]` XLSX patient IDs can be found in CPT tables

### `spotcheck` — interactive REPL

```bash
python src/pipeline.py spotcheck C7LC_compare_deq_q01
```

Commands inside the REPL:

| Command | Description |
|---|---|
| `orig <id>` | Look up the de-identified ID for a raw value |
| `new <id>` | Reverse-look up a surrogate ID → original value |
| `sample [n]` | Show n random mapping pairs |
| `xlsx [file]` | Check a specific XLSX output |
| `stats` | Print mapping counts by prefix |
| `q` | Quit |

---

## Podman

SAS 9.4 cannot be included in the image (licensing). The image is Python-only; SAS is accessed via a bind-mount of the host SAS installation.

> **Note:** `podman compose` is not available on this server. Use `podman run` directly.

### Build

```bash
podman build -t lessid .
```

### Configure

Set `sas_bin` in `config/lessid.toml` to the path **inside the container** where SAS will be mounted:

```toml
[paths]
sas_bin = "/host_sas/SASFoundation/9.4/bin/sas_u8"
```

### Run

```bash
podman run --rm \
  -v /path/to/config/lessid.toml:/app/config/lessid.toml:ro \
  -v "$CPT_BASE":/data/cpt:ro \
  -v "$OUT_BASE":/data/output \
  -v "$LOOKUP_BASE":/data/lookup \
  -v "$WORK_BASE":/data/work \
  -v "${SAS_HOME:-/usr/local/SAS}":/host_sas:ro \
  lessid run --yes
```

Or pass a custom command:

```bash
podman run --rm [...same mounts...] lessid verify
podman run --rm [...same mounts...] lessid spotcheck C7LC_compare_deq_q01
```

---

## Column selection

The pipeline remaps every column whose name ends in `id` (case-insensitive), with the following exceptions and extensions:

| Setting | Effect |
|---|---|
| `remap_never` | Always skip these columns even though they end in `id` |
| `remap_exclude` | Per-run additional exclusions |
| `remap_extra` | Columns that don't end in `id` but should still be remapped |
| `[columns.aliases]` | Multiple column names that share the same mapping key (e.g. all `*_providerid` variants) |

ID prefix assignment:

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

They still work and continue to read from `.env`. Use `src/pipeline.py` for new runs.

---

## Audit / column inventory

```bash
# List all ID-type columns across all sites
python src/list_remap_cols.py

# Save to CSV
python src/list_remap_cols.py -o remap_cols.csv

# Single site
python src/list_remap_cols.py -o remap_cols.csv C7LC_compare_deq_q01
```
