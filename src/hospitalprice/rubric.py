"""Stage 3 compliance rubric. FROZEN at tag prereg-v1.

Ten binary elements derived from 45 CFR 180.50 and the CY2024/CY2025 OPPS final
rules. Thresholds were declared before any price data was seen; the full score
distribution is published so a reader may impose their own line.
"""
from __future__ import annotations
from dataclasses import dataclass

ELEMENTS = [
    ("r1_retrievable",      "MRF exists and is retrievable without authentication", "180.50(a)"),
    ("r2_cms_hpt",          "cms-hpt.txt present at domain root and well-formed",   "CY2024 OPPS"),
    ("r3_machine_readable", "Machine-readable format (JSON/CSV/XML)",               "180.50(c)"),
    ("r4_gross",            "Contains gross charges",                               "180.50(b)(1)(i)"),
    ("r5_cash",             "Contains discounted cash prices",                      "180.50(b)(1)(ii)"),
    ("r6_named_payers",     "Payer-specific negotiated charges with NAMED payers",  "180.50(b)(1)(iii)"),
    ("r7_minmax",           "De-identified minimum and maximum negotiated charges", "180.50(b)(1)(iv)-(v)"),
    ("r8_codes_typed",      "Billing codes present with code type identified",      "180.50(b)(2)"),
    ("r9_cms_schema",       "Conforms to the CMS standardized schema",              "CY2024 OPPS"),
    ("r10_no_wall",         "Not behind login, captcha, click-through, or JS wall", "180.50(d)(3)"),
]
ELEMENT_KEYS = [k for k, _, _ in ELEMENTS]

# Necessary conditions for the primary "materially compliant" threshold: a file
# that cannot be retrieved, cannot be machine-read, does not name payers, or
# does not identify codes is unusable for the statute's purpose regardless of
# what else it contains.
MATERIAL_NECESSARY = ("r1_retrievable", "r3_machine_readable", "r6_named_payers", "r8_codes_typed")
MATERIAL_MIN_SCORE = 7


@dataclass
class Scored:
    elements: dict
    score: int
    strict: bool
    material: bool
    lenient: bool
    anonymized_payers: bool


def score(profile, discovery_row, has_wall: str | None = None) -> Scored:
    """profile: parsers.FileProfile (may be None if nothing was retrieved).
    discovery_row: dict with cms_hpt_wellformed, url_found, wall_type."""
    d = discovery_row or {}
    retrievable = bool(d.get("url_found")) and profile is not None and \
        getattr(profile, "detected_format", None) not in (None, "unknown")
    wall = (has_wall or d.get("wall_type") or "none")

    e = {k: False for k in ELEMENT_KEYS}
    e["r1_retrievable"] = bool(retrievable)
    e["r2_cms_hpt"] = bool(d.get("cms_hpt_wellformed"))
    e["r10_no_wall"] = wall in ("none", None, "")

    if profile is not None:
        e["r3_machine_readable"] = bool(profile.is_machine_readable)
        e["r4_gross"] = bool(profile.has_gross)
        e["r5_cash"] = bool(profile.has_cash)
        # A file with only anonymized labels fails R6 even though it technically
        # contains payer-specific rates. Anonymization is an evasion, not a
        # formatting defect, and is counted separately.
        real_names = [p for p in getattr(profile, "payer_names_seen", set())
                      if p and not _anon(p)]
        e["r6_named_payers"] = bool(profile.has_negotiated and real_names)
        e["r7_minmax"] = bool(profile.has_min and profile.has_max)
        e["r8_codes_typed"] = bool(profile.has_codes and profile.has_code_type)
        e["r9_cms_schema"] = bool(
            profile.cms_schema_version
            and str(profile.dialect or "").startswith(("cms_json", "cms_csv")))

    s = sum(1 for k in ELEMENT_KEYS if e[k])
    material = all(e[k] for k in MATERIAL_NECESSARY) and s >= MATERIAL_MIN_SCORE
    return Scored(elements=e, score=s, strict=(s == len(ELEMENT_KEYS)),
                  material=material,
                  lenient=e["r1_retrievable"] and e["r3_machine_readable"],
                  anonymized_payers=bool(getattr(profile, "anonymized_payers", False)))


def _anon(p: str) -> bool:
    from .parsers import is_anonymized_payer
    return is_anonymized_payer(p)
