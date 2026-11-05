"""Tests for the logic that, if wrong, silently corrupts published numbers."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hospitalprice.basket import BASKET, normalize_code, PLACEBO_KEYS
from hospitalprice.parsers import is_anonymized_payer, sniff_format, FileProfile
from hospitalprice.parsers import cms_json, cms_csv
from hospitalprice.http import HashingReader
from hospitalprice.rubric import score as rubric_score, ELEMENT_KEYS
from hospitalprice.stages.discover import parse_cms_hpt, match_locations_to_ccns
from hospitalprice.stages.frame import categorize
from hospitalprice.stages.normalize import classify, load_rules
import hashlib, json

FIX = pathlib.Path(__file__).parent / "fixtures"
RULES = load_rules()


def rd(p):
    b = p.read_bytes()
    return HashingReader([b[i:i+4096] for i in range(0, len(b), 4096)]), b


# --- codes ------------------------------------------------------------------
def test_code_normalization():
    assert normalize_code("00470") == "470"
    assert normalize_code("MS-DRG 470") == "470"
    assert normalize_code("DRG-470") == "470"
    assert normalize_code("70551") == "70551"
    assert normalize_code(None) is None


def test_tka_inpatient_and_outpatient_are_distinct_services():
    keys = {s.key for s in BASKET if "tka" in s.key}
    assert keys == {"tka_op", "tka_ip"}
    ip = next(s for s in BASKET if s.key == "tka_ip")
    op = next(s for s in BASKET if s.key == "tka_op")
    assert ip.setting != op.setting and ip.code != op.code


def test_placebo_services_are_the_unambiguous_labs():
    assert PLACEBO_KEYS == {"bmp", "cbc_diff"}


# --- CCN categorization -----------------------------------------------------
def test_ccn_categorization():
    assert categorize("360001") == "short_term_acute"
    assert categorize("361301") == "critical_access"
    assert categorize("362001") == "ltch"
    assert categorize("363030") == "rehab"
    assert categorize("363301") == "childrens"
    assert categorize("364001") == "psychiatric"
    assert categorize("36S001") == "psych_unit"      # subunit, excluded
    assert categorize("36T001") == "rehab_unit"


# --- anonymized payers ------------------------------------------------------
def test_anonymized_payer_detection_is_conservative():
    for s in ["Payer 1", "PAYER_2", "Plan 3", "Commercial A", "payer name redacted", "", None]:
        assert is_anonymized_payer(s), s
    for s in ["Aetna Plan 3", "Anthem BCBS", "Payer Services of Ohio", "UnitedHealthcare"]:
        assert not is_anonymized_payer(s), s


# --- format sniffing --------------------------------------------------------
def test_sniff():
    assert sniff_format(b"%PDF-1.4 ...") == "pdf"
    assert sniff_format(b"<!DOCTYPE html><html>") == "html"
    assert sniff_format(b'{"hospital_name":"x"}') == "json"
    assert sniff_format(b"a,b,c\n1,2,3\n4,5,6\n") == "csv"


# --- parsers ----------------------------------------------------------------
def test_json_parser_extracts_and_profiles():
    r, b = rd(FIX / "cms_v2.json")
    p = FileProfile()
    recs = list(cms_json.parse(r, p))
    assert p.error is None and p.dialect == "cms_json_v2"
    assert p.has_gross and p.has_cash and p.has_min and p.has_max and p.has_code_type
    assert p.cms_schema_version == "2.0.0"
    assert p.anonymized_payers is True              # "Payer 3" present
    neg = [x for x in recs if x.rate_type == "negotiated"]
    assert {x.code for x in neg} == {"70551", "80048", "470"}   # 99999 excluded
    pct = [x for x in neg if x.rate_percent is not None]
    assert pct and pct[0].rate_dollars is None and pct[0].estimated_allowed_amount == 1440.0


def test_csv_tall_skips_metadata_preamble():
    r, b = rd(FIX / "cms_tall.csv")
    p = FileProfile()
    recs = list(cms_csv.parse(r, p))
    assert p.dialect == "cms_csv_tall" and p.error is None
    assert "TEST GENERAL HOSPITAL" in p.hospital_ids_in_file
    assert {x.code for x in recs if x.rate_type == "negotiated"} == {"70551", "85025"}


def test_csv_wide_preserves_verbatim_payer_case():
    """Prereg §5.1: raw payer strings are preserved verbatim."""
    r, b = rd(FIX / "cms_wide.csv")
    p = FileProfile()
    recs = list(cms_csv.parse(r, p))
    assert p.dialect == "cms_csv_wide"
    assert sorted(p.payer_names_seen) == ["Aetna", "Anthem BCBS"]


def test_zero_and_na_are_missing_not_zero():
    assert cms_json._f(0) is None
    assert cms_json._f("$0.00") is None
    assert cms_json._f("N/A") is None
    assert cms_json._f("") is None
    assert cms_json._f("1,234.50") == 1234.50


def test_hash_covers_whole_file_even_if_parser_stops_early():
    b = (FIX / "cms_v2.json").read_bytes()
    r = HashingReader([b[i:i+512] for i in range(0, len(b), 512)])
    r.read(100)          # stop early
    r.drain()
    assert r.sha256 == hashlib.sha256(b).hexdigest()


# --- cms-hpt.txt ------------------------------------------------------------
HPT = """location-name: Alpha Hospital
source-page-url: https://x.org/pt
mrf-url: https://x.org/123_alpha_standardcharges.json
contact-email: pt@x.org

