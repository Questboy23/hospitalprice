# Methods

Every exclusion and normalization decision in this study, with its reason. The
preregistration (`PREREGISTRATION.md`, frozen at git tag `prereg-v1`) fixed the
basket, the rubric, the thresholds, and the analysis specifications before any
price data was retrieved. This file records what was actually done, including
where practice departed from the plan.

Global seed: **20260902**. Every external download is cached with its sha256 and
retrieval timestamp in the `download_log` table.

---

## 1. Sample frame

**Unit of observation: the CCN** (CMS Certification Number), not the hospital
brand and not the parent system.

**Sources.** Two CMS datasets, unioned:

| Source | Covers | Why needed |
|---|---|---|
| CMS Hospital Enrollments (PECOS) | all Part A hospital enrollments incl. rehab, LTCH, psych | the only source that includes IRF and LTCH |
| CMS Hospital General Information | acute, CAH, children's, psych | authoritative ownership and hospital type |

Hospital General Information **omits rehabilitation and long-term care hospitals
entirely**. A frame built from it alone would silently drop ~31 Ohio facilities
and inflate the compliance rate, because IRF/LTCH compliance is systematically
worse. This is the single most common frame error in this literature.

**Categorization is derived from the CCN facility-type range**, which is the
authoritative CMS categorization, rather than from the free-text `hospital_type`
field, which disagrees across the two datasets:

| CCN positions 3-6 | Category |
|---|---|
| 0001-0879 | short-term acute |
| 1300-1399 | critical access |
| 2000-2299 | long-term care (LTCH) |
| 3025-3099 | rehabilitation |
| 3300-3399 | children's |
| 4000-4499 | psychiatric |

**Realized frame: N = 224.** (Preregistered expectation was ~220; within the
±15% tolerance declared in prereg §2, so no investigation was triggered.)

| Category | N |
|---|---|
| short-term acute | 119 |
| critical access | 34 |
| psychiatric | 32 |
| rehabilitation | 17 |
| LTCH | 14 |
| children's | 8 |
| **total** | **224** |

### Documented exclusions

1. **Subunit CCNs excluded (100 CCNs).** A CCN with a letter in position 3
   (`S` psychiatric unit, `T`/`R` rehabilitation unit, `U` swing bed) denotes a
   distinct-part unit *inside* a parent hospital. These are not separately
   licensed facilities, do not publish their own MRF, and are covered by the
   parent's file. Counting them would double-count parent hospitals and deflate
   the compliance rate. Recorded in the attrition table, not silently dropped.

2. **Federal facilities flagged, not removed.** VA and DoD hospitals are outside
   the definition of "hospital" at 45 CFR 180.20 and have no negotiated payer
   rates to publish. They are retained in the frame with `ownership='federal'`
   and reported separately, so the reader can see the denominator both ways.

3. **Rehab / LTCH / psychiatric excluded from Q2 only.** These facilities do not
   perform colonoscopies, deliveries, or knee replacements. The exclusion was
   declared in advance (prereg §2) precisely so that a missing colonoscopy price
   at a rehabilitation hospital is never scored as a disclosure failure. They
   remain fully in the Q1 compliance analysis.

---

## 2. Domain resolution (Stage 1b)

**No CMS dataset contains a hospital website.** This is a genuine gap in the
standard pipeline and is the first place hospitals are silently lost, so it is
run as its own stage with its own N and its own confidence coding.

Method: a hand-built, version-controlled map of Ohio health systems to domains
(`stages/domains.py`), matched against hospital name and DBA name. Everything
unmatched is written to `data/cache/domain_manual_queue.csv` for human
resolution and is marked `unresolved` until a person fills it in. Automatic
resolutions are coded `probable`; manual ones `certain`.

**One domain covers many hospitals.** This is not incidental in Ohio, where
roughly a dozen systems hold most acute beds. Observed:

| Domain | CCNs |
|---|---|
| mercy.com | 17 |
| uhhospitals.org | 13 |
| ohiohealth.com | 13 |
| clevelandclinic.org | 12 |
| selectspecialtyhospitals.com | 10 |

`shared_with_n` is stored on every row because it drives the Stage 3 attribution
question (§4 below).

---

## 3. MRF discovery (Stage 2a)

Order: `cms-hpt.txt` -> footer/known-path scan -> manual queue. Every attempt is
recorded even when it fails, because *how* a file was found is itself a finding:
a hospital reachable only by hand-searching a footer has not met the same
standard as one with a valid pointer file.

### cms-hpt.txt

Required at the domain root by the CY2024 OPPS final rule. CMS specifies the
keys `location-name`, `source-page-url`, `mrf-url`, `contact-name`,
`contact-email`, and **a system publishes one repeating block per hospital**.

Two implementation decisions that materially change the results:

