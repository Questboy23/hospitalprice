"""Stage 3 - compliance scoring, at TWO attribution rules.

Whether the fifteen hospitals on one system domain are each credited for one
system file is the single largest lever on the headline number, so both rules
are computed and both are reported:

  named_in_file  - a hospital is credited only where the file (or cms-hpt.txt)
                   names it. Conservative; risks understating compliance.
  domain_credit  - any hospital whose domain serves a compliant file is
                   credited. Permissive; risks overstating it.

Reporting one without the other would be a choice disguised as a measurement.
"""
from __future__ import annotations
import json
import random
from datetime import datetime, timezone

from ..config import SEED, TABLES
from ..db import connect, record_attrition
from ..parsers import FileProfile
from ..rubric import ELEMENT_KEYS, score as score_one

ATTRIBUTION_RULES = ("named_in_file", "domain_credit")


def _profile_from_db(row) -> FileProfile:
    """Rebuild the structural profile recorded at fetch time. Scoring never
    re-downloads: at 300GB of MRFs a second pass is not affordable."""
    p = FileProfile()
    (p.dialect, p.rows_scanned, status, p.cms_schema_version) = row[:4]
    p.detected_format = ("json" if "json" in (p.dialect or "") else
                         "csv" if "csv" in (p.dialect or "") else
                         (p.dialect or "").split(":")[-1] or None)
    p.is_machine_readable = status != "not_machine_readable"
    return p


def run() -> dict:
    con = connect()
    now = datetime.now(timezone.utc)
    con.execute("DELETE FROM compliance")

    hospitals = con.execute(
        "SELECT ccn, provider_category, ownership FROM hospital ORDER BY ccn").fetchall()

    for rule in ATTRIBUTION_RULES:
        for ccn, _cat, _own in hospitals:
            d = con.execute(
                "SELECT url_found, cms_hpt_wellformed, wall_type, discovery_method "
                "FROM discovery WHERE ccn=? LIMIT 1", [ccn]).fetchone()
            drow = {"url_found": d[0], "cms_hpt_wellformed": d[1],
                    "wall_type": d[2], "discovery_method": d[3]} if d else {}

            if rule == "named_in_file":
                # credit only files that actually name this hospital
                f = con.execute(
                    "SELECT m.dialect, m.rows_scanned, m.parse_status, NULL, m.file_id "
                    "FROM mrf_file m JOIN file_hospital_attribution a "
                    "  ON a.file_id=m.file_id AND a.ccn=m.ccn "
                    "WHERE m.ccn=? AND a.named_in_file "
                    "ORDER BY m.parse_status='ok' DESC LIMIT 1", [ccn]).fetchone()
                if f is None and drow.get("discovery_method") != "cms_hpt_txt":
                    f = None
                elif f is None:
                    f = con.execute(
                        "SELECT dialect, rows_scanned, parse_status, NULL, file_id "
                        "FROM mrf_file WHERE ccn=? ORDER BY parse_status='ok' DESC LIMIT 1",
                        [ccn]).fetchone()
            else:
                f = con.execute(
                    "SELECT dialect, rows_scanned, parse_status, NULL, file_id "
                    "FROM mrf_file WHERE ccn=? ORDER BY parse_status='ok' DESC LIMIT 1",
                    [ccn]).fetchone()

            prof = None
            file_id = None
            if f is not None:
                prof = _profile_from_db(f)
                file_id = f[4]
                agg = con.execute(
                    "SELECT max(rate_type='gross'), max(rate_type='cash'), "
                    "max(rate_type='min'), max(rate_type='max'), "
                    "max(rate_type='negotiated'), max(code IS NOT NULL), "
                    "max(code_type IS NOT NULL) FROM price_raw WHERE file_id=?",
                    [file_id]).fetchone()
                if agg and any(agg):
                    (prof.has_gross, prof.has_cash, prof.has_min, prof.has_max,
                     prof.has_negotiated, prof.has_codes, prof.has_code_type) = [bool(x) for x in agg]
                names = con.execute(
                    "SELECT DISTINCT payer_string_raw FROM price_raw "
                    "WHERE file_id=? AND payer_string_raw IS NOT NULL LIMIT 500",
                    [file_id]).fetchall()
                prof.payer_names_seen = {n[0] for n in names}
                # R9 must reflect a DECLARED CMS schema version, not merely that
                # our CSV/JSON parser succeeded. Crediting any cms_* dialect makes
                # R9 true by construction and turns the rubric into a measure of
                # parser success rather than of compliance.
                ver = con.execute(
                    "SELECT cms_schema_version FROM mrf_file WHERE file_id=?",
                    [file_id]).fetchone()
                prof.cms_schema_version = ver[0] if ver and ver[0] else None

            s = score_one(prof, drow)
            con.execute(
                "INSERT INTO compliance VALUES (" + ",".join(["?"] * 19) + ")",
                [ccn, file_id] + [s.elements[k] for k in ELEMENT_KEYS] +
                [s.score, s.anonymized_payers, s.material, s.strict, s.lenient, rule, now])

    n = len(hospitals)
    out = {"frame": n}
    for rule in ATTRIBUTION_RULES:
        g = lambda w: con.execute(
            f"SELECT count(*) FROM compliance WHERE attribution_rule=? AND {w}", [rule]).fetchone()[0]
        out[rule] = {"strict": g("strict_compliant"),
                     "material": g("materially_compliant"),
                     "lenient": g("lenient_compliant"),
                     "anonymized_payers": g("anonymized_payers")}
        record_attrition(con, "3_score", f"materially_compliant[{rule}]",
                         out[rule]["material"], n, f"attribution rule = {rule}")

    _sensitivity(con)
    _sample_for_manual_review(con)
    con.close()
    return out


