"""Stage 2b - acquisition.

Downloading, structural profiling, and basket extraction happen in ONE pass.
This is a deliberate design choice, not a shortcut: some Ohio MRFs exceed 10GB
and the build machine has under 5GB of disk, so a file is seen exactly once.
Stage 3 scores from the profile recorded here; Stage 4 reads price_raw.

Under the default stream-only policy the raw bytes are discarded after passing
through the parser. What survives is the sha256 of the full file, the byte
offset of each extracted record, and a bounded evidence excerpt -- enough to
re-verify a number against a re-download, which is the honest limit of what
this disk allows. See PREREGISTRATION §9.
"""
from __future__ import annotations
import io
from datetime import datetime, timezone

from ..basket import ALL_CODES
from ..config import EVIDENCE_EXCERPT_CHARS
from ..db import connect, record_attrition
from ..http import open_stream
from ..parsers import FileProfile, sniff_format
from ..parsers import cms_csv, cms_json

# Formats that are not machine-readable are still profiled and scored (R3=0);
# they are a compliance finding, not a fetch failure.
NON_MACHINE = {"pdf", "html"}


def _decompress(peek, kind: str):
    """Wrap a compressed stream so the parser sees plaintext. Streaming, so a
    10GB .json.gz never lands on a disk with 4GB free."""
    import zlib, io as _io
    if kind == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif kind == "zip":
        # single-entry stored/deflated zip: skip the local file header
        head = peek.read(30)
        if len(head) < 30 or head[:4] != b"PK\x03\x04":
            return None
        import struct
        method = struct.unpack("<H", head[8:10])[0]
        nlen, elen = struct.unpack("<HH", head[26:30])
        peek.read(nlen + elen)
        if method == 0:
            return _Peek(_Raw(peek))
        if method != 8:
            return None
        d = zlib.decompressobj(-zlib.MAX_WBITS)
    else:
        return None

    def gen():
        while True:
            chunk = peek.read(1 << 20)
            if not chunk:
                tail = d.flush()
                if tail:
                    yield tail
                return
            out = d.decompress(chunk)
            if out:
                yield out

    return _Peek(_Iter(gen(), peek))


class _Iter(io.RawIOBase):
    """File-like over a byte generator, reporting the UNDERLYING compressed
    position so byte offsets still point into the real file."""

    def __init__(self, it, under):
        super().__init__()
        self._it, self._buf, self._eof, self._under = it, b"", False, under

    @property
    def pos(self):
        return self._under.pos

    def read(self, n=-1):
        while (n < 0 or len(self._buf) < n) and not self._eof:
            try:
                self._buf += next(self._it)
            except StopIteration:
                self._eof = True
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def readinto(self, b):
        d = self.read(len(b)); b[:len(d)] = d; return len(d)

    def readable(self):
        return True


class _Raw(_Iter):
    def __init__(self, under):
        super().__init__(iter(lambda: under.read(1 << 20), b""), under)

    def writable(self):
        return False


class _Peek(io.RawIOBase):
    """Lets us sniff the first bytes without losing them, and without buffering
    the file. Wraps the HashingReader transparently."""

    def __init__(self, reader, n=65536):
        super().__init__()
        self._r = reader
        self._head = reader.read(n)
        self._used = False

    @property
    def head(self):
        return self._head

    @property
    def pos(self):
        return self._r.pos

    def read(self, n=-1):
        if not self._used:
            self._used = True
            if n is None or n < 0:
                return self._head + self._r.read(-1)
            if n <= len(self._head):
                rest, self._head = self._head[:n], self._head[n:]
                self._used = False
                return rest
            out = self._head + self._r.read(n - len(self._head))
            self._head = b""
            return out
        return self._r.read(n)

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)

    def readable(self):
        return True