1. **Parse blocks, not URLs.** Scraping URLs out of the file yields a domain-level
   answer. Parsing the per-location blocks lets a *specific* MRF be attributed to
   a *specific* CCN by name match, which is what makes the conservative
   attribution rule possible at all. Observed block counts: mercy.com 23,
   ohiohealth.com 16, uhhospitals.org 14, aultman.org 3.

2. **Never require a file extension.** An early version tested MRF URLs for
   `.json`/`.csv`/`.xml`. Real files are served from Sitecore `.ashx` handlers
   (mercy.com) and extensionless vendor endpoints (nationwidechildrens.org via
   Panacea). The extension test scored both of those hospitals non-compliant when
   both are in fact compliant. **Any pipeline that filters on file extension will
   understate compliance**, and the error falls hardest on hospitals using
   third-party vendors.

**Well-formed** is scored on substance, not layout: the file must name at least
one machine-readable file and give a contact. Formatting variation is not
penalized.

### Name matching

cms-hpt.txt location names are matched to CCNs by normalized token overlap
(stopwords such as *hospital*, *medical*, *center*, *health*, *regional* removed)
with a minimum Jaccard score of 0.34. **Deliberately conservative**: an unmatched
CCN stays unmatched rather than being assigned the system's first file, because a
wrong attribution manufactures both a compliance credit and a set of prices
belonging to a different hospital.

---

## 4. Acquisition (Stage 2b) and the storage constraint

**Declared limitation.** The build machine had **4.3 GB free** against an
estimated 300 GB of raw Ohio MRFs (p90 ~500 GB; the distribution is severely
right-skewed, with a handful of academic medical center files at 5-40 GB each).
Full archival was impossible.

The pipeline therefore defaults to **stream-only**: each file is streamed from
the socket through the parser, hashed in flight, and the raw bytes discarded.
Retained per extracted price row: the sha256 of the **complete** file, the byte
offset, the parser dialect, and a 400-character evidence excerpt of the source
record.

Consequences, stated plainly:

- A third party can verify any number against a re-download **only if the
  hospital has not changed its file**. Hospitals revise MRFs frequently.
- Files whose hash changes between runs are flagged, never silently updated.
- `--archive` (env `HOSPITALPRICE_ARCHIVE=1`) enables full zstd retention and
  refuses to run below 150 GB free.

The hash covers the whole file even when the parser stops early (`reader.drain()`),
so a sha256 in this dataset is never a partial-file hash masquerading as the
file's identity.

**Download, structural profiling, and basket extraction happen in one pass.**
This fuses Stages 2-4 for files that cannot be re-read. Scoring never
re-downloads.

**Politeness.** One request initiation per domain per 5 seconds; never two
concurrent transfers against one host; robots.txt fetched and obeyed; the crawler
identifies itself with a project name and a working contact address. System
domains are fetched once and shared across their CCNs rather than once per CCN.

---

## 5. Compliance scoring (Stage 3)

Ten binary elements (see `out/tables/rubric.md`), thresholds preregistered.

### Anonymized payers

A file listing "Payer 1", "Payer 2" scores **R6 = 0**. Anonymization is treated
as an evasion of 180.50(b)(1)(iii), not a formatting defect, and is counted in
its own column so the reader can see how much of non-compliance it represents.

Matching is conservative: only labels that are *entirely* a generic word plus an
index or a redaction marker. "Aetna Plan 3" does not match.

### Dual attribution - reported both ways

Whether the twelve hospitals on one system domain are each credited for one
system file is **the single largest lever on the headline number**. Reporting one
rule without the other would be a choice disguised as a measurement, so both are
computed for every hospital:

- `named_in_file` - credited only where the file or cms-hpt.txt names the
  hospital. Conservative; risks understating compliance.
- `domain_credit` - credited if the domain serves a compliant file. Permissive;
  risks overstating it.

`out/tables/compliance_threshold_sensitivity.csv` and the corresponding figure
give the compliance rate at every threshold 0-10 under both rules.

### Validation

25 hospitals (seed 20260902) are hand-scored blind to the automated result and
Cohen's kappa reported (`hospitalprice score --kappa`). Per prereg §4/§10, kappa
< 0.80 means the automated rubric is reported as measuring parser robustness
rather than compliance. It is not tuned until it agrees.

---

## 6. Price extraction (Stage 4)

Service identity is **(code, code_type, setting)**. Two conflations are
structurally prevented:

1. **Setting is never pooled.** CPT 27447 appears as an outpatient facility rate,
   an inpatient component, and a professional fee at wildly different dollars.
   TKA outpatient (27447) and TKA inpatient (DRG 470) are separate preregistered
   basket entries and are never merged. Pooling across setting manufactures
   dispersion out of nothing and is, in my assessment, the most likely route to a
   large and wrong published number.