def _sensitivity(con) -> None:
    """Compliance rate at every threshold 0..10. The headline is a function of
    where the line is drawn, so the whole curve is published."""
    rows = []
    n = con.execute("SELECT count(*) FROM hospital").fetchone()[0]
    for rule in ATTRIBUTION_RULES:
        for t in range(0, 11):
            c = con.execute(
                "SELECT count(*) FROM compliance WHERE attribution_rule=? AND score>=?",
                [rule, t]).fetchone()[0]
            rows.append((rule, t, c, n, round(100.0 * c / n, 2) if n else None))
    import csv
    with open(TABLES / "compliance_threshold_sensitivity.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["attribution_rule", "threshold_min_score", "n_compliant", "n_frame", "pct"])
        w.writerows(rows)


def _sample_for_manual_review(con) -> None:
    """Draw the preregistered 25-hospital validation sample (seed 20260902).
    Scored by hand, blind to the automated result; kappa is reported. If
    kappa < 0.80 the rubric is reported as invalid rather than tuned."""
    import csv
    ccns = [r[0] for r in con.execute("SELECT ccn FROM hospital ORDER BY ccn").fetchall()]
    rnd = random.Random(SEED)
    sample = sorted(rnd.sample(ccns, min(25, len(ccns))))
    with open(TABLES / "manual_review_sample.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ccn", "name", "domain"] + ELEMENT_KEYS + ["reviewer", "notes"])
        for ccn in sample:
            r = con.execute(
                "SELECT h.name, d.domain FROM hospital h LEFT JOIN hospital_domain d USING(ccn) "
                "WHERE h.ccn=? LIMIT 1", [ccn]).fetchone()
            w.writerow([ccn, r[0] if r else "", r[1] if r else ""] + [""] * len(ELEMENT_KEYS) + ["", ""])


def kappa() -> dict:
    """Cohen's kappa between manual and automated element-level scores."""
    con = connect()
    man = con.execute("SELECT ccn, elements FROM compliance_manual").fetchall()
    if not man:
        con.close()
        return {"error": "no manual scores loaded; fill out/tables/manual_review_sample.csv"}
    a = b = 0
    agree = 0
    n = 0
    both = [0, 0, 0, 0]
    for ccn, elems in man:
        m = json.loads(elems)
        auto = con.execute(
            "SELECT " + ",".join(ELEMENT_KEYS) +
            " FROM compliance WHERE ccn=? AND attribution_rule='named_in_file'", [ccn]).fetchone()
        if auto is None:
            continue
        for k, av in zip(ELEMENT_KEYS, auto):
            mv = bool(m.get(k))
            av = bool(av)
            n += 1
            agree += (mv == av)
            both[(1 if mv else 0) * 2 + (1 if av else 0)] += 1
    if n == 0:
        con.close()
        return {"error": "no overlapping scored hospitals"}
    po = agree / n
    p_m1 = (both[2] + both[3]) / n
    p_a1 = (both[1] + both[3]) / n
    pe = p_m1 * p_a1 + (1 - p_m1) * (1 - p_a1)
    k = (po - pe) / (1 - pe) if pe < 1 else 1.0
    con.close()
    return {"n_element_comparisons": n, "percent_agreement": round(po, 4),
            "cohens_kappa": round(k, 4),
            "verdict": "PASS" if k >= 0.80 else
                       "FAIL - rubric measures parser robustness, not compliance (prereg §4)"}
