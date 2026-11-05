"""Stage 6 - publication tables, figures, and the public CSV.

Every figure ships with the CSV that produced it: the palette's mid-chroma hues
sit below 3:1 against the surface, which obligates visible labels and a table
view rather than relying on color alone.
"""
from __future__ import annotations
import csv
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..basket import BASKET
from ..config import FIGURES, TABLES
from ..db import connect
from ..rubric import ELEMENTS, ELEMENT_KEYS

# Validated categorical slots (light mode, surface #fcfcfb):
# all four PASS lightness, chroma, CVD separation and normal-vision floor.
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, INK2, MUTED, SURFACE = "#0b0b0b", "#52514e", "#8a8880", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "font.size": 9,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK2, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": "#e8e7e3", "grid.linewidth": 0.6,
    "axes.axisbelow": True, "legend.frameon": False,
})


def _save(fig, name):
    fig.tight_layout()
    for ext in ("png", "pdf"):
        fig.savefig(FIGURES / f"{name}.{ext}", dpi=200)
    plt.close(fig)
    return f"{name}.png"


def hospital_compliance_csv(con) -> int:
    df = con.execute(f"""
        SELECT h.ccn, h.name, h.city, h.county, h.provider_category, h.ownership,
               h.beds, h.system_name, h.rural,
               d.domain, x.discovery_method, x.cms_hpt_present, x.cms_hpt_wellformed,
               x.url AS mrf_url, x.wall_type,
               f.sha256 AS file_sha256, f.file_size_bytes, f.content_type,
               f.download_timestamp, f.dialect, f.parse_status,
               c.attribution_rule, {','.join('c.'+k for k in ELEMENT_KEYS)},
               c.score, c.anonymized_payers, c.materially_compliant,
               c.strict_compliant, c.lenient_compliant
        FROM hospital h
        LEFT JOIN hospital_domain d USING(ccn)
        LEFT JOIN discovery x USING(ccn)
        LEFT JOIN mrf_file f ON f.ccn=h.ccn
        LEFT JOIN compliance c ON c.ccn=h.ccn
        ORDER BY h.ccn, c.attribution_rule
    """).df()
    df.to_csv(TABLES / "hospital_compliance.csv", index=False)
    return len(df)


def price_basket_csv(con) -> int:
    df = con.execute("""
        SELECT p.ccn, h.name AS hospital_name, p.service_key, p.code, p.code_type,
               p.setting, p.billing_class,
               p.payer_string_raw, p.plan_string_raw,
               ps.parent_entity, ps.brand, ps.plan_type, ps.confidence AS payer_confidence,
               ps.rule_id AS payer_rule_id, ps.anonymized AS payer_anonymized,
               p.rate_type, p.rate_dollars, p.rate_percent,
               p.estimated_allowed_amount, p.methodology,
               p.source_sha256, p.byte_offset, p.parser_dialect, p.download_date
        FROM price_raw p
        LEFT JOIN hospital h USING(ccn)
        LEFT JOIN payer_string ps USING(payer_string_raw)
        WHERE p.service_key IS NOT NULL
        ORDER BY p.ccn, p.service_key, ps.parent_entity
    """).df()
    df.to_csv(TABLES / "price_basket.csv", index=False)
    return len(df)


def attrition_csv(con) -> int:
    df = con.execute(
        "SELECT stage, step, n, denominator, "
        "round(100.0*n/nullif(denominator,0),2) AS pct_of_denominator, note, recorded_at "
        "FROM attrition ORDER BY recorded_at").df()
    df.to_csv(TABLES / "attrition_table.csv", index=False)
    return len(df)