2. **Offered vs. disclosed is never inferred from silence.** Three states are
   recorded and never collapsed: `not_disclosed` (usable MRF, service absent),
   `not_offered` (external evidence the hospital does not provide it), and
   `undeterminable`. The `not_offered` determination comes only from external
   evidence — currently the CCN provider category. Absence from an MRF is never
   itself evidence that a service is not offered. Cells without external evidence
   stay `undeterminable` rather than being guessed.

**Non-prices are missing, never zero.** A percent-of-charge with no dollar
amount, a `$0`, an `N/A`, or a placeholder is coded missing. The count of
percent-only rows is reported, as is the count that carry a CY2025-required
`estimated_amount` — that ratio is a finding in its own right about whether the
CY2025 OPPS encoding requirement actually took effect.

Companion codes 45380/45385 (diagnostic colonoscopy) are recorded where present
but analyzed separately and **never substituted** for screening 45378.

---

## 7. Payer normalization (Stage 4b)

Normalization unit: **(parent entity × product line × state)**. Not the parent
corporation: an Elevance commercial PPO and an Elevance Medicare Advantage HMO
are different contracts at different prices, and rolling them up manufactures
dispersion. Network variants within a product line are treated as within-payer
variation and are not collapsed into the payer identity.

Rules:

- Raw payer strings are preserved **verbatim** with file hash and byte offset,
  and never overwritten. (The wide-CSV parser was corrected during development
  because it was lowercasing payer names taken from the column header.)
- Merges come from an ordered, version-controlled regex ruleset
  (`payers/rules.yaml`). The matching `rule_id` is stamped on every row.
- **Similarity proposes; it never merges.** No score threshold auto-merges under
  any circumstance. `rapidfuzz` output populates a human worklist
  (`out/tables/payer_review_queue.csv`) whose proposal column is explicitly
  labelled `DO_NOT_TRUST`.
- Every decision is appended to `payers/decisions.jsonl` with proposer score,
  verdict, reviewer, and timestamp.
- Manual review covers the **top 300 payer strings by row count UNION the top
  300 by hospital count** — a payer that is small everywhere but present at many
  hospitals is exactly the kind that matters for cross-hospital comparison.
  Everything below is `unresolved` and **excluded from the primary analysis,
  not merged**.

**Ordering matters.** Medicare Advantage and Medicaid patterns are evaluated
*before* commercial brand rules, so "Anthem Medicare Advantage HMO" is not
silently scored as a commercial Anthem rate. Where an MA/Medicaid rule fires, the
brand is still recorded from the brand rule, but the plan type wins.

### The four anonymization cases, kept distinct

| Case | Example | Treatment |
|---|---|---|
| (a) truly anonymized | "Payer 1" | R6=0. Excluded from named-payer analysis, but **retained** for within-hospital dispersion, which is computable without knowing who the payers are |
| (b) coded but decodable | contract IDs with an in-file legend | decode attempted, decode logged |
| (c) payer named, plan not | "Aetna" at 12 different rates | `plan_type=unknown`, excluded from the commercial-only specification |
| (d) named, but not a dollar figure | percent-of-charge, `$0` | missing, never zero |

Case (c) is more dangerous than (a) and is the more common error: a hospital
listing "Aetna" at $400 and $1,900 for one CPT is almost certainly commercial vs.
Medicaid managed care, and including both inflates the within-hospital ratio
enormously.

### Plan-type screen

Rate-level checks **flag for review and never reclassify**: a "commercial" row
below 100% of that hospital's Medicare rate, or a "Medicaid" row above 200% of
it, is queued. The number of flagged rows is reported.

---

## 8. Analysis (Stage 5)

All Q2 headline quantities are produced at three payer specifications:
`certain`, `certain+probable` (primary), `all`. The **fragility test is executed
in code**, not left to the writer: if the median within-hospital max/min ratio
moves more than 10% across specifications, the output states that the point
estimate must be withdrawn and an interval reported.

**Placebo test.** Median within-hospital dispersion for the unambiguous
single-line labs (80048, 85025) must be materially lower than for the surgical
services. If it is not, the pipeline is measuring parse noise and the dispersion
results are reported as invalid. Declared in advance; reported whichever way it
comes out.

Within-hospital dispersion collapses to one rate per (hospital, service, parent
payer) **before** computing spread, so a payer listed under six plan names cannot
inflate the ratio by counting one contract repeatedly.

### Regressions - two non-nested specifications

`log(rate) ~ ... + market_HHI + hospital fixed effects` **is not estimable**:
hospital FE absorb any hospital-level covariate, HHI included. This was an error
in the original study design and is corrected here by reporting two columns:

- **B1** `log(rate) ~ payer + hospital FE + service FE` — identifies
  across-payer variation *within* a hospital. Hospital-level covariates are
  absent **by construction, not by oversight**.
