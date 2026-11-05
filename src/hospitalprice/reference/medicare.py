"""Medicare benchmark rates.

The commercial-to-Medicare ratio is the standard unit in the health economics
literature; without it our dollar figures are not comparable to published work.

Two sources, deliberately kept separate because they are different payment
systems and must not be pooled:
  - outpatient CPT/HCPCS -> Medicare Physician Fee Schedule (PFS) national
    non-facility/facility amounts, or OPPS APC where the service is facility-paid
  - inpatient MS-DRG     -> IPPS base rate x DRG relative weight

CMS reposts these files at new URLs every year, so the loader takes an explicit
URL or a local CSV. A stale or wrong benchmark silently rescales every ratio in
the paper, so the source and its hash are recorded on every row.
"""
from __future__ import annotations
import csv
import hashlib
from pathlib import Path

from ..db import connect

# IPPS FY2025 national standardized amount (operating + capital, full update,
# wage index 1.0). Documented here rather than hidden in a magic number.
IPPS_BASE_RATE_DEFAULT = 6852.00


def load_csv(path: str, year: int, source_label: str) -> int:
    """Ingest a benchmark CSV with columns:
       code, code_type, setting, national_rate [, wage_adjusted_rate, cbsa]
    """
    p = Path(path)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    con = connect()
    n = 0
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            # Prefer the per-row provenance the builder emits: a benchmark CSV
            # mixes OPPS, CLFS and IPPS rows, each from a different CMS file, so
            # one file-level label would misattribute most rows.
            src = " | ".join(x for x in (row.get("payment_system"), row.get("basis"),
                                         row.get("source_file")) if x) or source_label
            con.execute("INSERT INTO medicare_rate VALUES (?,?,?,?,?,?,?,?,?)",
                        [str(row["code"]).strip(), row["code_type"].strip().upper(),
                         row["setting"].strip().lower(), year,
                         float(row["national_rate"]) if row.get("national_rate") else None,
                         float(row["wage_adjusted_rate"]) if row.get("wage_adjusted_rate") else None,
                         row.get("cbsa"), src, row.get("source_sha256") or sha])
            n += 1
    con.close()
    return n


def load_drg_from_weights(path: str, year: int, base_rate: float = IPPS_BASE_RATE_DEFAULT) -> int:
    """IPPS Table 5: MS-DRG relative weights -> national payment.
    Expects columns: drg, weight (extra columns ignored)."""
    p = Path(path)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    con = connect()
    n = 0
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            drg = str(row.get("drg") or row.get("MS-DRG") or "").strip().lstrip("0")
            w = row.get("weight") or row.get("Weights")
            if not drg or not w:
                continue
            try:
                rate = float(str(w).strip()) * base_rate
            except ValueError:
                continue
            con.execute("INSERT INTO medicare_rate VALUES (?,?,?,?,?,?,?,?,?)",
                        [drg, "MS-DRG", "inpatient", year, rate, None, None,
                         f"IPPS Table 5 x base {base_rate}", sha])
            n += 1
    con.close()
    return n


def status() -> dict:
    con = connect(True)
    rows = con.execute(
        "SELECT code_type, setting, count(*), count(DISTINCT code) "
        "FROM medicare_rate GROUP BY 1,2").fetchall()
    con.close()
    return {"loaded": [{"code_type": r[0], "setting": r[1], "rows": r[2], "codes": r[3]}
                       for r in rows]}
