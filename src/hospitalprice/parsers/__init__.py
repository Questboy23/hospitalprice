"""MRF dialect detection and streaming extraction.

Every parser here is a GENERATOR over a byte stream. Nothing loads a file into
memory: some Ohio MRFs exceed 10GB uncompressed and the build machine has under
5GB of disk.

Each yielded record carries its own provenance (byte offset + evidence excerpt)
so that a price can be traced back to a file hash and a location within it.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class PriceRecord:
    code: str | None
    code_type: str | None
    setting: str | None
    payer_string_raw: str | None
    plan_string_raw: str | None
    rate_type: str            # negotiated|gross|cash|min|max
    rate_dollars: float | None
    rate_percent: float | None = None
    estimated_allowed_amount: float | None = None
    methodology: str | None = None
    billing_class: str | None = None
    byte_offset: int = 0
    evidence_excerpt: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class FileProfile:
    """Structural facts about a file, gathered in one streaming pass. Feeds the
    Stage 3 rubric -- so scoring never requires re-downloading."""
    dialect: str = "unknown"
    is_machine_readable: bool = False
    detected_format: str | None = None       # json|csv|xml|pdf|html|unknown
    has_gross: bool = False
    has_cash: bool = False
    has_negotiated: bool = False
    has_min: bool = False
    has_max: bool = False
    has_codes: bool = False
    has_code_type: bool = False
    cms_schema_version: str | None = None
    conforms_cms_schema: bool = False
    payer_names_seen: set = field(default_factory=set)
    anonymized_payers: bool = False
    hospital_ids_in_file: set = field(default_factory=set)   # CCN/EIN -> attribution
    rows_scanned: int = 0
    bytes_scanned: int = 0
    error: str | None = None


MAGIC = [
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\x1f\x8b", "gzip"),
]

# Anonymized payer labels are a distinct evasion category, not a formatting
# defect. Matched conservatively: only labels that are *entirely* a generic word
# plus an index. "Aetna Plan 3" must NOT match.
import re
ANON_PATTERNS = [
    re.compile(r"^\s*(payer|payor|plan|carrier|insurer|commercial|contract|network)"
               r"[\s_\-#]*([0-9]{1,3}|[a-z])\s*$", re.I),
    re.compile(r"^\s*(payer|payor|plan|carrier)[\s_\-]*(name)?[\s_\-]*(redacted|withheld|"
               r"confidential|n/?a|unknown|other|various|misc)\s*$", re.I),
    re.compile(r"^\s*[a-z]\s*$", re.I),
]


def is_anonymized_payer(s: str | None) -> bool:
    if not s or not s.strip():
        return True
    return any(p.match(s) for p in ANON_PATTERNS)


def sniff_format(head: bytes, content_type: str | None = None) -> str:
    """Detect format from magic bytes, structure, and the server's content-type.

    The window must be large: a wide-format CSV header carries one column per
    payer-plan pair and routinely exceeds 64KB on a single line. Sniffing the
    first 4KB of such a file sees ONE partial line, concludes 'unknown', and
    silently scores a large compliant hospital as not-machine-readable.
    """
    for magic, fmt in MAGIC:
        if head.startswith(magic):
            return fmt
    # A UTF-8 BOM is whitespace to a human and a byte to lstrip(). Without
    # stripping it, `{...}` does not look like JSON, the file falls through to
    # the CSV heuristic (JSON is full of commas), and a large compliant hospital
    # is parsed with the wrong parser and yields zero rows.
    t = head.lstrip(b"\xef\xbb\xbf").lstrip()
    low = t[:512].lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<body" in low:
        return "html"
    if t.startswith(b"<?xml") or (t.startswith(b"<") and b"<html" not in low):
        return "xml"
    if t.startswith(b"{") or t.startswith(b"["):
        return "json"
    # CSV heuristic: delimiter-consistent first lines
    try:
        lines = t.decode("utf-8", "replace").splitlines()[:8]
    except Exception:
        return "unknown"
    for delim in (",", "|", "\t", ";"):
        counts = [ln.count(delim) for ln in lines if ln.strip()]
        if not counts:
            continue
        if len(counts) >= 2 and counts[0] >= 2 and len(set(counts)) <= 2:
            return "csv"
        # Delimiter counts legitimately vary across rows because MRF headers
        # carry quoted affirmation text containing commas, and a wide-format
        # header line can exceed the sniff window entirely. Requiring
        # consistency rejects real CSVs; a delimiter-dense first line is the
        # reliable signal.
        if counts[0] >= 5:
            return "csv"
    ct = (content_type or "").lower()
    if "csv" in ct or "excel" in ct or "text/plain" in ct:
        return "csv"
    if "json" in ct:
        return "json"
    if "xml" in ct:
        return "xml"
    return "unknown"