def fig_compliance_by_ownership(con):
    df = con.execute("""
        SELECT h.ownership,
               count(*) n,
               100.0*avg(CASE WHEN c.lenient_compliant THEN 1 ELSE 0 END) AS "lenient",
               100.0*avg(CASE WHEN c.materially_compliant THEN 1 ELSE 0 END) AS "material",
               100.0*avg(CASE WHEN c.strict_compliant THEN 1 ELSE 0 END) AS "strict"
        FROM hospital h JOIN compliance c USING(ccn)
        WHERE c.attribution_rule='named_in_file'
        GROUP BY 1 ORDER BY n DESC
    """).df()
    if df.empty:
        return None
    df.to_csv(TABLES / "fig_compliance_by_ownership.csv", index=False)
    x = np.arange(len(df))
    w = 0.26
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    for i, (col, lab) in enumerate([("lenient", "Lenient (file exists, machine-readable)"),
                                    ("material", "Material (primary threshold)"),
                                    ("strict", "Strict (all 10 elements)")]):
        bars = ax.bar(x + (i - 1) * w, df[col], w, color=C[i], label=lab,
                      edgecolor=SURFACE, linewidth=2)
        for b, v in zip(bars, df[col]):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.2, f"{v:.0f}",
                    ha="center", va="bottom", fontsize=7.5, color=INK2)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{o}\n(n={n})" for o, n in zip(df.ownership, df.n)])
    ax.set_ylabel("% of hospitals compliant")
    ax.set_ylim(0, 105)
    ax.set_title("Ohio hospital price-transparency compliance, by ownership",
                 loc="left", fontsize=11, color=INK)
    ax.legend(loc="upper right", fontsize=8)
    return _save(fig, "compliance_by_ownership")


def fig_threshold_sensitivity(con):
    p = TABLES / "compliance_threshold_sensitivity.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    for i, (rule, sub) in enumerate(df.groupby("attribution_rule")):
        ax.plot(sub.threshold_min_score, sub.pct, marker="o", ms=5, lw=2,
                color=C[i], label=rule)
    ax.set_xlabel("Compliance threshold (minimum rubric elements met, of 10)")
    ax.set_ylabel("% of Ohio hospitals 'compliant'")
    ax.set_xticks(range(0, 11))
    ax.set_title("The headline number is a choice: compliance vs. threshold",
                 loc="left", fontsize=11, color=INK)
    ax.legend(fontsize=8, title="Attribution rule", title_fontsize=8)
    return _save(fig, "compliance_threshold_sensitivity")


def fig_within_hospital_distribution():
    p = TABLES / "within_hospital_dispersion.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    v = df["max_min_ratio"].dropna()
    if v.empty:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    xs = np.sort(v.values)
    ax.step(xs, np.arange(1, len(xs) + 1) / len(xs) * 100, where="post", lw=2, color=C[0])
    for q in (0.5, 0.9):
        val = float(np.quantile(xs, q))
        ax.axvline(val, color=MUTED, lw=1, ls="--")
        ax.text(val, 4, f" p{int(q*100)} = {val:.1f}x", fontsize=8, color=INK2)
    ax.set_xscale("log")
    ax.set_xlabel("Within-hospital max/min negotiated rate ratio across commercial payers (log)")
    ax.set_ylabel("Cumulative % of hospital-service pairs")
    ax.set_title(f"Within-hospital price dispersion (n={len(xs)} hospital-service pairs)",
                 loc="left", fontsize=11, color=INK)
    return _save(fig, "within_hospital_ratio_distribution")


def fig_commercial_to_medicare():
    p = TABLES / "hospital_service_rates.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    if "ratio_to_medicare" not in df or df.ratio_to_medicare.notna().sum() == 0:
        return None
    g = df.dropna(subset=["ratio_to_medicare"]).groupby("service_key")["ratio_to_medicare"]
    stats = g.agg(["median", "count",
                   lambda s: s.quantile(.10), lambda s: s.quantile(.90)])
    stats.columns = ["median", "n", "p10", "p90"]
    stats = stats.sort_values("median")
    stats.to_csv(TABLES / "fig_commercial_to_medicare.csv")
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    y = np.arange(len(stats))
    ax.hlines(y, stats.p10, stats.p90, color=MUTED, lw=2)
    ax.plot(stats["median"], y, "o", ms=8, color=C[0],
            markeredgecolor=SURFACE, markeredgewidth=2)
    for i, (idx, r) in enumerate(stats.iterrows()):
        ax.text(r.p90 * 1.03, i, f"{r['median']:.1f}x  (n={int(r['n'])})",
                va="center", fontsize=8, color=INK2)
    ax.axvline(1.0, color=INK2, lw=1)
    ax.set_yticks(y)
    ax.set_yticklabels(stats.index)
    ax.set_xlabel("Negotiated commercial rate ÷ Medicare rate (p10–p90, median)")
    ax.set_title("Commercial-to-Medicare ratio by service", loc="left",
                 fontsize=11, color=INK)
    return _save(fig, "commercial_to_medicare_by_service")


