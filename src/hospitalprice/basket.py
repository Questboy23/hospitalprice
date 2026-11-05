"""The preregistered shoppable-services basket. FROZEN at tag prereg-v1.

Service identity is (code, code_type, setting). Rates are NEVER pooled across
settings: an inpatient DRG rate and an outpatient CPT rate for the same clinical
procedure are different products at different prices.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    code: str
    code_type: str      # CPT | HCPCS | MS-DRG | NDC
    setting: str        # inpatient | outpatient | professional
    placebo: bool = False   # unambiguous single-line service, used as noise check


BASKET: tuple[Service, ...] = (
    Service("mri_brain_wo",   "MRI brain without contrast",      "70551", "CPT",    "outpatient"),
    Service("ct_abd_pelvis",  "CT abdomen/pelvis with contrast", "74177", "CPT",    "outpatient"),
    Service("colonoscopy_scr","Screening colonoscopy",           "45378", "CPT",    "outpatient"),
    Service("bmp",            "Basic metabolic panel",           "80048", "CPT",    "outpatient", placebo=True),
    Service("cbc_diff",       "CBC with differential",           "85025", "CPT",    "outpatient", placebo=True),
    Service("delivery_vag",   "Vaginal delivery",                "807",   "MS-DRG", "inpatient"),
    Service("delivery_cs",    "Cesarean delivery",               "788",   "MS-DRG", "inpatient"),
    Service("tka_op",         "Total knee arthroplasty (OP)",    "27447", "CPT",    "outpatient"),
    Service("tka_ip",         "Total knee arthroplasty (IP)",    "470",   "MS-DRG", "inpatient"),
    Service("cataract",       "Cataract surgery",                "66984", "CPT",    "outpatient"),
    Service("ed_level3",      "ED visit, level 3",               "99283", "CPT",    "outpatient"),
)

# Recorded where present, analyzed separately, NEVER substituted for 45378.
COMPANION_CODES = {"45380": "colonoscopy_diag", "45385": "colonoscopy_diag"}

BY_CODE: dict[tuple[str, str], Service] = {(s.code, s.code_type): s for s in BASKET}
ALL_CODES: frozenset[str] = frozenset(s.code for s in BASKET) | frozenset(COMPANION_CODES)
PLACEBO_KEYS = frozenset(s.key for s in BASKET if s.placebo)
SURGICAL_KEYS = frozenset({"tka_op", "tka_ip", "cataract", "colonoscopy_scr"})


def normalize_code(raw: str | None) -> str | None:
    """Strip modifiers/padding so '00470', '470.0', 'DRG-470' all match '470'."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    for pre in ("MS-DRG", "MSDRG", "DRG", "APR-DRG", "CPT", "HCPCS"):
        if s.startswith(pre):
            s = s[len(pre):].lstrip(" -:_")
    s = s.split(".")[0].split("-")[0].strip()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s or None
