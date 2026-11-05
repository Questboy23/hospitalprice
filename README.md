# hospitalprice

Ohio hospital price transparency: a research pipeline producing (1) a compliance
dataset for 45 CFR 180 and (2) a negotiated-price dispersion analysis.

CLI only. Python 3.11. Not a web app.

## Two questions

1. **Compliance.** What fraction of Ohio hospitals comply with the federal
   Hospital Price Transparency rule, and how does compliance vary by ownership,
   system affiliation, size, and rurality?
2. **Dispersion.** Among compliant hospitals, how much does the negotiated price
   for an identical service vary across payers within a hospital, and across
   hospitals for the same service?

## Preregistration

`PREREGISTRATION.md` fixes the basket, the compliance rubric, the thresholds, and
the analysis specifications. It was committed and tagged **`prereg-v1` before any
price data was retrieved**:

```
git show prereg-v1 --stat
```

Amendments go in `docs/prereg_amendments.md` with a date, a reason, and a SHA.
Silent edits are not permitted.

## Install

```bash
uv venv --python 3.11 && uv pip install -e .
```

## Pipeline

```bash
hospitalprice frame                  # 1  sample frame from CMS  -> N=224
hospitalprice domains                # 1b hospital -> web domain (+ manual queue)
hospitalprice discover               # 2a cms-hpt.txt, then footer scan
hospitalprice fetch                  # 2b download, hash, stream-parse
hospitalprice score                  # 3  compliance rubric, both attribution rules
hospitalprice extract                # 4  assemble the preregistered basket
hospitalprice normalize              # 4b payer crosswalk + review queue
hospitalprice analyze                # 5  dispersion, placebo, regressions
hospitalprice export                 # 6  tables + figures + public CSV
hospitalprice status                 # attrition table at every stage
```

Human-in-the-loop steps, by design:

```bash
hospitalprice domains --load data/cache/domain_manual_queue.csv
hospitalprice normalize --load out/tables/payer_review_queue.csv --reviewer you
hospitalprice score --kappa          # manual vs automated agreement
hospitalprice medicare --csv benchmarks.csv --drg-weights ipps_table5.csv
```

## Outputs

| Path | What |
|---|---|
| `out/tables/hospital_compliance.csv` | one row per hospital per attribution rule, full rubric |
| `out/tables/price_basket.csv` | hospital × service × payer × rate, with provenance |
| `out/tables/attrition_table.csv` | N at every stage — a finding, not an appendix |
| `out/tables/compliance_threshold_sensitivity.csv` | compliance rate at every threshold 0–10 |
| `out/tables/payer_review_queue.csv` | human worklist; proposals are `DO_NOT_TRUST` |
| `out/tables/balance_disclosers_vs_nondisclosers.csv` | selection check |
| `out/figures/` | compliance by ownership, threshold sensitivity, attrition funnel, within-hospital ratio distribution, commercial-to-Medicare by service, statewide price distributions |
| `methods.md` | every exclusion and normalization decision, with reasons |

## Design commitments

- **Every number traces** to a file sha256, a byte offset, a parser dialect, a
  payer rule id, and a download date.
- **Missing is a result.** No price is ever imputed. A percent-of-charge with no
  dollar amount is missing, not zero.
- **Offered ≠ disclosed.** Absence from an MRF is never evidence a hospital does
  not provide a service.
- **Similarity proposes; it never merges.** No payer strings are joined by a
  fuzzy score. Every merge is a logged, inspectable, reversible decision.
- **Both attribution rules are reported.** Crediting twelve hospitals for one
  system file, or refusing to, is the largest lever on the headline number.
- **Falsification tests run in code**: the lab-vs-surgical placebo, the
  payer-specification fragility check, and Cohen's kappa on the rubric.

## Storage

Default is **stream-only**: files are hashed and parsed in flight and the raw
bytes discarded. Ohio MRFs total an estimated ~300 GB and this was built on a
machine with 4.3 GB free. `HOSPITALPRICE_ARCHIVE=1` enables full zstd retention
and refuses to run below 150 GB free. See `methods.md` §4 for what this costs.

## Crawler conduct

One request per domain per 5 seconds, never two concurrent transfers per host,
robots.txt obeyed, and an honest User-Agent with a working contact address. This
is academic research on legally mandated public disclosure and behaves that way.
The crawler refuses to start until you set a contact address. There is no default:

```bash
export HOSPITALPRICE_CONTACT="you@university.edu"
```
