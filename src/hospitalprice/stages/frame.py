"""Stage 1 - sample frame.

The denominator determines whether the compliance number means anything, so the
frame is built and frozen BEFORE any MRF is touched.

Sources (each cached with sha256 + retrieval timestamp):
  - CMS Hospital Enrollments (PECOS). Covers rehab and LTCH, which Hospital
    Compare omits entirely, and carries the subgroup flags.
  - CMS Hospital General Information. Authoritative ownership and hospital type
    for acute / CAH / psych / children's.
  - CMS Hospital Provider Cost Report (HCRIS) for beds and revenue.

Unit of observation is the CCN. Facility category is derived from the CCN
facility-type range, which is the authoritative CMS categorization -- not from
free-text hospital_type, which disagrees across datasets.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone

from ..config import CACHE, STATE
from ..db import connect, record_attrition
from ..http import cached_download

ENROLLMENT_URL = ("https://data.cms.gov/data-api/v1/dataset/"
                  "f6f6505c-e8b0-4d57-b258-e2b94133aaf2/data?filter[STATE]=OH&size=5000")
HGI_URL = ("https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
           "?limit=1000&conditions[0][property]=state&conditions[0][value]=OH"
           "&conditions[0][operator]==")
HCRIS_URL = ("https://data.cms.gov/sites/default/files/2026-01/"
             "3c39f483-c7e0-4025-8396-4df76942e10f/CostReport_2023_Final.csv")

# CMS CCN facility-type ranges (positions 3-6). Authoritative categorization.
FACILITY_RANGES = [
    ((1, 879), "short_term_acute"),
    ((1300, 1399), "critical_access"),
    ((2000, 2299), "ltch"),
    ((3025, 3099), "rehab"),
    ((3300, 3399), "childrens"),
    ((4000, 4499), "psychiatric"),
]
IN_FRAME = {"short_term_acute", "critical_access", "ltch", "rehab", "childrens", "psychiatric"}
# Subunit CCNs (letter in position 3) are psych/rehab UNITS inside a parent
# hospital. They are not separately-licensed facilities and do not publish their
# own MRF; the parent hospital's file covers them. Excluded, and counted.
SUBUNIT_LETTERS = {"S": "psych_unit", "T": "rehab_unit", "R": "rehab_unit",
                   "U": "swing_bed", "M": "subunit_other", "Z": "subunit_other"}


def categorize(ccn: str) -> str:
    s = (ccn or "").strip().upper()
    if len(s) != 6:
        return "malformed"
    suffix = s[2:]
    if not suffix.isdigit():
        return SUBUNIT_LETTERS.get(suffix[0], f"subunit_{suffix[0]}")
    n = int(suffix)
    for (lo, hi), label in FACILITY_RANGES:
        if lo <= n <= hi:
            return label
    return "other_range"


def normalize_ownership(hgi_own: str | None, prop_nonprofit: str | None) -> str:
    o = (hgi_own or "").lower()
    if "veterans" in o or "defense" in o:
        return "federal"
    if "non-profit" in o or "nonprofit" in o:
        return "nonprofit"
    if "proprietary" in o:
        return "forprofit"
    if "government" in o or "district" in o or "authority" in o:
        return "government"
    if prop_nonprofit == "N":
        return "nonprofit"
    if prop_nonprofit == "P":
        return "forprofit"
    return "unknown"


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(refresh: bool = False) -> int:
    con = connect()
    now = datetime.now(timezone.utc)

    enr_p = cached_download(ENROLLMENT_URL, "cms_hospital_enrollments_OH.json", "frame", con, force=refresh)
    hgi_p = cached_download(HGI_URL, "cms_hospital_general_info_OH.json", "frame", con, force=refresh)
    enr_sha, hgi_sha = _sha(enr_p), _sha(hgi_p)

    enrollment = json.loads(enr_p.read_text())
    hgi_raw = json.loads(hgi_p.read_text())
    hgi = {r["facility_id"]: r for r in hgi_raw.get("results", hgi_raw if isinstance(hgi_raw, list) else [])}

    enr_by_ccn: dict[str, list[dict]] = {}
    for row in enrollment:
        enr_by_ccn.setdefault((row.get("CCN") or "").strip(), []).append(row)
    enr_by_ccn.pop("", None)

    all_ccns = sorted(set(hgi) | set(enr_by_ccn))
    record_attrition(con, "1_frame", "union_of_cms_sources", len(all_ccns), None,
                     f"HGI={len(hgi)} enrollment_ccns={len(enr_by_ccn)}")

    cats = {c: categorize(c) for c in all_ccns}
    subunits = [c for c, k in cats.items() if k not in IN_FRAME]
    record_attrition(con, "1_frame", "excluded_subunit_or_nonfacility_ccn", len(subunits),
                     len(all_ccns), "psych/rehab/swing units inside a parent hospital; "
                                    "covered by the parent's MRF, do not publish their own")

    con.execute("DELETE FROM hospital")
    kept = 0
    for ccn in all_ccns:
        cat = cats[ccn]
        if cat not in IN_FRAME:
            continue
        h = hgi.get(ccn, {})
        e_rows = enr_by_ccn.get(ccn, [])
        e = next((r for r in e_rows if r.get("PRACTICE LOCATION TYPE") == "MAIN/PRIMARY HOSPITAL LOCATION"),
                 e_rows[0] if e_rows else {})
        zc = (h.get("zip_code") or e.get("ZIP CODE") or "")[:5]
        con.execute(
            "INSERT INTO hospital (ccn,name,dba_name,address,city,state,zip,county,"
            "provider_category,ownership_raw,ownership,beds,system_name,system_id,"
            "cbsa,rural,source_dataset,source_sha256,retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL,NULL,?,?,?)",
            [ccn,
             h.get("facility_name") or e.get("ORGANIZATION NAME"),
             e.get("DOING BUSINESS AS NAME"),
             h.get("address") or e.get("ADDRESS LINE 1"),
             h.get("citytown") or e.get("CITY"),
             STATE, zc,
             h.get("countyparish"),
             cat,
             h.get("hospital_ownership") or e.get("PROPRIETARY NONPROFIT"),
             normalize_ownership(h.get("hospital_ownership"), e.get("PROPRIETARY NONPROFIT")),
             e.get("ASSOCIATE ID"),
             "hgi+enrollment" if ccn in hgi and ccn in enr_by_ccn else ("hgi" if ccn in hgi else "enrollment"),
             f"hgi:{hgi_sha[:16]},enr:{enr_sha[:16]}",
             now],
        )
        kept += 1

    record_attrition(con, "1_frame", "frame_facilities", kept, len(all_ccns),
                     "unit = CCN; categories: short_term_acute, critical_access, "
                     "ltch, rehab, childrens, psychiatric")

    fed = con.execute("SELECT count(*) FROM hospital WHERE ownership='federal'").fetchone()[0]
    record_attrition(con, "1_frame", "federal_out_of_scope_flagged", fed, kept,
                     "VA/DoD: 45 CFR 180.20 excludes federally owned/operated "
                     "facilities. RETAINED in frame and flagged, not silently dropped.")

    breakdown = con.execute(
        "SELECT provider_category, count(*) FROM hospital GROUP BY 1 ORDER BY 2 DESC").fetchall()
    for cat, n in breakdown:
        record_attrition(con, "1_frame", f"category:{cat}", n, kept, "")
    con.close()
    return kept
