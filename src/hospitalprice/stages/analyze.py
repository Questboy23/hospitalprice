"""Stage 5 - dispersion and regressions.

Every headline quantity is produced at three payer-normalization specifications
(certain / certain+probable / all). If the headline moves more than 10% across
them, the preregistration requires the point estimate be withdrawn in favour of
an interval -- that check is executed here, not left to the writer's judgment.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from ..basket import PLACEBO_KEYS, SURGICAL_KEYS
from ..config import TABLES
from ..db import connect, record_attrition

# Plausibility screen. A negotiated rate hundreds of times the Medicare rate for
# the same code is not a unit price: hospitals encode outlier/stop-loss
# thresholds, case rates, and bundle prices on the same row as the CPT (observed:
# a "basic metabolic panel" carrying $63,065.48 with the note "APC Price... if
# cost exceeds the threshold, the outlier payment is added"). Extracting that
# number is correct; calling it the price of a blood test is not.
#
# Flagged rows are EXCLUDED from the primary specification and COUNTED, never
# deleted, and the unscreened result is reported alongside (prereg §6).
IMPLAUSIBLE_HIGH_X_MEDICARE = 100.0
IMPLAUSIBLE_LOW_X_MEDICARE = 0.05

SPECS = {
    "certain": ("certain",),
    "certain_probable": ("certain", "probable"),          # primary
    "all": ("certain", "probable", "ambiguous"),
}
PRIMARY = "certain_probable"


# Rate methodology is part of the contract's identity, not a footnote. A case
# rate, a per-diem, a percent-of-charge and a fee-schedule amount for the SAME
# CPT are different objects; comparing them produces spread that is an artifact
# of contract structure rather than a difference in price. Observed: within one
# hospital, lab rows run $5.51-$250.70 under percent-of-charge and $0.62-$63,065
# under case-rate, against a Medicare rate of $8.46.
#
# The primary specification therefore compares fee-schedule rates only. The
# unrestricted result is reported alongside. (Prereg amendment 2026-09-02;
# see docs/prereg_amendments.md.)
PRIMARY_METHODOLOGY = ("fee_schedule",)


def _rates(con, spec: str, plan_type: str = "commercial",
           screen: bool = True, methodology: tuple | None = None) -> pd.DataFrame:
    confs = SPECS[spec]
    meth_sql = ""
    if methodology:
        meth_sql = " AND p.methodology IN (" + ",".join("'%s'" % m for m in methodology) + ")"
    screen_sql = ("" if not screen else f"""
        AND NOT EXISTS (
          SELECT 1 FROM medicare_rate m
          WHERE m.code=p.code AND m.code_type=p.code_type AND m.setting=p.setting
            AND m.national_rate > 0
            AND (p.rate_dollars > {IMPLAUSIBLE_HIGH_X_MEDICARE} * m.national_rate
              OR p.rate_dollars < {IMPLAUSIBLE_LOW_X_MEDICARE} * m.national_rate))
    """)
    q = f"""
      SELECT p.ccn, p.service_key, p.code, p.code_type, p.setting,
             ps.parent_entity, ps.brand, ps.plan_type, p.payer_string_raw,
             p.rate_dollars, p.methodology, p.billing_class,
             h.provider_category, h.ownership, h.beds, h.system_name, h.cbsa, h.rural
      FROM price_raw p
      JOIN payer_string ps USING(payer_string_raw)
      JOIN hospital h ON h.ccn=p.ccn
      WHERE p.rate_type='negotiated' AND p.rate_dollars IS NOT NULL
        AND p.service_key IS NOT NULL
        AND ps.plan_type = ?
        AND ps.confidence IN ({','.join('?' * len(confs))})
        AND ps.parent_entity IS NOT NULL
        {screen_sql}{meth_sql}
    """
    return con.execute(q, [plan_type, *confs]).df()


def count_implausible(con) -> dict:
    """How many rows the screen removes, and from where. Reported, not hidden."""
    q = f"""
      SELECT p.service_key, count(*) n
      FROM price_raw p JOIN medicare_rate m
        ON m.code=p.code AND m.code_type=p.code_type AND m.setting=p.setting
      WHERE p.rate_type='negotiated' AND p.rate_dollars IS NOT NULL
        AND m.national_rate > 0
        AND (p.rate_dollars > {IMPLAUSIBLE_HIGH_X_MEDICARE} * m.national_rate
          OR p.rate_dollars < {IMPLAUSIBLE_LOW_X_MEDICARE} * m.national_rate)
      GROUP BY 1 ORDER BY 2 DESC
    """
    rows = con.execute(q).fetchall()
    return {"by_service": {r[0]: r[1] for r in rows}, "total": sum(r[1] for r in rows)}


def within_hospital(df: pd.DataFrame) -> pd.DataFrame:
    """Dispersion across payers inside one hospital, per service.

    Collapsed to one rate per (hospital, service, parent payer) first: a payer
    listed under six plan names would otherwise inflate the spread by counting
    the same contract repeatedly.
    """
    if df.empty:
        return pd.DataFrame()
    g = (df.groupby(["ccn", "service_key", "setting", "parent_entity"], dropna=False)
           ["rate_dollars"].median().reset_index())
    rows = []
    for (ccn, svc, setting), sub in g.groupby(["ccn", "service_key", "setting"]):
        v = sub["rate_dollars"].dropna().values
        if len(v) < 2:
            continue
        q1, q3 = np.percentile(v, [25, 75])
        rows.append({"ccn": ccn, "service_key": svc, "setting": setting,
                     "n_payers": len(v), "min": v.min(), "max": v.max(),
                     "median": float(np.median(v)),
                     "max_min_ratio": float(v.max() / v.min()) if v.min() > 0 else np.nan,
                     "cv": float(v.std(ddof=1) / v.mean()) if v.mean() > 0 else np.nan,
                     "iqr_ratio": float(q3 / q1) if q1 > 0 else np.nan})
    return pd.DataFrame(rows)


def across_hospital(con, df: pd.DataFrame) -> pd.DataFrame:
    """Statewide distribution per service, normalized to Medicare where loaded."""
    if df.empty:
        return pd.DataFrame()
    med = con.execute(
        "SELECT code, code_type, setting, national_rate FROM medicare_rate").df()
    h = (df.groupby(["ccn", "service_key", "code", "code_type", "setting"], dropna=False)
           ["rate_dollars"].median().reset_index())
    if not med.empty:
        h = h.merge(med, on=["code", "code_type", "setting"], how="left")
        h["ratio_to_medicare"] = h["rate_dollars"] / h["national_rate"]
    else:
        h["national_rate"] = np.nan
        h["ratio_to_medicare"] = np.nan
    out = (h.groupby(["service_key", "setting"])
             .agg(n_hospitals=("ccn", "nunique"),
                  p10=("rate_dollars", lambda s: s.quantile(.10)),
                  median=("rate_dollars", "median"),
                  p90=("rate_dollars", lambda s: s.quantile(.90)),
                  max_min_ratio=("rate_dollars", lambda s: s.max() / s.min() if s.min() > 0 else np.nan),
                  median_ratio_to_medicare=("ratio_to_medicare", "median"))
             .reset_index())
    return out, h


def placebo_test(within: pd.DataFrame) -> dict:
    """Preregistered validity check (§3). Unambiguous single-line labs must show
    materially LESS dispersion than surgical procedures. If they do not, the
    pipeline is measuring parse noise and the dispersion results are invalid."""
    if within.empty:
        return {"status": "no_data"}
    lab = within[within.service_key.isin(PLACEBO_KEYS)]["max_min_ratio"].dropna()
    surg = within[within.service_key.isin(SURGICAL_KEYS)]["max_min_ratio"].dropna()
    if len(lab) < 5 or len(surg) < 5:
        return {"status": "insufficient_data", "n_lab": int(len(lab)), "n_surgical": int(len(surg))}
    lm, sm = float(lab.median()), float(surg.median())
    return {"status": "ran", "n_lab": int(len(lab)), "n_surgical": int(len(surg)),
            "median_ratio_lab": round(lm, 3), "median_ratio_surgical": round(sm, 3),
            "verdict": "PASS" if lm < sm else
                       "FAIL - lab dispersion >= surgical; extraction is generating "
                       "noise, dispersion results invalid (prereg §3/§10)"}


def compute_hhi(con) -> pd.DataFrame:
    """CBSA-level HHI from staffed-bed shares.

    A PROXY. The literature standard is discharge-weighted patient-flow HHI,
    which needs Ohio discharge data that is not public. Labelled as a proxy in
    every table that uses it.
    """
    df = con.execute(
        "SELECT cbsa, ccn, coalesce(beds,0) beds, system_name FROM hospital "
        "WHERE cbsa IS NOT NULL").df()
    if df.empty or df["beds"].sum() == 0:
        return pd.DataFrame(columns=["geography", "hhi", "n_hospitals"])
    # System-level shares: two hospitals in one system are one competitor.
    df["unit"] = df["system_name"].fillna(df["ccn"])
    rows = []
    for cbsa, sub in df.groupby("cbsa"):
        tot = sub["beds"].sum()
        if tot <= 0:
            continue
        shares = sub.groupby("unit")["beds"].sum() / tot
        rows.append({"geography": cbsa, "hhi": float((shares ** 2).sum() * 10000),
                     "n_hospitals": int(sub["ccn"].nunique())})
    out = pd.DataFrame(rows)
    con.execute("DELETE FROM market_hhi")
    for _, r in out.iterrows():
        con.execute("INSERT INTO market_hhi VALUES ('CBSA',?,?,?,?)",
                    [r["geography"], r["hhi"], int(r["n_hospitals"]),
                     "staffed_bed_share (PROXY; not discharge-weighted)"])
    return out


def regressions(con, df: pd.DataFrame) -> dict:
    """Spec A (compliance), B1 (within-hospital price), B2 (across-hospital).

    B1 and B2 are NOT nested and are reported as separate columns: hospital
    fixed effects absorb hospital-level HHI, so a single specification
    containing both is not estimable. Neither is interpreted causally.
    """
    import statsmodels.formula.api as smf
    out: dict = {}

    comp = con.execute("""
        SELECT c.ccn, c.score, h.ownership, h.beds, h.provider_category,
               h.system_name, h.rural, m.hhi
        FROM compliance c JOIN hospital h USING(ccn)
        LEFT JOIN market_hhi m ON m.geography=h.cbsa
        WHERE c.attribution_rule='named_in_file'
    """).df()
    if len(comp) > 20:
        comp["log_beds"] = np.log(comp["beds"].fillna(comp["beds"].median() if comp["beds"].notna().any() else 100).clip(lower=1))
        comp["in_system"] = comp["system_name"].notna().astype(int)
        comp["rural_i"] = comp["rural"].fillna(False).astype(int)
        terms = ["C(ownership)", "log_beds", "in_system"]
        if comp["hhi"].notna().sum() > 20:
            terms.append("hhi")
        try:
            grp = comp["system_name"].fillna(comp["ccn"])
            m = smf.ols("score ~ " + " + ".join(terms), data=comp).fit(
                cov_type="cluster", cov_kwds={"groups": grp})
            out["A_compliance"] = _tidy(m, "compliance_score ~ ownership + log(beds) + "
                                           "system + HHI; SE clustered by system",
                                        n_clusters=int(grp.nunique()))
        except Exception as e:
            out["A_compliance"] = {"error": str(e)}
    else:
        out["A_compliance"] = {"error": f"insufficient N ({len(comp)})"}

    if not df.empty and df["ccn"].nunique() > 10:
        d = df.dropna(subset=["rate_dollars"]).copy()
        d = d[d["rate_dollars"] > 0]
        d["log_rate"] = np.log(d["rate_dollars"])
        try:
            m1 = smf.ols("log_rate ~ C(parent_entity) + C(service_key) + C(ccn)",
                         data=d).fit(cov_type="cluster",
                                     cov_kwds={"groups": d["system_name"].fillna(d["ccn"])})
            out["B1_within_hospital"] = _tidy(
                m1, "log(rate) ~ payer + hospital FE + service FE. Identifies "
                    "ACROSS-PAYER variation within a hospital. Hospital-level "
                    "covariates incl. HHI are collinear with hospital FE and are "
                    "absent BY CONSTRUCTION, not by oversight.",
                keep_prefix="C(parent_entity)")
        except Exception as e:
            out["B1_within_hospital"] = {"error": str(e)}

        d2 = d.merge(con.execute("SELECT geography cbsa, hhi FROM market_hhi").df(),
                     on="cbsa", how="left")
        terms = ["C(ownership)", "C(service_key)"]
        if d2["hhi"].notna().sum() > 50:
            terms.insert(0, "hhi")
        if d2["beds"].notna().sum() > 50:
            d2["log_beds"] = np.log(d2["beds"].clip(lower=1))
            terms.append("log_beds")
        try:
            m2 = smf.ols("log_rate ~ " + " + ".join(terms), data=d2).fit(
                cov_type="cluster", cov_kwds={"groups": d2["system_name"].fillna(d2["ccn"])})
            out["B2_across_hospital"] = _tidy(
                m2, "log(rate) ~ HHI + ownership + beds + service FE, NO hospital FE. "
                    "Identifies CROSS-HOSPITAL variation. Descriptive, not causal; "
                    "estimated on hospitals that chose to disclose.")
        except Exception as e:
            out["B2_across_hospital"] = {"error": str(e)}
    return out


def _tidy(m, spec: str, keep_prefix: str | None = None, n_clusters: int | None = None) -> dict:
    terms = []
    for name in m.params.index:
        if keep_prefix and not (name.startswith(keep_prefix) or name == "Intercept"):
            continue
        terms.append({"term": name, "coef": round(float(m.params[name]), 5),
                      "se": round(float(m.bse[name]), 5),
                      "t": round(float(m.tvalues[name]), 3),
                      "p": round(float(m.pvalues[name]), 5)})
    return {"specification": spec, "n_obs": int(m.nobs),
            "r2": round(float(m.rsquared), 4), "n_clusters": n_clusters,
            "terms": terms[:60],
            "caveat": "Descriptive association on a self-selected sample of "
                      "disclosing hospitals. Not causal."}


def run() -> dict:
    con = connect()
    compute_hhi(con)
    results: dict = {"specs": {}}

    for spec in SPECS:
        df = _rates(con, spec, "commercial", methodology=PRIMARY_METHODOLOGY)
        w = within_hospital(df)
        acr, per_hosp = across_hospital(con, df) if not df.empty else (pd.DataFrame(), pd.DataFrame())
        results["specs"][spec] = {
            "n_rows": int(len(df)),
            "n_hospitals": int(df["ccn"].nunique()) if not df.empty else 0,
            "median_within_hospital_max_min_ratio":
                round(float(w["max_min_ratio"].median()), 4) if not w.empty else None,
            "median_cv": round(float(w["cv"].median()), 4) if not w.empty else None,
        }
        if spec == PRIMARY:
            if not w.empty:
                w.to_csv(TABLES / "within_hospital_dispersion.csv", index=False)
            if not acr.empty:
                acr.to_csv(TABLES / "across_hospital_dispersion.csv", index=False)
            if not per_hosp.empty:
                per_hosp.to_csv(TABLES / "hospital_service_rates.csv", index=False)
            results["placebo"] = placebo_test(w)
            results["placebo_all_methodologies"] = placebo_test(within_hospital(
                _rates(con, spec, "commercial", methodology=None)))
            results["placebo_unscreened"] = placebo_test(within_hospital(
                _rates(con, spec, "commercial", screen=False, methodology=None)))
            results["implausible_rows_screened"] = count_implausible(con)
            results["regressions"] = regressions(con, df)

    # Preregistered fragility test (§5).
    vals = [v["median_within_hospital_max_min_ratio"] for v in results["specs"].values()
            if v["median_within_hospital_max_min_ratio"] is not None]
    if len(vals) >= 2 and min(vals) > 0:
        spread = (max(vals) - min(vals)) / min(vals)
        results["payer_spec_fragility"] = {
            "min": min(vals), "max": max(vals), "relative_spread": round(spread, 4),
            "verdict": ("STABLE - point estimate may be reported" if spread <= 0.10 else
                        "FRAGILE - prereg §5 requires the point estimate be WITHDRAWN "
                        "and an interval reported; payer normalization is the binding "
                        "source of uncertainty")}
    else:
        results["payer_spec_fragility"] = {"status": "insufficient_data"}

    for spec, v in results["specs"].items():
        record_attrition(con, "5_analyze", f"hospitals_with_commercial_rates[{spec}]",
                         v["n_hospitals"], None, "")
    con.close()
    return results


def balance_table() -> pd.DataFrame:
    """Disclosers vs non-disclosers. Required output (prereg §8): the dispersion
    sample is conditional on complying and is not a random sample of Ohio."""
    con = connect()
    df = con.execute("""
        SELECT h.ccn, h.provider_category, h.ownership, h.beds, h.rural,
               h.system_name IS NOT NULL AS in_system,
               coalesce(c.materially_compliant,false) AS discloser
        FROM hospital h
        LEFT JOIN compliance c ON c.ccn=h.ccn AND c.attribution_rule='named_in_file'
    """).df()
    con.close()
    if df.empty:
        return df
    rows = []
    for col in ["beds", "in_system", "rural"]:
        if col not in df or df[col].isna().all():
            continue
        a = df[df.discloser][col].astype(float)
        b = df[~df.discloser][col].astype(float)
        rows.append({"variable": col, "disclosers_mean": round(a.mean(), 3),
                     "nondisclosers_mean": round(b.mean(), 3),
                     "n_disclosers": int(a.notna().sum()),
                     "n_nondisclosers": int(b.notna().sum()),
                     "difference": round(a.mean() - b.mean(), 3)})
    for cat in ["ownership", "provider_category"]:
        for val, sub in df.groupby(cat):
            rows.append({"variable": f"{cat}={val}",
                         "disclosers_mean": round(sub.discloser.mean(), 3),
                         "nondisclosers_mean": None,
                         "n_disclosers": int(sub.discloser.sum()),
                         "n_nondisclosers": int((~sub.discloser).sum()),
                         "difference": None})
    out = pd.DataFrame(rows)
    out.to_csv(TABLES / "balance_disclosers_vs_nondisclosers.csv", index=False)
    return out
