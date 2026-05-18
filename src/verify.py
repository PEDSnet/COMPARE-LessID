#!/usr/bin/env python3
"""
verify.py  <site_name>

CPT-only verification (no XLSX):
  1. CPT output exists and is non-empty
  2. Mapping CSV exists and loads
  3. All new_id values match PAT/ENC/PRV/FAC/ID_XXXXXXXX format
  4. LessID uniqueness: no two originals in the same column share a new_id
  5. CPT spot-check via SAS: sampled original IDs absent, mapped IDs present
"""

import csv
import glob
import os
import re
import sys

if len(sys.argv) != 2:
    print("Usage: verify.py <site_name>")
    print("  e.g. verify.py C7LC_compare_deq_q02")
    sys.exit(1)

SITE = sys.argv[1]

# Paths — read from environment (set by pipeline.py) or fall back to placeholders
CPT_BASE    = os.environ.get("CPT_BASE",    "")
OUT_BASE    = os.environ.get("OUT_BASE",    "")
LOOKUP_BASE = os.environ.get("LOOKUP_BASE", "")

# Mapping lives at lookup_base/{site_code}/{site_code}_mapping.csv  (cumulative across queries)
_m = re.match(r'^(.+?)_compare.*?_(q\d+)$', SITE, re.IGNORECASE)
if not _m:
    print(f"ERROR: Cannot parse site code from: {SITE!r}")
    sys.exit(1)
_site_code = _m.group(1).upper()

src_drnoc   = os.path.join(CPT_BASE, SITE, "drnoc")
out_dir     = os.path.join(OUT_BASE, SITE)
mapping_csv = os.path.join(LOOKUP_BASE, _site_code, f'{_site_code}_mapping.csv')

MAPPED_PATTERN = re.compile(r'^(PAT|ENC|PRV|FAC|ID)_[A-Z0-9]+_\d{8}$')

# ── Checks ─────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[36mINFO\033[0m"
COPY = "\033[36mCOPY\033[0m"

errors = 0

print(f"\n{'═'*60}")
print(f" Verifying: {SITE}")
print(f"{'═'*60}")

# ── 1. CPT output exists ────────────────────────────────────────────────────
print(f"\n[1] CPT output")
cpt_files = glob.glob(os.path.join(out_dir, "*.cpt"))
if not cpt_files:
    print(f"  [{FAIL}] No .cpt file found in {out_dir}")
    errors += 1
else:
    for f in cpt_files:
        size_mb = os.path.getsize(f) / 1024 / 1024
        print(f"  [{PASS}] {os.path.basename(f)}  ({size_mb:.1f} MB)")

# ── 2. Mapping file ─────────────────────────────────────────────────────────
print(f"\n[2] Mapping file")
if not os.path.exists(mapping_csv):
    print(f"  [{FAIL}] mapping.csv not found: {mapping_csv}")
    errors += 1
    sys.exit(errors)

