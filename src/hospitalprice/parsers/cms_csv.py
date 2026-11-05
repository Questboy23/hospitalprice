"""CMS standardized CSV template (tall and wide) plus generic CSV MRFs.

The CMS CSV template puts hospital metadata in rows 1-2 and the real column
header in row 3, so a naive csv.DictReader silently mislabels every column.
Both layouts are handled, and which one was detected is recorded as the dialect
so that any downstream number can be traced to the parse path that produced it.
"""
from __future__ import annotations
import csv
import io
import re

from . import FileProfile, PriceRecord, is_anonymized_payer
from ..basket import ALL_CODES, normalize_code
from .cms_json import _f, _methodology

csv.field_size_limit(10_000_000)

CODE_RE = re.compile(r"^code\s*\|\s*(\d+)$", re.I)
CODE_TYPE_RE = re.compile(r"^code\s*\|\s*(\d+)\s*\|\s*type$", re.I)
# wide: standard_charge|<payer>|<plan>|negotiated_dollar
WIDE_RE = re.compile(r"^standard_charge\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*"
                     r"(negotiated_dollar|negotiated_percentage|negotiated_algorithm|"
                     r"estimated_amount|methodology)$", re.I)


def _norm(h: str) -> str:
    return (h or "").strip().lower()


def _find_header(rows: list[list[str]]) -> int:
    """CMS template: metadata rows precede the real header. Pick the first row
    that actually looks like a data header."""
    for i, r in enumerate(rows[:6]):
        low = [_norm(c) for c in r]
        if "description" in low and any(CODE_RE.match(c) or c in
                                        ("code|1", "billing_class", "setting") for c in low):
            return i
        if "description" in low and any(c.startswith("standard_charge") for c in low):
            return i
    return 0


def parse(reader, profile: FileProfile, want_codes=ALL_CODES, collect_prices=True):
    profile.detected_format = "csv"
    profile.is_machine_readable = True
    text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace", newline="")
    try:
        sample_rows: list[list[str]] = []
        r = csv.reader(text)
        for _ in range(6):
            try:
                sample_rows.append(next(r))
            except StopIteration:
                break
        if not sample_rows:
            profile.error = "empty_csv"
            return
        hdr_i = _find_header(sample_rows)
        header = [c.strip() for c in sample_rows[hdr_i]]
        low = [_norm(c) for c in header]

        # CMS template metadata row (row 2) names the hospital -> attribution.
        if hdr_i >= 2 and len(sample_rows) > 1:
            meta_hdr = [_norm(c) for c in sample_rows[0]]
            for k, v in zip(meta_hdr, sample_rows[1]):
                if k in ("hospital_name", "hospital_location") and v:
                    profile.hospital_ids_in_file.add(str(v)[:200])
                if k.startswith("license_number") and v:
                    profile.hospital_ids_in_file.add(f"license:{v}")
                if k == "version" and v:
                    profile.cms_schema_version = v

        code_cols = {}   # idx -> ordinal
        type_cols = {}
        wide_cols = []   # (idx, payer, plan, field)
        for i, c in enumerate(low):
            if (m := CODE_RE.match(c)):
                code_cols[i] = m.group(1)
            elif (m := CODE_TYPE_RE.match(c)):
                type_cols[i] = m.group(1)
            elif (m := WIDE_RE.match(header[i])):
                # payer/plan captured from the ORIGINAL header, not the
                # lowercased one: prereg §5.1 requires verbatim payer strings.
                wide_cols.append((i, m.group(1), m.group(2), m.group(3).lower()))

        idx = {c: i for i, c in enumerate(low)}
        tall = "payer_name" in idx
        profile.dialect = ("cms_csv_wide" if wide_cols else
                           "cms_csv_tall" if tall else "generic_csv")
        if code_cols:
            profile.has_codes = True
        if type_cols:
            profile.has_code_type = True

        def col(row, name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        # remaining sample rows below the header are real data
        pending = sample_rows[hdr_i + 1:]
        for row in _chain(pending, r):
            profile.rows_scanned += 1
            gross = _f(col(row, "standard_charge|gross"))
            cash = _f(col(row, "standard_charge|discounted_cash"))
            mn = _f(col(row, "standard_charge|min"))
            mx = _f(col(row, "standard_charge|max"))
            if gross is not None: profile.has_gross = True
            if cash is not None: profile.has_cash = True
            if mn is not None: profile.has_min = True
            if mx is not None: profile.has_max = True
            setting = (col(row, "setting") or "").strip().lower() or None
            billing_class = (col(row, "billing_class") or "").strip().lower() or None

            codes = []
            for i, ordv in code_cols.items():
                c = normalize_code(row[i] if i < len(row) else None)
                t = None
                for j, o2 in type_cols.items():
                    if o2 == ordv and j < len(row):
                        t = (row[j] or "").strip().upper() or None
                if c:
                    codes.append((c, t))

            if tall:
                pn = col(row, "payer_name")
                if pn is not None:
                    profile.has_negotiated = True
                    if pn.strip():
                        profile.payer_names_seen.add(pn.strip()[:120])
                    if is_anonymized_payer(pn):
                        profile.anonymized_payers = True
            for (_i, payer, _plan, _fld) in wide_cols[:200]:
                profile.has_negotiated = True
                profile.payer_names_seen.add(payer[:120])
                if is_anonymized_payer(payer):
                    profile.anonymized_payers = True

            if not collect_prices:
                continue
            matched = [(c, t) for (c, t) in codes if c in want_codes]
            if not matched:
                continue
            off = getattr(reader, "pos", 0)
            excerpt = ",".join(str(x) for x in row)[:400]
            for code, ctype in matched:
                base = dict(code=code, code_type=ctype, setting=setting,
                            billing_class=billing_class, byte_offset=off,
                            evidence_excerpt=excerpt)
                for val, rt in ((gross, "gross"), (cash, "cash"), (mn, "min"), (mx, "max")):
                    if val is not None:
                        yield PriceRecord(payer_string_raw=None, plan_string_raw=None,
                                          rate_type=rt, rate_dollars=val, **base)
                if tall:
                    yield PriceRecord(
                        payer_string_raw=(col(row, "payer_name") or None),
                        plan_string_raw=(col(row, "plan_name") or None),
                        rate_type="negotiated",
                        rate_dollars=_f(col(row, "standard_charge|negotiated_dollar")),
                        rate_percent=_f(col(row, "standard_charge|negotiated_percentage")),
                        estimated_allowed_amount=_f(col(row, "estimated_amount")),
                        methodology=_methodology(col(row, "standard_charge|methodology")),
                        **base)
                else:
                    acc: dict[tuple[str, str], dict] = {}
                    for i, payer, plan, fld in wide_cols:
                        v = row[i] if i < len(row) else None
                        if v is None or not str(v).strip():
                            continue
                        acc.setdefault((payer, plan), {})[fld] = v
                    for (payer, plan), d in acc.items():
                        yield PriceRecord(
                            payer_string_raw=payer, plan_string_raw=plan,
                            rate_type="negotiated",
                            rate_dollars=_f(d.get("negotiated_dollar")),
                            rate_percent=_f(d.get("negotiated_percentage")),
                            estimated_allowed_amount=_f(d.get("estimated_amount")),
                            methodology=_methodology(d.get("methodology")),
                            **base)
    except Exception as e:
        profile.error = f"{type(e).__name__}: {e}"
    finally:
        profile.bytes_scanned = getattr(reader, "pos", 0)


def _chain(pending, rdr):
    yield from pending
    yield from rdr
