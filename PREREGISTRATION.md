# Preregistration — Ohio Hospital Price Transparency Study

**Status: FROZEN.** Committed and git-tagged before any price data was retrieved,
parsed, or inspected. Any change after the tag must be recorded in
`docs/prereg_amendments.md` with a date, a reason, and the git SHA at which it was
made. Amendments are permitted; silent edits are not.

- Preregistered: 2026-09-02
- Frozen at tag: `prereg-v1`
- Authors: R. (Denison University)
- Contact: set at run time through the `HOSPITALPRICE_CONTACT` environment variable (see amendment of 2026-11-05)

---

## 1. Research questions

**Q1 (Compliance).** What fraction of Ohio hospitals comply with 45 CFR 180, and how
does compliance vary with ownership type, system affiliation, bed count, and
urban/rural status?

**Q2 (Dispersion).** Among hospitals with usable disclosures, how much does the
negotiated price for an identical service vary (a) across payers within a single
hospital, and (b) across hospitals for the same service?

These are reported as two papers. Q1 is self-contained and does not depend on any
result from Q2.

## 2. Sample frame

**Universe.** All Medicare-certified hospital facilities with a service address in
Ohio, as of the CMS Provider of Services (POS) file and the CMS Hospital General
Information dataset, both downloaded and hashed on the frame build date.

**Included provider categories** (no exclusions at frame stage):
short-term acute care, critical access, children's, psychiatric, rehabilitation,
long-term care hospitals (LTCH).

**Unit of observation is the CCN** (CMS Certification Number), not the hospital
brand name and not the parent system. Facilities sharing a campus but holding
distinct CCNs are distinct observations.

**Expected N ≈ 220.** The realized N and the exact inclusion query are recorded in
`out/tables/attrition_table.csv` and `methods.md`. If realized N departs from 220
by more than ±15%, the discrepancy is investigated and documented before proceeding.

**Preregistered exclusions for Q2 only** (Q1 uses the full frame): psychiatric,
rehabilitation, and LTCH facilities are excluded from dispersion analysis because
the basket services are not within their scope of practice. This exclusion is
declared here, in advance, precisely so that it cannot later be mistaken for a
disclosure failure. See §6 on the offer/disclose distinction.

## 3. Preregistered basket

Frozen before any price data. Services are keyed on **(code, code_type, setting)**.
Setting is part of the service identity and rates are never pooled across settings.

| # | Service | Code(s) | Type | Setting |
|---|---|---|---|---|
| 1 | MRI brain without contrast | 70551 | CPT | outpatient |
| 2 | CT abdomen/pelvis with contrast | 74177 | CPT | outpatient |
| 3 | Screening colonoscopy | 45378 | CPT | outpatient |
| 4 | Basic metabolic panel | 80048 | CPT | outpatient |
| 5 | CBC with differential | 85025 | CPT | outpatient |
| 6 | Vaginal delivery | 807 | MS-DRG | inpatient |
| 7 | Cesarean delivery | 788 | MS-DRG | inpatient |
| 8 | Total knee arthroplasty | 27447 | CPT | outpatient |
| 9 | Total knee arthroplasty | 470 | MS-DRG | inpatient |
| 10 | Cataract surgery | 66984 | CPT | outpatient |
| 11 | ED visit, level 3 | 99283 | CPT | outpatient |

**Codes 8 and 9 are distinct observations and are never merged.** TKA migrated to
the outpatient-eligible list; the inpatient DRG rate and the outpatient CPT rate are
different products at different prices.

**Preregistered placebo.** Services 4 and 5 (BMP, CBC) are single-line, unambiguous
laboratory tests. Measured dispersion for these must be materially lower than for
services 8-10. If it is not, the extraction pipeline is generating noise rather than
measuring prices, and the dispersion results are reported as invalid. This test is
declared in advance and will be reported whichever way it comes out.

**Not collected as substitutes.** 45380/45385 (diagnostic colonoscopy) are recorded
where present but analyzed separately and never substituted for 45378.

## 4. Compliance rubric (Q1)

Ten binary elements, derived from 45 CFR 180.50 and the CY2024/CY2025 OPPS final
rules. Each element scores 0 or 1. Elements are weighted equally; the weighting is
itself a choice and the sensitivity to it is reported.

