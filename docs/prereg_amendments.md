# Preregistration amendments

The preregistration is frozen at tag `prereg-v1`. Changes are recorded here with
a date, a reason, and a SHA. Silent edits to `PREREGISTRATION.md` are not
permitted.

| # | Date | Section | Change | Reason |
|---|---|---|---|---|
| 1 | 2026-09-02 | §7 | Primary within-hospital dispersion restricted to `methodology='fee_schedule'` | See A1 |
| 2 | 2026-09-02 | §6 | Added a plausibility screen flagging negotiated rates >100× or <0.05× Medicare | See A2 |
| 3 | 2026-09-02 | §3/§10 | The preregistered placebo test is reported as **mis-specified**, not as passed | See A3 |

---

## A1 — Stratify by rate methodology

**Change.** The primary within-hospital dispersion specification now compares
`fee_schedule` rates only. The unrestricted result is reported alongside.

**Reason.** A case rate, a per-diem, a percent-of-charge and a fee-schedule
amount for the *same* CPT are different contract objects. Comparing them
produces spread that is an artifact of contract structure, not a difference in
the price of a service. Measured within a single hospital for CPT 80048
(Medicare $8.46):

| Methodology | n | min | max |
|---|---|---|---|
| percent_of_charge | 925 | $5.51 | $250.70 |
| fee_schedule | 735 | $1.00 | $8,399.00 |
| other | 213 | $125.82 | $63,065.48 |
| case_rate | 22 | $0.62 | $32,610.00 |

Restricting to fee-schedule moves the median lab max/min ratio from 5.28× to
3.60×. The original design treated methodology as a descriptive field; it is
part of the contract's identity and belongs in the service key.

## A2 — Plausibility screen (flag, never delete)

**Change.** Negotiated rates above 100× or below 0.05× the Medicare rate for the
same (code, code_type, setting) are flagged and excluded from the primary
specification. They are counted and reported, never deleted, and the unscreened
result is reported alongside.

**Reason.** Hospitals encode outlier/stop-loss thresholds, case rates and bundle
prices on the same row as the CPT. Observed verbatim in a Mercy Health file, for
a row described as *"HC Basic Metabolic Panel (Calcium Total)"*:

```
...,CASE-80048,LOCAL,80048,CPT,0301,RC,...,63065.48,,
APC Price ($63,065.48). If cost exceeds the threshold, the outlier payment is
added to the expected reimbursement...
```

$63,065.48 is a threshold, not the price of a blood test. Extracting the number
is correct; treating it as a unit price is not. The screen removes 505 rows of
119,948 (0.4%).

## A3 — The placebo test as preregistered is mis-specified

**This amendment does not rescue the test. It records that the test failed and
that the test itself was wrong.**

Preregistration §3 declared: dispersion for the unambiguous single-line labs
(80048, 85025) must be materially *lower* than for surgical services, otherwise
the extraction is generating noise. Result at every specification:

| Specification | lab median ratio | surgical median ratio | verdict |
|---|---|---|---|
| all methodologies, unscreened | 5.85× | 1.80× | FAIL |
| all methodologies, screened | 5.28× | 1.78× | FAIL |
| fee-schedule only, screened (primary) | 3.60× | 1.36× | FAIL |

**Diagnosis.** The test conflates two different things:

1. *Extraction ambiguity* — labs genuinely have less of this, which is what the
   test was designed to detect.
2. *Economic dispersion* — cheap ancillary services genuinely have wider
   **proportional** variation than expensive procedures. A lab priced $10 at one
   payer and $40 at another is a 4× ratio and $30 of money; a knee replacement
   at $20,000 and $36,000 is a 1.8× ratio and $16,000 of money. Labs are
   routinely carved out, priced off small fee schedules or percentages, and
   receive far less negotiating attention per unit.

A ratio-based test cannot separate these. Methodology stratification (A1)
removed roughly a third of the gap, which is consistent with part of the
original failure being an artifact and part being real economics.

**Consequences, adopted:**

- The dispersion results for the **low-dollar services** (`bmp`, `cbc_diff`) are
  reported as **not validated** and are excluded from headline claims.
- Headline dispersion is reported for the **high-dollar services only**, where
  the ratio measure is stable and the proportional-variation confound is small.
- The placebo is **not** re-specified post hoc to something that passes. A
  replacement diagnostic is proposed for a future preregistration: compare the
  same (hospital, service, payer) rate extracted independently by two different
  parser dialects, which isolates extraction noise without any economic content.

**What was not done.** The threshold was not moved, the lab codes were not
swapped for services that would pass, and the test was not deleted. It is
reported as failed.


## Amendment 2026-11-05: contact address moved out of the document

**What changed.** The contact line in `PREREGISTRATION.md` no longer prints a
personal email address. The crawler now reads its contact address only from the
`HOSPITALPRICE_CONTACT` environment variable and refuses to send a request when
it is unset. The earlier code default was removed.

**Why.** The address is personal and should not ship in the repository.

**Effect on the study.** None. No research question, measure, threshold or
analysis step changed. The crawler still identifies itself with a working
contact address on every request.
