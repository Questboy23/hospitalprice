"""Stage 4b - payer normalization. The hard part.

Design commitments, all preregistered:
  - the raw payer string is preserved verbatim and never overwritten
  - merges come from an ordered, version-controlled regex ruleset; the matching
    rule id is stamped on every row so any merge is inspectable
  - string similarity PROPOSES candidates for human review and NEVER merges
  - strings outside the top-300 review set are 'unresolved' and EXCLUDED from
    the primary analysis rather than quietly merged
  - every decision carries confidence; all results are reported at three
    specifications (certain / certain+probable / all)

Bad payer normalization invalidates the entire dispersion result, so the point
of this module is auditability, not throughput.
"""
from __future__ import annotations
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from ..config import PAYERS_DIR, TABLES
from ..db import connect, record_attrition
from ..parsers import is_anonymized_payer

RULES_PATH = PAYERS_DIR / "rules.yaml"
DECISIONS_PATH = PAYERS_DIR / "decisions.jsonl"
REVIEW_TOP_N = 300      # preregistered manual review depth
PROPOSE_THRESHOLD = 88  # similarity at which a candidate is SHOWN to a human


def load_rules() -> list[dict]:
    doc = yaml.safe_load(RULES_PATH.read_text())
    rules = doc.get("rules", [])
    for r in rules:
        r["_rx"] = re.compile(r["pattern"])
    return rules


PLAN_HINTS = [
    (re.compile(r"(?i)\b(medicare\s*advantage|\bMA\b|medicare\s*(hmo|ppo)|senior)\b"), "medicare_advantage"),
    (re.compile(r"(?i)\bmedicaid|managed\s*medicaid|\bMMC\b|community\s*plan\b"), "medicaid_mco"),
    (re.compile(r"(?i)\b(commercial|ppo|hmo|epo|pos|exchange|marketplace|individual|group)\b"), "commercial"),
]


def _matchable(s: str) -> str:
    """Underscores and slashes are WORD characters, so `\b` does not break on
    them: `\banthem\b` does not match "ANTHEM_COMMERCIAL_PPO". Real MRFs are
    full of underscore-delimited payer strings, and without this every one of
    them falls through to 'unresolved' -- a silent, large, and invisible loss.
    The raw string is untouched; only the matching surface is normalized."""
    return re.sub(r"[_/\\|.+]+", " ", s or "")


def classify(raw: str, plan: str | None, rules: list[dict]) -> dict:
    """Apply the ordered ruleset. First match wins; the rule id is recorded."""
    combined = _matchable(f"{raw or ''} {plan or ''}").strip()
    if is_anonymized_payer(raw):
        return {"parent_entity": None, "brand": None, "product_line": None,
                "plan_type": "unknown", "network": None, "anonymized": True,
                "rule_id": "anonymized", "confidence": "unresolved", "resolved_by": "rule"}

    plan_type = None
    for rx, pt in PLAN_HINTS:
        if rx.search(combined):
            plan_type = pt
            break

    for r in rules:
        if r["_rx"].search(combined):
            # A brand rule must not override an explicit MA/Medicaid signal:
            # "Anthem Medicare Advantage" is an MA rate, not a commercial one.
            pt = r.get("plan_type")
            if r["id"] in ("ma_generic", "medicaid_generic"):
                brand_rule = next((x for x in rules
                                   if x["id"] not in ("ma_generic", "medicaid_generic")
                                   and x["_rx"].search(combined)), None)
                return {"parent_entity": (brand_rule or {}).get("parent_entity"),
                        "brand": (brand_rule or {}).get("brand"),
                        "product_line": pt, "plan_type": pt, "network": None,
                        "anonymized": False, "rule_id": r["id"],
                        "confidence": r.get("confidence", "probable"), "resolved_by": "rule"}
            if plan_type in ("medicare_advantage", "medicaid_mco"):
                pt = plan_type
            return {"parent_entity": r.get("parent_entity"), "brand": r.get("brand"),
                    "product_line": pt, "plan_type": pt or "unknown", "network": None,
                    "anonymized": False, "rule_id": r["id"],
                    "confidence": r.get("confidence", "probable"), "resolved_by": "rule"}

    return {"parent_entity": None, "brand": None, "product_line": None,
            "plan_type": plan_type or "unknown", "network": None, "anonymized": False,
            "rule_id": None, "confidence": "unresolved", "resolved_by": "rule"}