| # | Element | Regulatory basis |
|---|---|---|
| R1 | An MRF exists and is retrievable over HTTP without authentication | 180.50(a) |
| R2 | `cms-hpt.txt` present at the domain root and well-formed | CY2024 OPPS |
| R3 | Format is machine-readable (JSON/CSV/XML); not PDF, not an HTML table | 180.50(c) |
| R4 | Contains gross charges | 180.50(b)(1)(i) |
| R5 | Contains discounted cash prices | 180.50(b)(1)(ii) |
| R6 | Contains payer-specific negotiated charges **with named payers** | 180.50(b)(1)(iii) |
| R7 | Contains de-identified minimum and maximum negotiated charges | 180.50(b)(1)(iv)(v) |
| R8 | Billing codes present with the code type identified | 180.50(b)(2) |
| R9 | Conforms to the CMS standardized schema/template | CY2024 OPPS |
| R10 | Not behind a login, captcha, click-through agreement, or JS-only wall | 180.50(d)(3) |

**R6 is scored 0 where payer labels are anonymized** ("Payer 1", "Commercial A").
Anonymized labels are treated as a distinct, separately-counted evasion category,
not as a mere formatting defect.

### Preregistered thresholds

The headline number is a function of where the line is drawn, so three thresholds
are declared in advance and **all three are reported in the abstract**:

- **Strict** — all 10 elements. "Fully compliant."
- **Material (primary)** — R1 ∧ R3 ∧ R6 ∧ R8, plus ≥ 7 of 10 total.
  Rationale: a file that cannot be retrieved, cannot be machine-read, does not name
  payers, or does not identify billing codes is unusable for the statute's purpose
  regardless of what else it contains. These four are necessary conditions.
- **Lenient** — R1 ∧ R3. "A machine-readable file exists."

The full score distribution is published, so any reader may impose their own
threshold. A sensitivity curve (compliance rate vs. threshold, 0-10) is a required
figure.

### Preregistered validation

A random sample of **25 hospitals** (seed 20260902) is scored by hand, blind to the
automated score. Cohen's κ between manual and automated scoring is reported. **If
κ < 0.80 the automated rubric is treated as measuring parser robustness rather than
compliance**, and the finding is reported as such rather than being tuned until it
agrees.

## 5. Payer normalization (Q2)

Normalization unit: **(parent entity × product line × state)**. Network variants
within a product line are within-payer variation, reported separately, never
collapsed into the payer identity.

Rules:

1. Raw payer strings are preserved verbatim with file hash and byte offset. The raw
   string is never overwritten.
2. Merges are made by an ordered, version-controlled regex ruleset
   (`src/hospitalprice/payers/rules.yaml`). The matching rule ID is stamped on every
   row.
3. **String similarity proposes candidates for human review. It never merges.** No
   similarity threshold auto-merges under any circumstance.
4. Every decision is appended to `payers/decisions.jsonl` with proposer score,
   verdict, reviewer, and timestamp.
5. Every decision carries confidence ∈ {certain, probable, ambiguous}.
6. **Manual review covers the top 300 payer strings** by row count and by hospital
   count. Strings below that are coded `unresolved` and **excluded** from the
   primary analysis rather than merged.

**All Q2 results are reported at three normalization specifications:**
certain-only / certain+probable (primary) / all-including-ambiguous.

**Preregistered fragility test.** If the headline within-hospital dispersion ratio
moves by more than 10% across those three specifications, payer normalization is
reported as the binding source of uncertainty and the dispersion estimate is
reported as an interval, not a point.

### Plan type

Commercial, Medicare Advantage, and Medicaid managed care are analyzed **separately
and never pooled**. Rows whose plan type cannot be determined from the payer string
are coded `unknown` and excluded from the commercial-only primary specification.

Rate-level screens **flag for review; they never reclassify automatically**:
a "commercial" row below 100% of the hospital's Medicare rate, or a "Medicaid" row
above 200% of it, is queued for manual inspection. The count of flagged rows is
reported.

## 6. Missingness — declared in advance