mapping = {}
with open(mapping_csv, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        col = (row.get("column") or "").strip().lower()
        val = (row.get("original_value") or "").strip()
        nid = (row.get("new_id") or "").strip()
        if col and val and nid:
            mapping[(col, val)] = nid

print(f"  [{INFO}] {len(mapping):,} entries loaded")

# ── 3. new_id format check ──────────────────────────────────────────────────
print(f"\n[3] LessID format check")
bad_new_ids = [(k, v) for k, v in mapping.items() if not MAPPED_PATTERN.match(v)]
if bad_new_ids:
    print(f"  [{FAIL}] {len(bad_new_ids)} mapping entries have non-standard new_id format")
    for k, v in bad_new_ids[:5]:
        print(f"           e.g. col={k[0]} orig={k[1]} new={v}")
    errors += 1
else:
    print(f"  [{PASS}] All new_id values match PAT/ENC/PRV/FAC/ID_XXXXXXXX format")

# ── 4. LessID uniqueness ────────────────────────────────────────────────────
# Within each column, no two distinct originals should share the same new_id.
print(f"\n[4] LessID uniqueness (no collisions)")
# Group by column: col -> {new_id: [orig, ...]}
from collections import defaultdict
col_to_reverse: dict = defaultdict(lambda: defaultdict(list))
for (col, orig), nid in mapping.items():
    col_to_reverse[col][nid].append(orig)

collision_count = 0
for col, rev in col_to_reverse.items():
    collisions = {nid: origs for nid, origs in rev.items() if len(origs) > 1}
    if collisions:
        collision_count += len(collisions)
        print(f"  [{FAIL}] col={col}: {len(collisions)} lessID(s) map to multiple originals")
        for nid, origs in list(collisions.items())[:3]:
            print(f"           {nid} ← {origs[:4]}")

if collision_count:
    errors += collision_count
else:
    total_cols = len(col_to_reverse)
    total_ids  = sum(len(rev) for rev in col_to_reverse.values())
    print(f"  [{PASS}] {total_ids:,} unique lessIDs across {total_cols} column type(s) — no collisions")

# ── 5. CPT spot-check via SAS ───────────────────────────────────────────────
# Pick up to 10 patid sample pairs from mapping.csv.
# Confirm: (a) original IDs are absent from CPT patid column,
#          (b) mapped lessIDs are present in CPT patid column.
print(f"\n[5] CPT ID spot-check (SAS)")

cpt_out = cpt_files[0] if cpt_files else None
if not cpt_out:
    print(f"  [{WARN}] Skipping SAS check — no CPT file")
else:
    import subprocess
    import tempfile as _tf

    # Sample up to 10 (orig, new_id) pairs for patid
    sample_pairs = [(orig, nid) for (col, orig), nid in mapping.items()
                    if col == "patid"][:10]

    if not sample_pairs:
        print(f"  [{WARN}] No patid entries in mapping.csv — skipping CPT cross-check")
    else:
        chk_dir = _tf.mkdtemp(prefix=f"lessid_verify_{SITE}_")

        # Build SAS checks: for each sample, assert orig absent, mapped present
        absent_checks = ""
        present_checks = ""
        for orig, mapped in sample_pairs:
            s_orig   = orig.replace("'", "''")
            s_mapped = mapped.replace("'", "''")
            absent_checks += (
                f"    data _null_; set chk.&first_ds (keep=patid);\n"
                f"        where patid = '{s_orig}';\n"
                f"        if _n_ = 1 then put 'FAIL_RAW_PRESENT orig={s_orig}';\n"
                f"    run;\n"
            )
            present_checks += (
                f"    data _null_; set chk.&first_ds (keep=patid);\n"
                f"        where patid = '{s_mapped}';\n"
                f"        if _n_ = 0 then put 'FAIL_MAPPED_ABSENT mapped={s_mapped} orig={s_orig}';\n"
                f"        else put 'PASS_MAPPED_PRESENT mapped={s_mapped}';\n"
                f"        stop;\n"
                f"    run;\n"
            )

        sas_code = f"""
libname chk "{chk_dir}";
proc cimport infile="{cpt_out}" library=chk; run;

proc sql noprint;
    select memname into :first_ds trimmed
    from dictionary.columns
    where libname='CHK' and upcase(name)='PATID'
    having monotonic() = min(monotonic());
quit;

%macro chk_ids;
%if %symexist(first_ds) and %superq(first_ds) ne %then %do;
    /* raw-format scan: flag any patid that doesn't look like a lessID */
    data _null_;
        set chk.&first_ds (obs=1000 keep=patid);
        if patid ne '' and not prxmatch('/^(PAT|ENC|PRV|FAC|ID)_[A-Z0-9]+_\\d+$/', strip(patid))
            then put 'WARN_RAW_ID patid=' patid;
    run;
    /* sample-based checks */
{absent_checks}
{present_checks}
%end;
%else %do;
    %put WARN_NO_PATID_TABLE no dataset with a patid column found in CPT;
%end;
%mend; %chk_ids;

proc datasets library=chk kill nolist; run; quit;
libname chk clear;
"""
        with _tf.NamedTemporaryFile(mode='w', suffix='.sas', delete=False) as tf:
            tf.write(sas_code)
            tf_path = tf.name

        log_path = tf_path.replace('.sas', '.log')
        result = subprocess.run(
            ['sas', '-nodms', '-log', log_path, tf_path],
            capture_output=True, text=True, timeout=300
        )
        os.unlink(tf_path)

        import shutil
        log = ""
        if os.path.exists(log_path):
            with open(log_path) as lf:
                log = lf.read()
            os.unlink(log_path)
        shutil.rmtree(chk_dir, ignore_errors=True)

        sas_errors      = [l for l in log.splitlines() if l.strip().startswith('ERROR')]
        warn_raw        = [l for l in log.splitlines() if l.startswith('WARN_RAW_ID')]
        warn_no_tbl     = [l for l in log.splitlines() if 'WARN_NO_PATID_TABLE' in l]
        fail_raw        = [l for l in log.splitlines() if l.startswith('FAIL_RAW_PRESENT')]
        fail_absent     = [l for l in log.splitlines() if l.startswith('FAIL_MAPPED_ABSENT')]
        pass_present    = [l for l in log.splitlines() if l.startswith('PASS_MAPPED_PRESENT')]

        if sas_errors:
            print(f"  [{FAIL}] SAS errors during CPT check:")
            for l in sas_errors[:5]:
                print(f"    {l}")
            errors += 1
        elif warn_no_tbl:
            print(f"  [{WARN}] CPT imported but contains no table with a patid column")
        else:
            if warn_raw:
                print(f"  [{FAIL}] CPT contains {len(warn_raw)} raw (un-mapped) patid value(s):")
                for l in warn_raw[:5]:
                    print(f"    {l}")
                errors += 1
            else:
                print(f"  [{PASS}] CPT raw-ID scan: no un-mapped patids in first 1 000 rows")

            if fail_raw:
                print(f"  [{FAIL}] Original IDs still present in CPT:")
                for l in fail_raw[:5]:
                    print(f"    {l}")
                errors += len(fail_raw)

            if fail_absent:
                print(f"  [{FAIL}] Expected mapped IDs missing from CPT:")
                for l in fail_absent[:5]:
                    print(f"    {l}")
                errors += len(fail_absent)

            if pass_present and not fail_raw and not fail_absent:
                print(f"  [{PASS}] {len(pass_present)}/{len(sample_pairs)} sampled lessIDs "
                      f"confirmed present; originals absent")

# ── Summary ─────────────────────────────────────────────────────────────────
print(f"\n{'═'*60}")
if errors == 0:
    print(f" {PASS}  All checks passed for {SITE}")
else:
    print(f" {FAIL}  {errors} check(s) FAILED for {SITE}")
print(f"{'═'*60}\n")
sys.exit(errors)