def build() -> dict:
    con = connect()
    rules = load_rules()
    now = datetime.now(timezone.utc)

    con.execute("DELETE FROM payer_string")
    con.execute("DELETE FROM payer_review_queue")

    strings = con.execute(
        "SELECT payer_string_raw, any_value(plan_string_raw), count(*) n, "
        "count(DISTINCT ccn) h FROM price_raw "
        "WHERE payer_string_raw IS NOT NULL AND rate_type='negotiated' "
        "GROUP BY 1 ORDER BY n DESC").fetchall()

    # Preregistered review depth: top 300 by row count UNION top 300 by hospital
    # count. A payer that is small everywhere but present at many hospitals is
    # exactly the kind that matters for cross-hospital comparison.
    by_rows = [s[0] for s in strings[:REVIEW_TOP_N]]
    by_hosp = [s[0] for s in sorted(strings, key=lambda r: -r[3])[:REVIEW_TOP_N]]
    in_review = set(by_rows) | set(by_hosp)

    manual = _load_decisions()
    resolved = anon = unresolved = 0
    for raw, plan, n, h in strings:
        c = classify(raw, plan, rules)
        if raw in manual:
            c.update(manual[raw]); c["resolved_by"] = "manual"
        if raw not in in_review and c["confidence"] == "unresolved":
            c["confidence"] = "unresolved"      # explicit: excluded, not merged
        con.execute("INSERT OR REPLACE INTO payer_string VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [raw, n, h, c["parent_entity"], c["brand"], c["product_line"],
                     c["plan_type"], c["network"], c["anonymized"], c["rule_id"],
                     c["confidence"], c["resolved_by"]])
        if c["anonymized"]:
            anon += 1
        elif c["confidence"] == "unresolved":
            unresolved += 1
        else:
            resolved += 1

    _propose(con, strings, in_review, now)

    n_rows = con.execute("SELECT count(*) FROM price_raw WHERE rate_type='negotiated'").fetchone()[0]
    cover = con.execute(
        "SELECT coalesce(sum(n_rows),0) FROM payer_string WHERE confidence IN ('certain','probable')"
    ).fetchone()[0]
    record_attrition(con, "4b_normalize", "distinct_payer_strings", len(strings), None, "")
    record_attrition(con, "4b_normalize", "payer_strings_resolved", resolved, len(strings), "")
    record_attrition(con, "4b_normalize", "payer_strings_anonymized", anon, len(strings),
                     "prereg §5: distinct evasion category, R6=0, retained for "
                     "within-hospital dispersion only")
    record_attrition(con, "4b_normalize", "payer_strings_unresolved", unresolved, len(strings),
                     "EXCLUDED from primary analysis, not merged")
    out = {"distinct_strings": len(strings), "resolved": resolved,
           "anonymized": anon, "unresolved": unresolved,
           "negotiated_rows": n_rows, "rows_covered_by_resolved": cover,
           "row_coverage_pct": round(100.0 * cover / n_rows, 2) if n_rows else None,
           "review_queue": str(TABLES / "payer_review_queue.csv")}
    con.close()
    return out


def _propose(con, strings, in_review, now) -> None:
    """Similarity proposes; it never merges. Output is a human worklist."""
    known = con.execute(
        "SELECT DISTINCT brand FROM payer_string WHERE brand IS NOT NULL").fetchall()
    known = [k[0] for k in known]
    rows = []
    for raw, _plan, n, h in strings:
        c = con.execute("SELECT confidence, anonymized FROM payer_string WHERE payer_string_raw=?",
                        [raw]).fetchone()
        if not c or c[0] != "unresolved" or c[1]:
            continue
        if raw not in in_review:
            continue
        best = process.extractOne(raw, known, scorer=fuzz.token_set_ratio) if known else None
        cand, sc = (best[0], best[1]) if best else (None, 0.0)
        reason = ("similar to a known brand" if sc >= PROPOSE_THRESHOLD
                  else "no rule matched; needs a hand-written rule")
        rows.append([raw, cand if sc >= PROPOSE_THRESHOLD else None, float(sc), n, h, reason])
        con.execute("INSERT INTO payer_review_queue VALUES (?,?,?,?,?,?,?,?)",
                    [raw, rows[-1][1], float(sc), n, h, reason, "pending", now])
    with open(TABLES / "payer_review_queue.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["payer_string_raw", "proposed_brand_DO_NOT_TRUST", "similarity_score",
                    "n_rows", "n_hospitals", "reason",
                    "decision_parent_entity", "decision_brand", "decision_plan_type",
                    "decision_confidence(certain|probable|ambiguous)", "reviewer", "note"])
        for r in rows:
            w.writerow(r + ["", "", "", "", "", ""])


