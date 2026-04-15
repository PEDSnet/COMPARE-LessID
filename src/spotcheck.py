#!/usr/bin/env python3
"""
spotcheck.py <site_name>

Interactive spot-check for a de-identified site.

Commands
--------
  orig <id>       Lookup what an original ID maps to
  new  <id>       Lookup what a de-identified ID came from
  sample [n]      Show n random sample (orig → new) pairs  [default 10]
  xlsx [file]     Show first 15 replaced Patient IDs from an XLSX output file
  stats           Re-print mapping statistics
  q / quit        Exit
"""

import csv
import glob
import os
import random
import re
import sys
import warnings
import openpyxl
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import XLSX_COLUMN_MAP, PAT_ID_NAMES, patch_openpyxl
patch_openpyxl()

# ── Paths — read from environment or fall back to defaults ──────────────────
CPT_BASE    = os.environ.get("CPT_BASE",    "REDACTED:/data/sas_queries/<source_user>/compare_q01")
OUT_BASE    = os.environ.get("OUT_BASE",    "REDACTED:/data/sas_queries/<your_user>/lessid_drnoc")
LOOKUP_BASE = os.environ.get("LOOKUP_BASE", "REDACTED:/data/sas_queries/<your_user>/lessid_lookup")

# ── Colour helpers ──────────────────────────────────────────────────────────
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def hi(s):   return f"{BOLD}{s}{RESET}"
def ok(s):   return f"{GREEN}{s}{RESET}"
def warn(s): return f"{YELLOW}{s}{RESET}"
def info(s): return f"{CYAN}{s}{RESET}"

# ── Argument ────────────────────────────────────────────────────────────────
if len(sys.argv) != 2:
    print("Usage: spotcheck.py <site_name>")
    print("  e.g. spotcheck.py C7LC_compare_deq_q01")
    sys.exit(1)

SITE        = sys.argv[1]
out_dir     = os.path.join(OUT_BASE,    SITE)
mapping_csv = os.path.join(LOOKUP_BASE, SITE, "mapping.csv")

if not os.path.exists(mapping_csv):
    print(warn(f"mapping.csv not found: {mapping_csv}"))
    sys.exit(1)

# ── Load mapping ────────────────────────────────────────────────────────────
print(info(f"Loading {mapping_csv} ..."), end="", flush=True)
forward  = {}   # (col, orig) -> new_id
reverse  = {}   # new_id      -> (col, orig)
by_col   = {}   # col -> count
by_pfx   = {}   # prefix (PAT/ENC/…) -> count