- **B2** `log(rate) ~ HHI + ownership + beds + service FE`, no hospital FE —
  identifies cross-hospital variation.

Neither is interpreted causally. Both are descriptive associations on a sample
selected on disclosure.

**Clustering.** Standard errors are clustered by hospital system. Because roughly
a dozen systems hold most Ohio acute beds, the effective number of clusters is
~30-40 rather than 224; the cluster count is reported alongside every
specification so the reader can discount accordingly.

**HHI is a proxy.** Computed at CBSA level from staffed-bed shares, with
hospitals in one system treated as a single competitor. The literature standard
is discharge-weighted patient-flow HHI, which requires Ohio discharge data that
is not publicly available. Labelled as a proxy in every table that uses it.

**Selection.** `hospitalprice analyze --balance` produces the required
disclosers-vs-non-disclosers balance table. The commercial-to-Medicare ratio
computed on compliant hospitals is not Ohio's ratio, and statewide quantities are
reported as bounds rather than point estimates. Bounding is not imputation: no
individual missing price is ever filled in.

---

## 9. Known limitations

1. **Stream-only storage** weakens independent replication if hospitals change
   their files (§4).
2. **HHI is bed-share, not discharge-weighted** (§8).
3. **`not_offered` evidence is currently only the provider category.** A general
   hospital without obstetrics is `undeterminable`, not `not_offered`, until
   service-line data is loaded. This is conservative in the right direction: it
   never converts a disclosure failure into an exemption.
4. **Domain resolution is partly manual** and its coverage is reported as an
   attrition step rather than assumed complete.
5. **Medicare benchmarks are pinned to specific CMS files** (§10). CMS reposts
   these annually, so no URL is hardcoded in the pipeline; the cached source
   files and their hashes are the citation.


---

## 10. Medicare benchmarks

The commercial-to-Medicare ratio is the standard unit in this literature, so the
benchmark has to be the payment system that **actually pays for that service at a
hospital**. Using one blanket fee schedule silently rescales every ratio in the
paper.

Built by `scripts/build_medicare_benchmarks.py` from CMS source files cached in
`data/cache/`, with each emitted row carrying the sha256 of the file it came from.

| Service class | Payment system | Source |
|---|---|---|
| hospital outpatient facility services | OPPS APC payment rate | CY2026 July Addendum B |
| clinical laboratory tests | Clinical Lab Fee Schedule | CY2026 Q3 CLFS PUF |
| inpatient stays | IPPS base rate × MS-DRG weight | FY2026 IPPS Final Rule Tables 5 and 1A/1D |

**Labs are not an OPPS service.** OPPS Addendum B lists 80048 and 85025 with
status indicator **Q4 and no payment rate** — conditionally packaged laboratory
tests, paid under the CLFS when billed alone. Benchmarking them against an OPPS
rate that does not exist, or against the Physician Fee Schedule, would be wrong
in both directions. This is exactly the kind of detail that separates a ratio
that is comparable to published work from one that is not.

**IPPS base rate** = operating labor $4,456.72 + operating nonlabor $2,295.89
(Table 1A, quality-reporting and meaningful-EHR-user, wage index > 1) + capital
$524.15 (Table 1D) = **$7,276.76**, multiplied by the Table 5 relative weight.
This is a *national, unadjusted* rate: it excludes the hospital-specific wage
index, IME, DSH, and outlier payments. It is therefore a benchmark, not what
Medicare actually paid any particular Ohio hospital, and is labelled as such.

Resulting benchmarks (FY/CY2026):

| Code | Service | Setting | System | Medicare |
|---|---|---|---|---|
| 70551 | MRI brain w/o contrast | OP | OPPS (Q3) | $243.77 |
| 74177 | CT abd/pelvis w/ contrast | OP | OPPS (Q3) | $356.43 |
| 45378 | Screening colonoscopy | OP | OPPS (T) | $950.10 |
| 66984 | Cataract surgery | OP | OPPS (J1) | $2,357.81 |
| 99283 | ED visit level 3 | OP | OPPS (J2) | $278.89 |
| 27447 | TKA (outpatient) | OP | OPPS (J1) | $13,116.76 |
| 80048 | Basic metabolic panel | OP | CLFS | $8.46 |
| 85025 | CBC with differential | OP | CLFS | $7.77 |
| 470 | TKA (inpatient) | IP | IPPS | $14,036.14 |
| 788 | Cesarean delivery | IP | IPPS | $6,976.96 |
| 807 | Vaginal delivery | IP | IPPS | $4,905.99 |

Note the two TKA entries, $13,116.76 outpatient and $14,036.14 inpatient. They
are close but not equal, and they are different products. Pooling the commercial
rates for 27447 and DRG 470 against a single benchmark would blend two payment
systems and manufacture spread; the preregistration keeps them separate.
