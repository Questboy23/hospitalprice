"""CMS standardized JSON template (v2.x) and generic JSON MRFs.

ONE streaming pass produces both the Stage 3 structural profile and the Stage 4
basket price records. A second download of a 10GB file is not affordable, so
scoring and extraction are deliberately fused.
"""
from __future__ import annotations
import json
import ijson

from . import FileProfile, PriceRecord, is_anonymized_payer
from ..basket import ALL_CODES, BY_CODE, normalize_code

MONEY_KEYS = {
    "gross_charge": "gross", "grosscharge": "gross",
    "discounted_cash": "cash", "discounted_cash_price": "cash",
    "minimum": "min", "maximum": "max",
}
METHODOLOGY_MAP = {
    "fee schedule": "fee_schedule", "case rate": "case_rate", "per diem": "per_diem",
    "percent of total billed charges": "percent_of_charge", "other": "other",
}


def _f(v):
    """Coerce to a dollar figure. A blank, 'N/A', or 0 placeholder is NOT a
    price -- it is missing. Returning 0.0 here would silently fabricate data."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s or s.upper() in {"N/A", "NA", "NULL", "-", "TBD", "VARIES"}:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f > 0 else None


def _methodology(s):
    if not s:
        return None
    return METHODOLOGY_MAP.get(str(s).strip().lower(), str(s).strip().lower()[:40])


def parse(reader, profile: FileProfile, want_codes=ALL_CODES, collect_prices=True):
    """Generator of PriceRecord. Mutates `profile` in place as structure is seen."""
    profile.dialect = "cms_json_v2"
    profile.detected_format = "json"
    profile.is_machine_readable = True

    # Header fields arrive before standard_charge_information; ijson's kvitems on
    # the root lets us catch them without buffering the array.
    try:
        events = ijson.parse(reader, use_float=True)
        cur_item: dict | None = None
        path_stack: list[str] = []
        builder = None

        for prefix, event, value in events:
            if prefix == "version" and event == "string":
                profile.cms_schema_version = value
            elif prefix in ("hospital_name", "hospital_location.item",
                            "location_name.item", "location_name") and event == "string":
                # v2.x uses hospital_location; v3.0 renamed it location_name.
                profile.hospital_ids_in_file.add(str(value)[:200])
            elif prefix.endswith("license_information.license_number") and event in ("string", "number"):
                profile.hospital_ids_in_file.add(f"license:{value}")
            elif prefix in ("hospital_address.item",) and event == "string":
                pass
            elif prefix == "standard_charge_information.item" and event == "start_map":
                builder = ijson.ObjectBuilder()
                builder.event(event, value)
            elif builder is not None and prefix.startswith("standard_charge_information.item"):
                builder.event(event, value)
                if prefix == "standard_charge_information.item" and event == "end_map":
                    item = builder.value
                    builder = None
                    profile.rows_scanned += 1
                    yield from _emit_item(item, profile, reader, want_codes, collect_prices)
    except Exception as e:
        profile.error = f"{type(e).__name__}: {e}"
    finally:
        profile.bytes_scanned = getattr(reader, "pos", 0)


def _emit_item(item: dict, profile: FileProfile, reader, want_codes, collect_prices):
    codes = []
    for ci in (item.get("code_information") or []):
        c = normalize_code(ci.get("code"))
        t = (ci.get("type") or "").strip().upper() or None
        if c:
            codes.append((c, t))
            profile.has_codes = True
            if t:
                profile.has_code_type = True

    for sc in (item.get("standard_charges") or []):
        setting = (sc.get("setting") or "").strip().lower() or None
        billing_class = (sc.get("billing_class") or "").strip().lower() or None
        gross = _f(sc.get("gross_charge"))
        cash = _f(sc.get("discounted_cash"))
        mn, mx = _f(sc.get("minimum")), _f(sc.get("maximum"))
        if gross is not None: profile.has_gross = True
        if cash is not None: profile.has_cash = True
        if mn is not None: profile.has_min = True
        if mx is not None: profile.has_max = True

        payers = sc.get("payers_information") or []
        for p in payers:
            pname = p.get("payer_name")
            profile.has_negotiated = True
            if pname:
                profile.payer_names_seen.add(str(pname)[:120])
                if is_anonymized_payer(str(pname)):
                    profile.anonymized_payers = True
            else:
                profile.anonymized_payers = True

        if not collect_prices:
            continue
        matched = [(c, t) for (c, t) in codes if c in want_codes]
        if not matched:
            continue
        off = getattr(reader, "pos", 0)
        excerpt = json.dumps(item)[:400]
        for code, ctype in matched:
            base = dict(code=code, code_type=ctype, setting=setting,
                        billing_class=billing_class, byte_offset=off,
                        evidence_excerpt=excerpt)
            if gross is not None:
                yield PriceRecord(payer_string_raw=None, plan_string_raw=None,
                                  rate_type="gross", rate_dollars=gross, **base)
            if cash is not None:
                yield PriceRecord(payer_string_raw=None, plan_string_raw=None,
                                  rate_type="cash", rate_dollars=cash, **base)
            if mn is not None:
                yield PriceRecord(payer_string_raw=None, plan_string_raw=None,
                                  rate_type="min", rate_dollars=mn, **base)
            if mx is not None:
                yield PriceRecord(payer_string_raw=None, plan_string_raw=None,
                                  rate_type="max", rate_dollars=mx, **base)
            for p in payers:
                yield PriceRecord(
                    payer_string_raw=(str(p.get("payer_name")) if p.get("payer_name") else None),
                    plan_string_raw=(str(p.get("plan_name")) if p.get("plan_name") else None),
                    rate_type="negotiated",
                    rate_dollars=_f(p.get("standard_charge_dollar")),
                    rate_percent=_f(p.get("standard_charge_percentage")),
                    # CY2025 OPPS requires a dollar estimate where the rate is a
                    # percentage or algorithm. Whether hospitals actually supply
                    # it is an empirical question this field lets us answer.
                    estimated_allowed_amount=_f(p.get("estimated_amount")),
                    methodology=_methodology(p.get("standard_charge_methodology")
                                             or p.get("methodology")),
                    **base)