def fig_statewide_price_distribution():
    p = TABLES / "hospital_service_rates.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    svcs = [s for s in df.service_key.dropna().unique()]
    svcs = [s for s in svcs if df[df.service_key == s].rate_dollars.notna().sum() >= 5]
    if not svcs:
        return None
    n = len(svcs)
    ncol = min(3, n)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(3.1 * ncol, 2.5 * nrow), squeeze=False)
    for ax, s in zip(axes.flat, svcs):
        v = df[df.service_key == s].rate_dollars.dropna()
        ax.hist(v, bins=min(20, max(5, len(v) // 3)), color=C[0], edgecolor=SURFACE)
        ax.set_title(f"{s}  (n={len(v)})", fontsize=8.5, loc="left", color=INK)
        ax.tick_params(labelsize=7)
        ax.set_xlabel("negotiated $", fontsize=7)
    for ax in axes.flat[n:]:
        ax.axis("off")
    fig.suptitle("Statewide distribution of negotiated commercial rates, by service",
                 fontsize=11, x=0.01, ha="left", color=INK)
    return _save(fig, "statewide_price_distribution")


def fig_attrition(con):
    df = con.execute("""
        SELECT step, n FROM attrition
        WHERE step IN ('frame_facilities','domain_resolved','cms_hpt_present',
                       'cms_hpt_wellformed','mrf_url_found_any_method','downloaded_ok',
                       'parsed_structured') ORDER BY recorded_at
    """).df().drop_duplicates("step", keep="last")
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    y = np.arange(len(df))[::-1]
    bars = ax.barh(y, df.n, color=C[0], edgecolor=SURFACE, linewidth=2)
    for b, v in zip(bars, df.n):
        ax.text(b.get_width() + 2, b.get_y() + b.get_height() / 2, str(int(v)),
                va="center", fontsize=8, color=INK2)
    ax.set_yticks(y)
    ax.set_yticklabels(df.step)
    ax.set_xlabel("Hospitals remaining")
    ax.set_title("Attrition: the funnel is a finding, not an appendix",
                 loc="left", fontsize=11, color=INK)
    return _save(fig, "attrition_funnel")


def rubric_doc() -> str:
    lines = ["# Compliance rubric (frozen at prereg-v1)", "",
             "| Element | Requirement | Regulatory basis |", "|---|---|---|"]
    lines += [f"| `{k}` | {d} | {b} |" for k, d, b in ELEMENTS]
    return "\n".join(lines)


def run() -> dict:
    con = connect()
    out = {"tables": {}, "figures": []}
    out["tables"]["hospital_compliance.csv"] = hospital_compliance_csv(con)
    out["tables"]["price_basket.csv"] = price_basket_csv(con)
    out["tables"]["attrition_table.csv"] = attrition_csv(con)
    for fn in (lambda: fig_compliance_by_ownership(con),
               lambda: fig_threshold_sensitivity(con),
               lambda: fig_attrition(con),
               fig_within_hospital_distribution,
               fig_commercial_to_medicare,
               fig_statewide_price_distribution):
        try:
            r = fn()
            if r:
                out["figures"].append(r)
        except Exception as e:
            out.setdefault("figure_errors", []).append(f"{fn}: {e}")
    (TABLES / "rubric.md").write_text(rubric_doc())
    con.close()
    return out