def fetch_one(con, ccn: str, url: str, extract: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    prof = FileProfile()
    n_rows = 0
    meta_out: dict = {}

    with open_stream(url, archive_name=f"{ccn}") as (reader, meta):
        meta_out = dict(meta or {})
        if reader is None:
            con.execute(
                "INSERT INTO mrf_file VALUES (" + ",".join(["?"] * 17) + ")",
                [f"ERR:{ccn}:{url[:64]}", ccn, url, meta_out.get("status"),
                 meta_out.get("content_type"), 0, None, None, None, now, None, None,
                 "failed", meta_out.get("error"), 0, "stream_only", None])
            return {"ccn": ccn, "status": meta_out.get("status"),
                    "error": meta_out.get("error"), "parsed": False}

        peek = _Peek(reader)
        fmt = sniff_format(peek.head, meta_out.get("content_type"))

        # Compressed MRFs are machine-readable: decompress in the stream rather
        # than scoring the hospital non-compliant for shipping a .gz/.zip.
        if fmt in ("gzip", "zip"):
            inner = _decompress(peek, fmt)
            if inner is not None:
                peek = inner
                fmt = sniff_format(peek.head, None)
                prof.dialect_note = f"decompressed:{'gzip' if fmt else fmt}"
        prof.detected_format = fmt
        if peek.head.startswith(b"\xef\xbb\xbf"):
            peek.read(3)          # ijson and csv both choke on a leading BOM
        prof.detected_format = fmt

        if fmt in NON_MACHINE or fmt in ("zip", "gzip", "unknown"):
            # Still hash the whole thing so the finding is citable.
            prof.is_machine_readable = False
            prof.dialect = f"not_machine_readable:{fmt}"
            reader.drain()
        else:
            mod = cms_json if fmt == "json" else cms_csv if fmt == "csv" else None
            if mod is None:
                prof.dialect = f"unsupported:{fmt}"
                reader.drain()
            else:
                rows = []
                try:
                    for rec in mod.parse(peek, prof, want_codes=ALL_CODES,
                                         collect_prices=extract):
                        rows.append(rec)
                        if len(rows) >= 5000:
                            n_rows += _flush(con, rows, ccn, prof, reader, now)
                            rows = []
                    n_rows += _flush(con, rows, ccn, prof, reader, now)
                except Exception as e:
                    # One malformed file must not abort a multi-hour crawl. The
                    # failure is recorded as parse_status, which is a finding.
                    prof.error = prof.error or f"{type(e).__name__}: {e}"
                try:
                    reader.drain()   # hash must cover the WHOLE file
                except Exception:
                    pass

        sha = reader.sha256
        size = reader.pos

    parse_status = ("not_machine_readable" if not prof.is_machine_readable
                    else "failed" if prof.error and prof.rows_scanned == 0
                    else "partial" if prof.error else "ok")
    con.execute(
        "INSERT OR REPLACE INTO mrf_file VALUES (" + ",".join(["?"] * 17) + ")",
        [sha, ccn, url, meta_out.get("status"), meta_out.get("content_type"), size,
         sha, None, meta_out.get("archived_path"), now, prof.dialect, "utf-8",
         parse_status, prof.error, prof.rows_scanned,
         meta_out.get("storage_policy", "stream_only"), prof.cms_schema_version])
    con.execute("UPDATE price_raw SET file_id=?, source_sha256=? WHERE file_id=? AND ccn=?",
                [sha, sha, f"PENDING:{ccn}", ccn])

    for hid in list(prof.hospital_ids_in_file)[:200]:
        con.execute("INSERT OR REPLACE INTO file_hospital_attribution VALUES (?,?,?,?)",
                    [sha, ccn, "name_in_file", True])

    _profile_cache[sha] = prof
    return {"ccn": ccn, "sha256": sha, "bytes": size, "dialect": prof.dialect,
            "rows_scanned": prof.rows_scanned, "basket_rows": n_rows,
            "parse_status": parse_status, "profile": prof}


_profile_cache: dict[str, FileProfile] = {}


def _flush(con, rows, ccn, prof, reader, now) -> int:
    if not rows:
        return 0
    payload = [
        [None, f"PENDING:{ccn}", ccn, None, r.code, r.code_type, r.setting,
         r.payer_string_raw, r.plan_string_raw, r.rate_type, r.rate_dollars,
         r.rate_percent, r.estimated_allowed_amount, r.methodology, r.billing_class,
         r.byte_offset, (r.evidence_excerpt or "")[:EVIDENCE_EXCERPT_CHARS],
         prof.dialect, None, now.date()]
        for r in rows]
    con.executemany(
        "INSERT INTO price_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
    return len(payload)


def run(limit: int | None = None, extract: bool = True, only_missing: bool = True) -> dict:
    con = connect()
    q = ("SELECT DISTINCT d.ccn, d.url FROM discovery d WHERE d.url_found AND d.url IS NOT NULL ")
    if only_missing:
        q += "AND d.ccn NOT IN (SELECT ccn FROM mrf_file WHERE parse_status='ok') "
    q += "ORDER BY d.ccn"
    if limit:
        q += f" LIMIT {limit}"
    targets = con.execute(q).fetchall()

    # De-duplicate by URL: system files shared by many CCNs are fetched once.
    seen: dict[str, dict] = {}
    results = []
    for ccn, url in targets:
        if url in seen:
            r = dict(seen[url], ccn=ccn, reused=True)
            prof = _profile_cache.get(r.get("sha256"))
            if r.get("sha256"):
                con.execute("INSERT OR REPLACE INTO file_hospital_attribution VALUES (?,?,?,?)",
                            [r["sha256"], ccn, "shared_url", False])
        else:
            r = fetch_one(con, ccn, url, extract=extract)
            seen[url] = r
        results.append(r)

    n_frame = con.execute("SELECT count(*) FROM hospital").fetchone()[0]
    g = lambda w: con.execute(f"SELECT count(DISTINCT ccn) FROM mrf_file WHERE {w}").fetchone()[0]
    stats = {"attempted": len(targets), "unique_urls": len(seen),
             "downloaded": g("parse_status<>'failed'"),
             "parsed_ok": g("parse_status='ok'"),
             "not_machine_readable": g("parse_status='not_machine_readable'"),
             "failed": g("parse_status='failed'"),
             "basket_rows": con.execute("SELECT count(*) FROM price_raw").fetchone()[0]}
    record_attrition(con, "2_fetch", "download_attempted", len(targets), n_frame, "")
    record_attrition(con, "2_fetch", "downloaded_ok", stats["downloaded"], n_frame, "")
    record_attrition(con, "2_fetch", "not_machine_readable", stats["not_machine_readable"],
                     n_frame, "PDF/HTML: rubric R3=0, a finding not a fetch failure")
    record_attrition(con, "2_fetch", "parsed_structured", stats["parsed_ok"], n_frame, "")
    con.close()
    return stats