with open(mapping_csv, newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        col = (row.get("column") or "").strip().lower()
        val = (row.get("original_value") or "").strip()
        nid = (row.get("new_id") or "").strip()
        if not col or not val or not nid:
            continue
        forward[(col, val)] = nid
        reverse[nid] = (col, val)
        by_col[col]  = by_col.get(col, 0)  + 1
        pfx = nid.split("_")[0]
        by_pfx[pfx]  = by_pfx.get(pfx, 0) + 1

print(ok(f" {len(forward):,} entries"))


def print_stats():
    print(f"\n{hi('Mapping statistics for')} {SITE}")
    print(f"  Total entries : {len(forward):,}")
    print(f"  By prefix     :")
    for p in sorted(by_pfx):
        print(f"    {p:6s}  {by_pfx[p]:>10,}")
    print(f"  By column (top 10):")
    for col, cnt in sorted(by_col.items(), key=lambda x: -x[1])[:10]:
        print(f"    {col:30s}  {cnt:>10,}")
    xlsx_out = sorted(glob.glob(os.path.join(out_dir, "*.xlsx")))
    print(f"  Output XLSX files: {len(xlsx_out)}")
    for f in xlsx_out:
        mb = os.path.getsize(f) / 1024 / 1024
        print(f"    {os.path.basename(f)}  ({mb:.1f} MB)")


def lookup_orig(orig_id):
    """Find all mappings for an original value (across all columns)."""
    results = [(col, nid) for (col, val), nid in forward.items() if val == orig_id]
    if not results:
        print(warn(f"  '{orig_id}' not found in mapping (not an ID we track, or already de-id'd)"))
        return
    for col, nid in sorted(results):
        print(f"  {info(col):30s}  {orig_id}  →  {ok(nid)}")


def lookup_new(new_id):
    """Find what original value a de-identified ID came from."""
    entry = reverse.get(new_id)
    if not entry:
        print(warn(f"  '{new_id}' not found in mapping"))
        return
    col, orig = entry
    print(f"  {info(col):30s}  {ok(new_id)}  ←  {orig}")


def show_sample(n=10):
    """Print n random (original → mapped) pairs from the patid column."""
    pat_pairs = [(orig, nid) for (col, orig), nid in forward.items() if col == "patid"]
    if not pat_pairs:
        print(warn("  No patid entries found in mapping"))
        return
    sample = random.sample(pat_pairs, min(n, len(pat_pairs)))
    print(f"\n  {hi('Random sample')} ({len(sample)} of {len(pat_pairs):,} patid mappings):")
    for orig, nid in sample:
        print(f"    {orig:40s}  →  {ok(nid)}")


def show_xlsx(xlsx_name=None):
    """Show first 15 replaced patient IDs from an output XLSX."""
    if xlsx_name:
        if not os.path.isabs(xlsx_name):
            xlsx_name = os.path.join(out_dir, xlsx_name)
        candidates = [xlsx_name] if os.path.exists(xlsx_name) else []
    else:
        candidates = sorted(glob.glob(os.path.join(out_dir, "*.xlsx")))

    if not candidates:
        print(warn(f"  No XLSX files found in {out_dir}"))
        return

    shown = False
    for path in candidates:
        name = os.path.basename(path)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                wb = openpyxl.load_workbook(path, read_only=True)
            except Exception as e:
                print(warn(f"  Could not open {name}: {e}"))
                continue
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            headers = [str(h).strip() if h is not None else "" for h in rows[0]]
            if headers and headers[0] == "No eligible records found":
                continue
            pat_idx = next(
                (i for i, h in enumerate(headers)
                 if XLSX_COLUMN_MAP.get(h.lower()) == "patid"
                 or h.lower() in PAT_ID_NAMES),
                None
            )
            if pat_idx is None:
                continue
            print(f"\n  {hi(name)}  — sheet: {ws.title}")
            print(f"  {'Original patid':40s}  →  Mapped patid")
            print(f"  {'─' * 40}     {'─' * 40}")
            count = 0
            for row in rows[1:]:
                mapped = str(row[pat_idx]).strip() if row[pat_idx] is not None else ""
                if not mapped:
                    continue
                orig_entry = reverse.get(mapped)
                if orig_entry:
                    _, orig = orig_entry
                    print(f"  {orig:40s}  →  {ok(mapped)}")
                    count += 1
                    if count >= 15:
                        remaining = sum(1 for r in rows[rows.index(row)+1:] if r[pat_idx])
                        if remaining:
                            print(f"  ... ({remaining} more rows not shown)")
                        break
            shown = True
        wb.close()
        if shown:
            break


# ── REPL ────────────────────────────────────────────────────────────────────
print_stats()
print(f"\n{hi('Commands:')}  orig <id>  |  new <id>  |  sample [n]  |  xlsx [file]  |  stats  |  q")
print("─" * 70)

try:
    while True:
        try:
            line = input(f"\n{BOLD}spotcheck>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "orig":
            if not arg:
                print(warn("  Usage: orig <original_id>"))
            else:
                lookup_orig(arg)
        elif cmd == "new":
            if not arg:
                print(warn("  Usage: new <mapped_id>"))
            else:
                lookup_new(arg)
        elif cmd == "sample":
            try:
                n = int(arg) if arg else 10
            except ValueError:
                n = 10
            show_sample(n)
        elif cmd == "xlsx":
            show_xlsx(arg if arg else None)
        elif cmd == "stats":
            print_stats()
        else:
            print(warn(f"  Unknown command '{cmd}'. Try: orig / new / sample / xlsx / stats / q"))
except Exception as e:
    print(warn(f"\nError: {e}"))

print("Bye.")
