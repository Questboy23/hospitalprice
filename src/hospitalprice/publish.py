"""Export one snapshot for reedsmoneymaker23.com/hospitals.

The site is a READ-ONLY MIRROR. Every number it shows is computed here, in the
research repo, and written to a single JSON file. The site computes nothing:
two code paths deriving the same number is how a dashboard starts disagreeing
with the research it reports on.

The snapshot carries the failed validity tests as prominently as the results.
A dashboard that shows only what worked is a advertisement, not a research page.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from .config import OUT, TABLES
from .db import connect
from .stages import analyze


def build() -> dict:
    con = connect(True)
    g = lambda q, p=[]: con.execute(q, p).fetchone()

    frame = g("SELECT count(*) FROM hospital")[0]
    by_cat = con.execute(
        "SELECT provider_category, count(*) FROM hospital GROUP BY 1 ORDER BY 2 DESC").fetchall()
    by_own = con.execute(
        "SELECT ownership, count(*) FROM hospital GROUP BY 1 ORDER BY 2 DESC").fetchall()

    domains = g("SELECT count(DISTINCT ccn) FROM hospital_domain")[0]
    hpt = g("SELECT count(DISTINCT ccn) FROM discovery WHERE cms_hpt_present")[0]
    hpt_ok = g("SELECT count(DISTINCT ccn) FROM discovery WHERE cms_hpt_wellformed")[0]
    via_hpt = g("SELECT count(DISTINCT ccn) FROM discovery WHERE discovery_method='cms_hpt_txt'")[0]
    via_footer = g("SELECT count(DISTINCT ccn) FROM discovery WHERE discovery_method='footer'")[0]
    urls = g("SELECT count(DISTINCT ccn) FROM discovery WHERE url_found")[0]

    parsed = g("SELECT count(DISTINCT ccn) FROM mrf_file WHERE parse_status='ok'")[0]
    failed = g("SELECT count(DISTINCT ccn) FROM mrf_file WHERE parse_status='failed'")[0]
    gb = g("SELECT coalesce(sum(file_size_bytes),0) FROM mrf_file WHERE parse_status='ok'")[0] / 1e9
    biggest = con.execute(
        "SELECT h.name, m.file_size_bytes/1e9 FROM mrf_file m JOIN hospital h USING(ccn) "
        "WHERE m.parse_status='ok' ORDER BY 2 DESC LIMIT 5").fetchall()
    dialects = con.execute(
        "SELECT dialect, count(*) FROM mrf_file WHERE parse_status='ok' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    schema_v = con.execute(
        "SELECT coalesce(cms_schema_version,'(none declared)'), count(*) FROM mrf_file "
        "WHERE parse_status='ok' GROUP BY 1 ORDER BY 2 DESC").fetchall()

    comp = {}
    for rule in ("named_in_file", "domain_credit"):
        r = g("""SELECT count(*) FILTER (WHERE lenient_compliant),
                        count(*) FILTER (WHERE materially_compliant),
                        count(*) FILTER (WHERE strict_compliant),
                        count(*) FILTER (WHERE anonymized_payers)
                 FROM compliance WHERE attribution_rule=?""", [rule])
        comp[rule] = {"lenient": r[0], "material": r[1], "strict": r[2],
                      "anonymized_payers": r[3], "denominator": frame}

    elements = []
    for k in ["r1_retrievable", "r2_cms_hpt", "r3_machine_readable", "r4_gross",
              "r5_cash", "r6_named_payers", "r7_minmax", "r8_codes_typed",
              "r9_cms_schema", "r10_no_wall"]:
        n = g(f"SELECT count(*) FROM compliance WHERE attribution_rule='named_in_file' "
              f"AND {k} AND ccn IN (SELECT ccn FROM mrf_file WHERE parse_status='ok')")[0]
        elements.append({"element": k, "n_passing": n, "of": parsed})

    sens = []
    for t in range(11):
        n = g("SELECT count(*) FROM compliance WHERE attribution_rule='named_in_file' AND score>=?",
              [t])[0]
        sens.append({"threshold": t, "n": n, "pct": round(100.0 * n / frame, 1)})

    payers = g("SELECT count(*), count(*) FILTER (WHERE confidence IN ('certain','probable')), "
               "count(*) FILTER (WHERE anonymized) FROM payer_string")
    top_payers = con.execute(
        "SELECT payer_string_raw, n_rows, n_hospitals, coalesce(parent_entity,'(unresolved)'), "
        "plan_type FROM payer_string ORDER BY n_rows DESC LIMIT 15").fetchall()
    rows_cov = g("SELECT count(*) FILTER (WHERE ps.confidence IN ('certain','probable')), count(*) "
                 "FROM price_raw p JOIN payer_string ps USING(payer_string_raw) "
                 "WHERE p.rate_type='negotiated'")

    services = con.execute(
        "SELECT service_key, count(DISTINCT ccn), count(*) FROM price_raw "
        "WHERE service_key IS NOT NULL AND rate_type='negotiated' AND rate_dollars IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    medicare = con.execute(
        "SELECT code, code_type, setting, national_rate, source FROM medicare_rate "
        "ORDER BY national_rate DESC").fetchall()
    attrition = con.execute(
        "SELECT stage, step, n, denominator, note FROM attrition "
        "QUALIFY row_number() OVER (PARTITION BY stage, step ORDER BY recorded_at DESC)=1 "
        "ORDER BY stage, step").fetchall()
    con.close()

    a = analyze.run()

    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": "Ohio Hospital Price Transparency",
        "status": "IN PROGRESS - partial crawl. Compliance denominators are not final.",
        "headline": {
            "frame": frame,
            "domains_resolved": domains,
            "mrf_urls_found": urls,
            "hospitals_parsed": parsed,
            "gb_streamed": round(gb, 1),
            "price_rows": g if False else None,
        },
        "frame": {"total": frame,
                  "by_category": [{"k": k, "n": n} for k, n in by_cat],
                  "by_ownership": [{"k": k, "n": n} for k, n in by_own]},
        "discovery": {"domains_resolved": domains, "cms_hpt_present": hpt,
                      "cms_hpt_wellformed": hpt_ok, "via_cms_hpt": via_hpt,
                      "via_footer": via_footer, "urls_found": urls,
                      "note": ("Every MRF URL located so far came from cms-hpt.txt. "
                               "Footer scraping contributed none.")},
        "acquisition": {"parsed": parsed, "failed": failed, "gb_streamed": round(gb, 1),
                        "biggest": [{"name": n, "gb": round(b, 2)} for n, b in biggest],
                        "dialects": [{"k": k, "n": n} for k, n in dialects],
                        "schema_versions": [{"k": k, "n": n} for k, n in schema_v],
                        "storage_note": ("Stream-only: files are hashed and parsed in flight "
                                         "and the bytes discarded. The build machine has "
                                         "under 10GB free against ~300GB of MRFs.")},
        "compliance": comp,
        "compliance_elements": elements,
        "threshold_sensitivity": sens,
        "payers": {"distinct_strings": payers[0], "resolved": payers[1],
                   "anonymized": payers[2],
                   "rows_covered": rows_cov[0], "rows_total": rows_cov[1],
                   "coverage_pct": round(100.0 * rows_cov[0] / rows_cov[1], 1) if rows_cov[1] else None,
                   "top": [{"raw": r[0], "rows": r[1], "hospitals": r[2],
                            "parent": r[3], "plan_type": r[4]} for r in top_payers],
                   "note": ("String similarity proposes candidates for human review and "
                            "never merges. Unresolved strings are excluded from the "
                            "primary analysis, not quietly merged.")},
        "services": [{"service": s, "hospitals": h, "rows": r} for s, h, r in services],
        "medicare": [{"code": c, "code_type": t, "setting": st, "rate": r, "source": src}
                     for c, t, st, r, src in medicare],
        "dispersion": a["specs"],
        "validity": {
            "placebo_primary": a.get("placebo"),
            "placebo_all_methodologies": a.get("placebo_all_methodologies"),
            "placebo_unscreened": a.get("placebo_unscreened"),
            "fragility": a.get("payer_spec_fragility"),
            "implausible_screened": a.get("implausible_rows_screened"),
        },
        "attrition": [{"stage": s, "step": st, "n": n, "of": d, "note": nt}
                      for s, st, n, d, nt in attrition],
        "honesty": [
            "Preregistered before any price data was retrieved; frozen at git tag prereg-v1.",
            "Both attribution rules are reported, because crediting every hospital on a "
            "system domain for one file is the largest single lever on the headline number.",
            "No price is ever imputed. A percent-of-charge with no dollar amount is missing, "
            "not zero.",
            "Absence from an MRF is never treated as evidence a hospital does not offer a "
            "service.",
            "The preregistered placebo test FAILS and is reported as mis-specified rather "
            "than tuned until it passes. See the validity section.",
        ],
    }
    snap["headline"]["price_rows"] = sum(s["rows"] for s in snap["services"])
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "site_snapshot.json"
    p.write_text(json.dumps(snap, indent=2, default=str))
    return {"path": str(p), "bytes": p.stat().st_size,
            "sections": len(snap)}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
