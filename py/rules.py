"""
De-identification column rules — mirrors _build__default_mappings.

    remap_id        qr/id$/i
    remap_label     site, facilityid, pro_response_text, vx_lot_num
    alias_attributes providerid -> [medadmin_providerid, obsgen_providerid,
                                    obsclin_providerid, rx_providerid, vx_providerid]
    redact_value    qr/^raw_|^trial_invite_code$|^provider_npi$|result_text$|zip9$/i
    remap_date      qr/_date$/i   (handled in lessid.sas)
    remap_datetime  qr/_time$/i   (handled in lessid.sas)
"""

import re

# ── Patterns ────────────────────────────────────────────────────────────────

REMAP_ID_RE = re.compile(r'id$', re.IGNORECASE)

# Specific columns to remap that don't match the id$ pattern
REMAP_LABEL = frozenset({'site', 'pro_response_text', 'vx_lot_num'})

# Values in these columns are blanked rather than replaced with a new ID
REDACT_RE = re.compile(
    r'^raw_|^trial_invite_code$|^provider_npi$|result_text$|zip9$',
    re.IGNORECASE,
)

# Column names (lowercased) that look like IDs but must NOT be remapped.
# "participant id" is a trial enrolment number, not a CDM patient identifier.
REMAP_NEVER = frozenset({
    'participant id',
})

# ── Aliases ─────────────────────────────────────────────────────────────────
# These columns share the mapping key of their canonical column, so a provider
# value gets the same new_id regardless of which provider column it appeared in.

ALIAS_TO_CANONICAL: dict[str, str] = {
    'medadmin_providerid': 'providerid',
    'obsgen_providerid':   'providerid',
    'obsclin_providerid':  'providerid',
    'rx_providerid':       'providerid',
    'vx_providerid':       'providerid',
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
    return bool(REMAP_ID_RE.search(c))


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