def _load_decisions() -> dict:
    """Append-only decision log. Every merge a human made, with who and when."""
    out: dict[str, dict] = {}
    if not DECISIONS_PATH.exists():
        return out
    for line in DECISIONS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out[d["payer_string_raw"]] = {
            "parent_entity": d.get("parent_entity"), "brand": d.get("brand"),
            "product_line": d.get("plan_type"), "plan_type": d.get("plan_type", "unknown"),
            "network": d.get("network"), "anonymized": False,
            "rule_id": f"manual:{d.get('reviewer','?')}",
            "confidence": d.get("confidence", "probable")}
    return out


def load_reviewed(path: str, reviewer: str) -> int:
    """Ingest a completed review CSV into the append-only decision log."""
    n = 0
    now = datetime.now(timezone.utc).isoformat()
    with open(path, newline="") as f, open(DECISIONS_PATH, "a") as out:
        for row in csv.DictReader(f):
            conf = (row.get("decision_confidence(certain|probable|ambiguous)") or "").strip()
            if not conf:
                continue
            rec = {"payer_string_raw": row["payer_string_raw"],
                   "parent_entity": (row.get("decision_parent_entity") or "").strip() or None,
                   "brand": (row.get("decision_brand") or "").strip() or None,
                   "plan_type": (row.get("decision_plan_type") or "unknown").strip(),
                   "confidence": conf,
                   "proposer_score": row.get("similarity_score"),
                   "reviewer": reviewer, "decided_at": now,
                   "note": row.get("note", "")}
            out.write(json.dumps(rec) + "\n")
            n += 1
    return n


def flag_plan_types() -> dict:
    """Rate-level screen. FLAGS for review; never reclassifies automatically.

    A 'commercial' rate below Medicare, or a 'Medicaid' rate far above it, is
    usually a mislabelled product -- and silently including a Medicaid MCO rate
    among commercial rates is the single most likely way to manufacture a large
    within-hospital dispersion number.
    """
    con = connect()
    con.execute("DELETE FROM plan_type_flag")
    con.execute("""
        INSERT INTO plan_type_flag
        SELECT p.payer_string_raw, p.ccn, p.service_key, ps.plan_type,
               p.rate_dollars, m.national_rate,
               p.rate_dollars / nullif(m.national_rate,0),
               CASE WHEN ps.plan_type='commercial'
                      AND p.rate_dollars < m.national_rate THEN 'commercial_below_medicare'
                    WHEN ps.plan_type='medicaid_mco'
                      AND p.rate_dollars > 2*m.national_rate THEN 'medicaid_above_2x_medicare'
               END,
               'pending'
        FROM price_raw p
        JOIN payer_string ps USING(payer_string_raw)
        JOIN medicare_rate m ON m.code=p.code AND m.code_type=p.code_type
                            AND m.setting=p.setting
        WHERE p.rate_type='negotiated' AND p.rate_dollars IS NOT NULL
          AND m.national_rate IS NOT NULL
          AND ((ps.plan_type='commercial' AND p.rate_dollars < m.national_rate)
            OR (ps.plan_type='medicaid_mco' AND p.rate_dollars > 2*m.national_rate))
    """)
    n = con.execute("SELECT count(*) FROM plan_type_flag").fetchone()[0]
    tot = con.execute("SELECT count(*) FROM price_raw WHERE rate_type='negotiated'").fetchone()[0]
    record_attrition(con, "4b_normalize", "plan_type_rows_flagged", n, tot,
                     "flagged for human review; NOT reclassified automatically")
    con.close()
    return {"flagged_rows": n, "negotiated_rows": tot}
