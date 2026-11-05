"""Build the Medicare benchmark CSV from CMS source files.

Each basket service is benchmarked against the payment system that ACTUALLY
pays for it at a hospital. This is not a detail: using one blanket fee schedule
silently rescales every commercial-to-Medicare ratio in the paper.

  hospital outpatient facility services -> OPPS Addendum B (APC payment rate)
  clinical laboratory tests             -> CLFS (OPPS lists these as SI=Q4 with
                                           NO payment rate; they are paid under
                                           the lab fee schedule when billed alone)
  inpatient stays                       -> IPPS: base rate x MS-DRG weight

Sources are the CMS files cached in data/cache/, and every emitted row carries
the sha256 of the file it came from.

Usage:  python scripts/build_medicare_benchmarks.py
Then:   hospitalprice medicare --csv out/tables/medicare_benchmarks.csv --year 2026
"""
from __future__ import annotations
import csv
import hashlib
import sys
import zipfile
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "out" / "tables" / "medicare_benchmarks.csv"

# FY2026 IPPS national standardized amounts, Tables 1A + 1D (quality-reporting,
# meaningful EHR user, wage index > 1). Parsed from the cached file below.
IPPS_LABOR, IPPS_NONLABOR, IPPS_CAPITAL = 4456.72, 2295.89, 524.15

OPPS_ZIP = CACHE / "july-2026-opps-addendum-b.zip"
CLFS_ZIP = CACHE / "26clabq3.zip"
DRG_ZIP = CACHE / "fy2026-ipps-fr-table-5.zip"

OUTPATIENT = ["70551", "74177", "45378", "66984", "99283", "27447", "45380", "45385"]
LABS = ["80048", "85025"]
DRGS = ["470", "788", "807"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _xlsx_from_zip(z: Path, suffix=".xlsx"):
    with zipfile.ZipFile(z) as zf:
        name = next(n for n in zf.namelist() if n.endswith(suffix) and "/" not in n.strip("/")
                    or n.endswith(suffix))
        tmp = CACHE / f"_tmp_{Path(name).name}"
        tmp.write_bytes(zf.read(name))
    return tmp


def opps_rates() -> dict:
    p = _xlsx_from_zip(OPPS_ZIP)
    ws = openpyxl.load_workbook(p, read_only=True, data_only=True).worksheets[0]
    hdr, out = None, {}
    for r in ws.iter_rows(values_only=True):
        c = [(str(x).strip() if x is not None else "") for x in r]
        if hdr is None:
            if c and c[0].upper().startswith("HCPCS") and any("Payment Rate" in v for v in c):
                hdr = c
                ci = {v: i for i, v in enumerate(c)}
            continue
        if c and c[0] in OUTPATIENT:
            rate = c[ci["Payment Rate"]]
            si = c[ci["SI"]]
            if rate:
                out[c[0]] = (float(rate), si, c[1])
    p.unlink(missing_ok=True)
    return out


def clfs_rates() -> dict:
    out = {}
    with zipfile.ZipFile(CLFS_ZIP) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        text = zf.read(name).decode("utf-8", "replace").splitlines()
    for row in csv.DictReader(text[4:]):
        if row.get("HCPCS") in LABS and not (row.get("MOD") or "").strip():
            out[row["HCPCS"]] = (float(row["RATE"]), row.get("SHORTDESC", ""))
    return out


def drg_rates() -> dict:
    p = _xlsx_from_zip(DRG_ZIP)
    ws = openpyxl.load_workbook(p, read_only=True, data_only=True).worksheets[0]
    base = IPPS_LABOR + IPPS_NONLABOR + IPPS_CAPITAL
    hdr, out = None, {}
    for r in ws.iter_rows(values_only=True):
        c = [(str(x).strip() if x is not None else "") for x in r]
        if hdr is None:
            if c and c[0].upper().startswith("MS-DRG"):
                hdr = c
                wi = c.index("Weights - 10% Cap Applied")
            continue
        code = c[0].lstrip("0") or "0"
        if code in DRGS and c[wi]:
            w = float(c[wi])
            out[code] = (round(w * base, 2), w, c[5])
    p.unlink(missing_ok=True)
    return out


def main() -> int:
    missing = [z for z in (OPPS_ZIP, CLFS_ZIP, DRG_ZIP) if not z.exists()]
    if missing:
        print("missing cached CMS sources:", *[m.name for m in missing], sep="\n  ")
        return 1
    opps, clfs, drg = opps_rates(), clfs_rates(), drg_rates()
    base = IPPS_LABOR + IPPS_NONLABOR + IPPS_CAPITAL
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "code_type", "setting", "national_rate", "payment_system",
                    "basis", "source_file", "source_sha256", "descriptor"])
        for code, (rate, si, desc) in sorted(opps.items()):
            w.writerow([code, "CPT", "outpatient", rate, "OPPS",
                        f"Addendum B APC payment rate (SI={si})",
                        OPPS_ZIP.name, sha(OPPS_ZIP), desc])
        for code, (rate, desc) in sorted(clfs.items()):
            w.writerow([code, "CPT", "outpatient", rate, "CLFS",
                        "Clinical Lab Fee Schedule (OPPS SI=Q4, no APC rate)",
                        CLFS_ZIP.name, sha(CLFS_ZIP), desc])
        for code, (rate, weight, desc) in sorted(drg.items()):
            w.writerow([code, "MS-DRG", "inpatient", rate, "IPPS",
                        f"weight {weight} x base {base:.2f} "
                        f"(operating {IPPS_LABOR}+{IPPS_NONLABOR} + capital {IPPS_CAPITAL})",
                        DRG_ZIP.name, sha(DRG_ZIP), desc])
    n = len(opps) + len(clfs) + len(drg)
    print(f"wrote {n} benchmark rows -> {OUT}")
    print(f"  OPPS {len(opps)}  CLFS {len(clfs)}  IPPS {len(drg)}   IPPS base ${base:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
