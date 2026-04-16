# LessID_v2

A Python + SAS pipeline for de-identifying CDM (Common Data Model) datasets for the COMPARE study. Raw patient, encounter, provider, and facility IDs are replaced with deterministic, site-scoped surrogate IDs. XLSX reports receive the same replacements so the two outputs stay cross-consistent.

---

## Overview

```
CPT (SAS transport)   ──┐
                        ├─► collect IDs ──► build mapping ──► apply mapping ──► de-identified CPT
XLSX reports          ──┘                                  └──► de-identified XLSX
```

Each site gets its own `mapping.csv` so surrogate IDs are stable across re-runs and never collide across sites.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **SAS 9.4** | Must be installed and licensed on the host machine. The pipeline calls the `sas` binary via subprocess. |
| **Podman 4+** | Primary runtime. **No sudo required.** Python is only required for local development (see below). |

### One-time Podman storage setup (per user, pedsdb08)

By default, Podman stores images in `/var/lib/containers/storage` which is small and will fill up. Each user must redirect storage to `/data` **once** before building:

```bash
# 1. Create your personal image store (no sudo needed — /data/containers is world-writable with sticky bit)
mkdir -p /data/containers/$USER

# 2. Point Podman at it
mkdir -p ~/.config/containers
cat > ~/.config/containers/storage.conf << 'EOF'
[storage]
driver = "overlay"
graphroot = "/data/containers/$USER"
EOF
# Substitute your actual username — $USER won't expand inside single-quoted heredoc:
sed -i "s|\$USER|$USER|g" ~/.config/containers/storage.conf

# 3. Verify
cat ~/.config/containers/storage.conf
# Should show: graphroot = "/data/containers/yourname"
```

You only need to do this once. After that, `podman build` and `podman run` work without sudo.

---

## Podman

SAS 9.4 cannot be included in the image (licensing). The image is Python-only; SAS is accessed via a bind-mount of the host SAS installation.

### Build

Each user builds their own local copy of the image (~145 MB, ~1 min):

```bash
podman build -t lessid .
```

> **Note:** Images are stored per-user in rootless Podman — other users on the same server cannot see or share your image. Each person runs `podman build` once.

### Configure

Copy the example config. Paths here are **container-internal** — the host-to-container mapping is in `run_lessid.sh` (see Run below):

```bash
cp config/lessid.example.toml config/lessid.toml
```

`config/lessid.toml` is **gitignored** — it contains absolute paths and must never be committed.

```toml
[paths]
cpt_base    = "/data/source"    # bind-mounted from HOST_SOURCE in run_lessid.sh
out_base    = "/data/output"
lookup_base = "/data/lookup"
work_base   = "/data/work"
lessid_dir  = "/app"            # repo root inside the container
sas_bin     = "/host_sas"       # bind-mounted SAS binary

[processing]
date_shift_days = 0       # set >0 to enable per-patient date perturbation (future projects)
sites           = []      # leave empty to auto-discover all sites
# parallel omitted → auto: max(2, cpu_count-8); on pedsdb08 (32 CPUs) = 24
```

### Run

Copy the wrapper script and fill in your host paths (this file is gitignored):

```bash
cp run_lessid.example.sh run_lessid.sh
# edit HOST_* variables in run_lessid.sh
```

The wrapper auto-detects your SAS binary via `which sas` and bind-mounts everything. Then:

```bash
./run_lessid.sh plan          # dry run — no execution
./run_lessid.sh run --yes     # full pipeline
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
