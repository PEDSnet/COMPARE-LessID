"""
De-identification column rules — mirrors _build__default_mappings.

    remap_id        qr/id$/i
    remap_label     site, facilityid, pro_response_text, vx_lot_num
    alias_attributes providerid -> [medadmin_providerid, obsgen_providerid,
                                    obsclin_providerid, rx_providerid, vx_providerid]
    redact_value    qr/^raw_|^trial_invite_code$|^provider_npi$|result_text$|zip9$/i
    remap_date      qr/_date$/i   (handled in lessid.sas)
    remap_datetime  qr/_time$/i   (handled in lessid.sas)

Configuration is loaded from config/lessid.toml relative to the repo root if
the file exists; otherwise hardcoded defaults are used.  This keeps the module
fully backwards-compatible when called directly (no config file required).
"""

import re
import sys
from pathlib import Path

# ── Config loading ───────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Return the parsed [columns] section from config/lessid.toml, or {}."""
    # Repo root is two levels up from this file (py/rules.py → py/ → repo root)
    repo_root = Path(__file__).parent.parent
    cfg_path = repo_root / 'config' / 'lessid.toml'
    if not cfg_path.exists():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(cfg_path, 'rb') as f:
                data = tomllib.load(f)
        else:
            import tomli  # type: ignore[import]
            with open(cfg_path, 'rb') as f:
                data = tomli.load(f)
        return data.get('columns', {})
    except Exception:
        return {}

_cfg = _load_config()

# ── Patterns ────────────────────────────────────────────────────────────────

REMAP_ID_RE = re.compile(r'id$', re.IGNORECASE)

# Specific columns to remap that don't match the id$ pattern
REMAP_LABEL = frozenset({'pro_response_text', 'vx_lot_num'})

# Values in these columns are blanked rather than replaced with a new ID
REDACT_RE = re.compile(
    r'^raw_|^trial_invite_code$|^provider_npi$|result_text$|zip9$',
    re.IGNORECASE,
)

# Column names (lowercased) that look like IDs but must NOT be remapped.
# - participantid: trial enrolment number, not a CDM patient identifier
# - datamartid: identifies the data mart, not a person
# - trialid: study label (always 'PT_COMPARE'), not a person identifier
_default_never = {'participantid', 'datamartid', 'trialid'}
REMAP_NEVER: frozenset[str] = frozenset(
    c.lower() for c in _cfg.get('remap_never', _default_never)
)

# Additional columns to remap beyond the id$ pattern
REMAP_EXTRA: frozenset[str] = frozenset(
    c.lower() for c in _cfg.get('remap_extra', [])
)

# id$-matching columns to explicitly exclude from remapping
REMAP_EXCLUDE: frozenset[str] = frozenset(
    c.lower() for c in _cfg.get('remap_exclude', [])
)

# ── Aliases ─────────────────────────────────────────────────────────────────
# Build alias→canonical map from config, or fall back to empty (aliases not
# used by default since these provider columns don't appear in this dataset).

_default_aliases: dict[str, list[str]] = {}
_cfg_aliases: dict[str, list[str]] = _cfg.get('aliases', _default_aliases)

ALIAS_TO_CANONICAL: dict[str, str] = {
    alias.lower(): canonical.lower()
    for canonical, aliases in _cfg_aliases.items()
    for alias in (aliases if isinstance(aliases, list) else [])
}

# ── Public helpers ───────────────────────────────────────────────────────────

def is_redact_col(col: str) -> bool:
    """True if this column's values should be blanked (not mapped)."""
    return bool(REDACT_RE.search(col))


def is_remap_col(col: str) -> bool:
    """True if this column's values should be replaced with a de-identified ID."""
    c = col.lower()
    if c in REMAP_NEVER:
        return False
    if c in REMAP_EXCLUDE:
        return False
    return bool(REMAP_ID_RE.search(c)) or c in REMAP_EXTRA


def mapping_col(col: str) -> str:
    """Return the canonical mapping-key column name (resolves aliases)."""
    c = col.lower()
    return ALIAS_TO_CANONICAL.get(c, c)


def prefix_for(col: str) -> str:
    """Return the new-ID prefix (PAT / ENC / PRV / FAC / ID) for a column."""
    c = col.lower()
    if c in {'patid', 'person_id', 'org_patid'}:
        return 'PAT'
    if c in {'encounterid', 'visit_id'}:
        return 'ENC'
    if c == 'providerid' or c.endswith('_providerid'):
        return 'PRV'
    if c in {'facilityid', 'lab_facilityid', 'trial_siteid', 'site'}:
        return 'FAC'
    return 'ID'


def load_paths() -> dict:
    """Return the [paths] section from config/lessid.toml, or {}."""
    repo_root = Path(__file__).parent.parent
    cfg_path = repo_root / 'config' / 'lessid.toml'
    if not cfg_path.exists():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(cfg_path, 'rb') as f:
                return tomllib.load(f).get('paths', {})
        else:
            import tomli  # type: ignore[import]
            with open(cfg_path, 'rb') as f:
                return tomli.load(f).get('paths', {})
    except Exception:
        return {}


def load_processing() -> dict:
    """Return the [processing] section from config/lessid.toml, or {}."""
    repo_root = Path(__file__).parent.parent
    cfg_path = repo_root / 'config' / 'lessid.toml'
    if not cfg_path.exists():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(cfg_path, 'rb') as f:
                return tomllib.load(f).get('processing', {})
        else:
            import tomli  # type: ignore[import]
            with open(cfg_path, 'rb') as f:
                return tomli.load(f).get('processing', {})
    except Exception:
        return {}