- **No missing price is ever imputed.** Missing is a result and is reported as one.
- Three states are recorded and never collapsed:
  - `not_disclosed` — hospital has a usable MRF; this service is absent from it.
  - `not_offered` — hospital does not provide this service (established from POS
    provider category and CMS utilization/service-line data, not inferred from the
    MRF's silence).
  - `undeterminable` — cannot distinguish the above two.
- The `not_offered` determination is made from **external evidence only**. Absence
  from an MRF is never itself treated as evidence that a service is not offered.
- Non-dollar "rates" (percent-of-charge with no dollar amount, `$0`, `N/A`,
  placeholder) are **not prices** and are coded missing, not zero.

## 7. Analysis specifications

### Dispersion
Within-hospital, per (hospital × service × plan type): max/min ratio across payers,
coefficient of variation, interquartile ratio. Reported as distributions across
hospitals, not as single averages.

Across-hospital, per service: full distribution of negotiated rates statewide,
normalized to the Medicare rate for that (code, setting) to yield a
commercial-to-Medicare ratio.

### Regressions

**Two specifications, reported as separate columns. They answer different questions
and are not nested.**

- **(A) Compliance.**
  `compliance_score ~ ownership + log(beds) + system_affiliation + rural + market_HHI`
  OLS. SEs clustered by hospital system. Because Ohio has ~12 systems holding the
  majority of acute beds, the effective number of clusters is ~30-40; **wild cluster
  bootstrap p-values are reported alongside asymptotic SEs.**

- **(B1) Within-hospital price.**
  `log(rate) ~ payer_market_share + hospital FE + service FE`
  Identifies across-payer variation within a hospital. **Hospital-level covariates,
  including HHI, are collinear with hospital FE and are therefore absent from B1
  by construction — not omitted by oversight.**

- **(B2) Across-hospital price.**
  `log(rate) ~ hospital_HHI + ownership + system_size + log(beds) + service FE`
  No hospital FE. SEs clustered by system.

**Neither B1 nor B2 is interpreted causally.** They are descriptive associations on
a selected sample.

### Market concentration
HHI computed at CBSA level from staffed-bed shares (HCRIS). **This is a proxy.** The
literature standard is discharge-weighted patient-flow HHI, which requires Ohio
discharge data that is not publicly available. The proxy is labeled as such in every
table and its limitations are stated in `methods.md`.

## 8. Selection — declared in advance

Q2 is estimated on hospitals that chose to comply. This is not a random sample. A
balance table comparing disclosers to non-disclosers on bed count, ownership,
system affiliation, rurality, HHI, and case mix is a **required** output and is
reported whether or not it is favorable.

Statewide quantities are reported as **bounds**, not point estimates, using
best/worst-case assumptions for non-disclosers derived from HCRIS aggregates.
Bounding is not imputation: no individual missing price is ever filled in.

## 9. Reproducibility

- Global seed: **20260902**.
- Every external download is cached with its sha256 and retrieval timestamp.
- Every extracted price row carries: source file sha256, byte offset, parser
  dialect ID, payer rule ID, and download date.
- **Disk constraint (declared).** The build machine has < 5 GB free against an
  estimated 300 GB of raw MRFs. The pipeline therefore defaults to **stream-only**:
  files are hashed and parsed in flight and the raw bytes discarded. Retained per
  extracted row is the sha256, the byte offset, and a bounded evidence excerpt of
  the source record. This is a real and acknowledged reduction in reproducibility
  relative to a full archive: a third party can verify our extraction against a
  re-download only if the hospital has not changed the file. Files whose hash
  changes between runs are flagged, not silently updated. `--archive` enables full
  retention where disk permits.

## 10. What would falsify or invalidate this study

Declared in advance so that these cannot later be reframed as limitations:

1. Placebo failure — lab dispersion ≈ surgical dispersion (§3) ⇒ extraction invalid.
2. κ < 0.80 on manual rubric validation (§4) ⇒ compliance score invalid as a
   compliance measure.
3. Headline dispersion moves > 10% across payer specs (§5) ⇒ point estimate
   withdrawn, interval reported.
4. Fewer than 40 hospitals with usable commercial rates for a given service ⇒ that
   service is reported descriptively only, with no regression.
