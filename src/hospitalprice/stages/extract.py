"""Stage 4 - basket assembly.

price_raw holds everything the parsers saw for basket codes. This stage turns it
into the analysis table, and it is where the two conflations that corrupt this
literature are prevented:

  1. Setting. CPT 27447 appears as an outpatient facility rate, an inpatient
     component, and a professional fee, at wildly different dollars. Pooling
     across setting manufactures dispersion out of nothing, so (code, code_type,
     setting) is the service key and rates are never pooled across it.

  2. Offered vs. disclosed. A rehabilitation hospital has no colonoscopy price
     because it does not do colonoscopies. Absence from an MRF is NEVER treated
     as evidence a service is not offered; that determination requires external
     evidence and otherwise the cell is 'undeterminable'.
"""
from __future__ import annotations
from ..basket import BASKET, BY_CODE, COMPANION_CODES
from ..db import connect, record_attrition

# Provider categories that cannot perform the basket. Preregistered (§2), so it
# is an exclusion, not a disclosure failure.
NO_BASKET_CATEGORIES = {"rehab", "ltch", "psychiatric"}


def run() -> dict:
    con = connect()

    # Assign service_key on (code, code_type). Setting is validated, not assumed:
    # a row whose setting disagrees with the basket definition is kept but marked,
    # never silently relabelled.
    con.execute("UPDATE price_raw SET service_key=NULL")
    for s in BASKET:
        con.execute(
            "UPDATE price_raw SET service_key=? WHERE code=? AND "
            "(code_type=? OR code_type IS NULL)", [s.key, s.code, s.code_type])
    for code, key in COMPANION_CODES.items():
        con.execute("UPDATE price_raw SET service_key=? WHERE code=? AND service_key IS NULL",
                    [key, code])

    _service_offered(con)

    n_rows = con.execute("SELECT count(*) FROM price_raw WHERE service_key IS NOT NULL").fetchone()[0]
    n_neg = con.execute("SELECT count(*) FROM price_raw WHERE service_key IS NOT NULL "
                        "AND rate_type='negotiated' AND rate_dollars IS NOT NULL").fetchone()[0]
    n_pct_only = con.execute(
        "SELECT count(*) FROM price_raw WHERE rate_type='negotiated' "
        "AND rate_dollars IS NULL AND rate_percent IS NOT NULL "
        "AND estimated_allowed_amount IS NULL").fetchone()[0]
    n_est = con.execute(
        "SELECT count(*) FROM price_raw WHERE rate_type='negotiated' "
        "AND rate_dollars IS NULL AND estimated_allowed_amount IS NOT NULL").fetchone()[0]

    record_attrition(con, "4_extract", "basket_rows", n_rows, None, "")
    record_attrition(con, "4_extract", "negotiated_dollar_rows", n_neg, n_rows, "")
    record_attrition(con, "4_extract", "percent_only_no_dollar", n_pct_only, n_rows,
                     "NOT a price; coded missing, never imputed (prereg §6)")
    record_attrition(con, "4_extract", "percent_with_cy2025_estimated_amount", n_est, n_rows,
                     "CY2025 OPPS estimated allowed amount supplied; usable")

    per_service = con.execute(
        "SELECT service_key, count(DISTINCT ccn), count(*) FROM price_raw "
        "WHERE service_key IS NOT NULL AND rate_type='negotiated' "
        "AND rate_dollars IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
    for key, nh, nr in per_service:
        record_attrition(con, "4_extract", f"service:{key}", nh, None,
                         f"{nr} negotiated dollar rows")
    out = {"basket_rows": n_rows, "negotiated_dollar_rows": n_neg,
           "percent_only_unusable": n_pct_only, "percent_with_estimate": n_est,
           "per_service": [{"service": k, "hospitals": h, "rows": r} for k, h, r in per_service]}
    con.close()
    return out


def _service_offered(con) -> None:
    """Populate offered/not_offered/undeterminable from EXTERNAL evidence only."""
    con.execute("DELETE FROM service_offered")
    for s in BASKET:
        con.execute(
            "INSERT INTO service_offered SELECT ccn, ?, 'no', "
            "'provider category cannot perform this service (prereg §2)', "
            "'CMS CCN facility category' FROM hospital WHERE provider_category IN "
            "('rehab','ltch','psychiatric')", [s.key])
    # Obstetric services at hospitals with no obstetric capability would also be
    # 'no', but that requires service-line evidence we do not yet load; those
    # cells stay 'undeterminable' rather than being guessed.
    for s in BASKET:
        con.execute(
            "INSERT INTO service_offered SELECT h.ccn, ?, 'undeterminable', "
            "'no external service-line evidence loaded', 'none' FROM hospital h "
            "WHERE h.provider_category NOT IN ('rehab','ltch','psychiatric') "
            "AND NOT EXISTS (SELECT 1 FROM service_offered o WHERE o.ccn=h.ccn "
            "AND o.service_key=?)", [s.key, s.key])