location-name: Beta Medical Center
source-page-url: https://x.org/pt
mrf-url: https://x.org/-/media/456_beta.ashx
contact-email: pt@x.org
"""


def test_cms_hpt_parses_per_location_blocks():
    p = parse_cms_hpt(HPT)
    assert p["wellformed"] and p["uses_cms_keys"]
    assert len(p["locations"]) == 2
    assert p["locations"][1]["mrf_url"].endswith(".ashx")   # no extension filter


def test_cms_hpt_extensionless_and_ashx_urls_are_accepted():
    """A .json/.csv extension test would score real compliant hospitals
    non-compliant (Sitecore .ashx, Panacea endpoints)."""
    t = ("location-name: Gamma\nmrf-url: https://v.example/MRFDownload/gamma\n"
         "contact-email: a@b.org\n")
    p = parse_cms_hpt(t)
    assert p["wellformed"] and p["locations"][0]["mrf_url"].endswith("/gamma")


def test_location_matching_is_conservative():
    locs = parse_cms_hpt(HPT)["locations"]
    m = match_locations_to_ccns(locs, [("360001", "Alpha Hospital", None),
                                       ("360002", "Zeta Community Hospital", None)])
    assert "360001" in m and m["360001"]["mrf_url"].endswith("alpha_standardcharges.json")
    assert "360002" not in m           # unmatched stays unmatched, not assigned


# --- rubric -----------------------------------------------------------------
def _prof(**kw):
    p = FileProfile()
    p.is_machine_readable = True
    p.detected_format = "json"
    p.dialect = "cms_json_v2"
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_anonymized_payers_fail_r6_even_with_negotiated_rates():
    p = _prof(has_negotiated=True, payer_names_seen={"Payer 1", "Payer 2"},
              anonymized_payers=True)
    s = rubric_score(p, {"url_found": True, "cms_hpt_wellformed": True, "wall_type": "none"})
    assert s.elements["r6_named_payers"] is False
    assert s.material is False          # R6 is a necessary condition


def test_material_threshold_requires_all_four_necessary_elements():
    p = _prof(has_gross=True, has_cash=True, has_min=True, has_max=True,
              has_negotiated=True, payer_names_seen={"Aetna"},
              has_codes=True, has_code_type=False, cms_schema_version="2.0.0")
    s = rubric_score(p, {"url_found": True, "cms_hpt_wellformed": True, "wall_type": "none"})
    assert s.elements["r8_codes_typed"] is False
    assert s.material is False and s.score >= 7   # high score, still not material


def test_clickthrough_wall_fails_r10():
    p = _prof(has_negotiated=True, payer_names_seen={"Aetna"}, has_codes=True,
              has_code_type=True)
    s = rubric_score(p, {"url_found": True, "cms_hpt_wellformed": True,
                         "wall_type": "clickthrough"})
    assert s.elements["r10_no_wall"] is False


# --- payer normalization ----------------------------------------------------
def test_medicare_advantage_is_not_scored_as_commercial():
    """The ordering bug that silently pools MA rates into commercial dispersion."""
    c = classify("Anthem Blue Cross Blue Shield", "Medicare Advantage HMO", RULES)
    assert c["plan_type"] == "medicare_advantage"
    assert c["brand"] == "Anthem BCBS"


def test_medicaid_mco_is_separated():
    assert classify("CareSource", "Ohio Medicaid", RULES)["plan_type"] == "medicaid_mco"
    assert classify("Buckeye Health Plan", None, RULES)["plan_type"] == "medicaid_mco"


def test_commercial_brands_resolve_to_parent_entity():
    c = classify("ANTHEM_COMMERCIAL_PPO", None, RULES)
    assert c["parent_entity"] == "Elevance Health" and c["plan_type"] == "commercial"
    assert classify("Aetna Choice POS II", None, RULES)["parent_entity"] == "CVS Health"
    assert classify("Medical Mutual of Ohio", None, RULES)["brand"] == "Medical Mutual"


def test_unknown_payer_is_unresolved_not_guessed():
    c = classify("Zzz Regional Benefit Trust", None, RULES)
    assert c["confidence"] == "unresolved" and c["parent_entity"] is None


def test_anonymized_payer_is_unresolved_and_flagged():
    c = classify("Payer 1", None, RULES)
    assert c["anonymized"] is True and c["confidence"] == "unresolved"


def test_workers_comp_is_not_commercial():
    assert classify("Ohio BWC", None, RULES)["plan_type"] == "other"


def test_underscore_delimited_payer_strings_resolve():
    """Regression: `\\b` does not break on `_`, so `\\banthem\\b` failed to match
    ANTHEM_COMMERCIAL_PPO. Real MRFs are full of these; without normalizing the
    matching surface they all fell silently to 'unresolved'."""
    for s, parent, pt in [
        ("ANTHEM_COMMERCIAL_PPO", "Elevance Health", "commercial"),
        ("AETNA_MEDICARE_ADVANTAGE_HMO", "CVS Health", "medicare_advantage"),
        ("UHC/COMMUNITY_PLAN_MEDICAID", "UnitedHealth Group", "medicaid_mco"),
        ("CIGNA.PPO", "Cigna", "commercial"),
    ]:
        c = classify(s, None, RULES)
        assert c["parent_entity"] == parent and c["plan_type"] == pt, s
        assert c["confidence"] != "unresolved"


def test_same_brand_different_product_lines_stay_separate():
    """Normalization unit is (parent x product line), not the parent corporation:
    an Elevance commercial PPO and an Elevance MA HMO are different contracts."""
    a = classify("Anthem BCBS", "Commercial PPO", RULES)
    b = classify("Anthem BCBS", "Medicare Advantage HMO", RULES)
    assert a["parent_entity"] == b["parent_entity"]
    assert a["plan_type"] != b["plan_type"]


def test_utf8_bom_json_is_detected_as_json_not_csv():
    """A BOM is whitespace to a human and a byte to lstrip(). Without stripping
    it, real v3.0 JSON MRFs fell through to the CSV heuristic (JSON is full of
    commas), were parsed by the wrong parser, and yielded zero rows."""
    assert sniff_format(b'\xef\xbb\xbf{"hospital_name":"x"}') == "json"


def test_csv_with_quoted_commas_and_octet_stream_is_detected():
    """MRF headers carry quoted affirmation text containing commas, so
    delimiter counts vary across rows; requiring consistency rejected real
    multi-GB CSVs served as application/octet-stream."""
    body = b'a,b,c,d,e,f,"x, y, z"\n1,2,3,4,5,6,7\n'
    assert sniff_format(body, "application/octet-stream") == "csv"
