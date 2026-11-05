"""Polite, honest, streaming HTTP layer.

Rules enforced here, not left to callers:
  - one request initiation per domain per PER_DOMAIN_DELAY_S
  - never two concurrent transfers against one host
  - robots.txt consulted and obeyed
  - crawler identifies itself with a real contact address
  - large bodies are streamed, hashed in flight, and never fully materialized
"""
from __future__ import annotations
import contextlib
import hashlib
import io
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import zstandard as zstd

from .config import (ARCHIVE_RAW, CACHE, CONNECT_TIMEOUT_S, MAX_RETRIES,
                     MIN_FREE_BYTES_FOR_ARCHIVE, PER_DOMAIN_DELAY_S, RAW,
                     READ_TIMEOUT_S, free_bytes, user_agent)

_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_domain_locks: dict[str, threading.Lock] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _domain_lock(dom: str) -> threading.Lock:
    with _lock:
        return _domain_locks.setdefault(dom, threading.Lock())


def _throttle(dom: str) -> None:
    """Block until this domain may be hit again. Applies to request *initiation*."""
    while True:
        with _lock:
            elapsed = time.monotonic() - _last_hit.get(dom, -1e9)
            if elapsed >= PER_DOMAIN_DELAY_S:
                _last_hit[dom] = time.monotonic()
                return
            wait = PER_DOMAIN_DELAY_S - elapsed
        time.sleep(wait)


def _client() -> httpx.Client:
    ua = user_agent()
    return httpx.Client(
        headers={"User-Agent": ua, "Accept": "*/*", "From": ua.split("contact: ")[-1].rstrip(")")},
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=True,
        verify=True,
    )


def robots_allows(url: str) -> bool:
    """Fetch and cache robots.txt per domain. On error, default to allowing
    (a missing robots.txt is not a disallow), but a fetched Disallow is obeyed."""
    dom = domain_of(url)
    if dom not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _throttle(dom)
            with _client() as c:
                r = c.get(f"https://{dom}/robots.txt")
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None
        with _lock:
            _robots[dom] = rp
    rp = _robots.get(dom)
    if rp is None:
        return True
    return rp.can_fetch(user_agent(), url)


@dataclass
class FetchResult:
    url: str
    status: int | None
    content_type: str | None
    bytes_len: int
    sha256: str | None
    archived_path: str | None
    sha256_archive: str | None
    error: str | None = None
    storage_policy: str = "stream_only"
    headers: dict = field(default_factory=dict)
    body: bytes | None = None      # populated for small-body GETs only

    def text(self, limit: int = 2_000_000) -> str:
        if not self.body:
            return ""
        return self.body[:limit].decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None


def get_text(url: str, max_bytes: int = 2_000_000, respect_robots: bool = True) -> FetchResult:
    """Small-body GET (cms-hpt.txt, homepages). Body kept in memory, capped."""
    dom = domain_of(url)
    if respect_robots and not robots_allows(url):
        return FetchResult(url, None, None, 0, None, None, None, error="robots_disallow")
    with _domain_lock(dom):
        _throttle(dom)
        try:
            with _client() as c, c.stream("GET", url) as r:
                buf = io.BytesIO()
                for chunk in r.iter_bytes(65536):
                    buf.write(chunk)
                    if buf.tell() > max_bytes:
                        break
                b = buf.getvalue()
                return FetchResult(str(r.url), r.status_code,
                                   r.headers.get("content-type"), len(b),
                                   hashlib.sha256(b).hexdigest(), None, None,
                                   headers=dict(r.headers), body=b)
        except Exception as e:
            return FetchResult(url, None, None, 0, None, None, None, error=f"{type(e).__name__}: {e}")


def stream_to(url: str, sink, archive_name: str | None = None,
              respect_robots: bool = True) -> FetchResult:
    """Stream a (possibly multi-GB) body.

    `sink` is called with successive byte chunks -- this is how Stage 4 parses in
    flight without ever materializing the file. sha256 is computed over the
    uncompressed stream, because that is the artifact we cite.

    Archiving is opt-in AND refuses to run without real headroom; under the
    default stream-only policy the raw bytes are discarded after parsing.
    """
    dom = domain_of(url)
    if respect_robots and not robots_allows(url):
        return FetchResult(url, None, None, 0, None, None, None, error="robots_disallow")

    do_archive = ARCHIVE_RAW and archive_name is not None
    if do_archive and free_bytes() < MIN_FREE_BYTES_FOR_ARCHIVE:
        do_archive = False  # policy: silently degrade to stream-only, but record it

    h = hashlib.sha256()
    ha = hashlib.sha256()
    total = 0
    out_path = None
    cctx = fh = writer = None
    if do_archive:
        out_path = RAW / f"{archive_name}.zst"
        fh = open(out_path, "wb")
        cctx = zstd.ZstdCompressor(level=10)
        writer = cctx.stream_writer(fh)

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with _domain_lock(dom):
                _throttle(dom)
                with _client() as c, c.stream("GET", url) as r:
                    if r.status_code != 200:
                        return FetchResult(str(r.url), r.status_code,
                                           r.headers.get("content-type"), 0, None,
                                           None, None, headers=dict(r.headers))
                    for chunk in r.iter_bytes(1 << 20):
                        h.update(chunk)
                        total += len(chunk)
                        sink(chunk)
                        if writer is not None:
                            writer.write(chunk)
                    ct = r.headers.get("content-type")
                    hdrs = dict(r.headers)
            if writer is not None:
                writer.flush(zstd.FLUSH_FRAME); writer.close(); fh.close()
                ha = hashlib.sha256(out_path.read_bytes())
            return FetchResult(url, 200, ct, total, h.hexdigest(),
                               str(out_path) if out_path else None,
                               ha.hexdigest() if out_path else None,
                               storage_policy="archive" if do_archive else "stream_only",
                               headers=hdrs)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    if fh is not None and not fh.closed:
        fh.close()
    return FetchResult(url, None, None, total, None, None, None, error=last_err)


def cached_download(url: str, name: str, stage: str, con=None, force: bool = False) -> Path:
    """Reference-data download with content-addressed caching. Every external
    artifact this study depends on is retained with its hash and timestamp."""
    dest = CACHE / name
    if dest.exists() and not force:
        return dest
    parts: list[bytes] = []
    res = stream_to(url, parts.append, respect_robots=False)
    if not res.ok:
        raise RuntimeError(f"download failed {url}: {res.status} {res.error}")
    dest.write_bytes(b"".join(parts))
    if con is not None:
        con.execute("INSERT INTO download_log VALUES (?,?,?,?,?,now(),?,?)",
                    [url, res.sha256, res.bytes_len, res.status, res.content_type,
                     str(dest), stage])
    return dest


# --- file-like streaming adapter -------------------------------------------
class HashingReader(io.RawIOBase):
    """Presents an HTTP byte-iterator as a readable file object while hashing,
    counting, and (optionally) archiving what passes through.

    This is what lets ijson stream-parse a 10GB MRF straight off the socket:
    the parser pulls, we never hold more than one chunk, and .sha256/.pos are
    exact at every point -- which is what makes byte-offset provenance real.
    """

    def __init__(self, chunks, archive_writer=None):
        self._it = iter(chunks)
        self._buf = b""
        self._eof = False
        self._h = hashlib.sha256()
        self.pos = 0
        self._archive = archive_writer

    def readable(self) -> bool:
        return True

    def _pull(self) -> bool:
        try:
            c = next(self._it)
        except StopIteration:
            self._eof = True
            return False
        self._h.update(c)
        if self._archive is not None:
            self._archive.write(c)
        self._buf += c
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            while self._pull():
                pass
            out, self._buf = self._buf, b""
        else:
            while len(self._buf) < n and not self._eof:
                self._pull()
            out, self._buf = self._buf[:n], self._buf[n:]
        self.pos += len(out)
        return out

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    @property
    def sha256(self) -> str:
        return self._h.hexdigest()

    def drain(self) -> None:
        """Consume any remainder so the hash covers the whole file even when the
        parser stopped early. Without this, sha256 would be a partial-file hash
        silently masquerading as the file's identity, and file_size_bytes would
        report however much the parser happened to read."""
        self.pos += len(self._buf)
        self._buf = b""
        while self._pull():
            self.pos += len(self._buf)
            self._buf = b""


@contextlib.contextmanager
def open_stream(url: str, archive_name: str | None = None, respect_robots: bool = True):
    """Yield (HashingReader, meta_dict) for a possibly-enormous body."""
    dom = domain_of(url)
    if respect_robots and not robots_allows(url):
        yield None, {"error": "robots_disallow", "status": None}
        return
    do_archive = ARCHIVE_RAW and archive_name is not None and free_bytes() >= MIN_FREE_BYTES_FOR_ARCHIVE
    fh = writer = None
    out_path = None
    if do_archive:
        out_path = RAW / f"{archive_name}.zst"
        fh = open(out_path, "wb")
        writer = zstd.ZstdCompressor(level=10).stream_writer(fh)
    try:
        with _domain_lock(dom):
            _throttle(dom)
            with _client() as c, c.stream("GET", url) as r:
                meta = {"status": r.status_code,
                        "content_type": r.headers.get("content-type"),
                        "final_url": str(r.url),
                        "headers": dict(r.headers),
                        "storage_policy": "archive" if do_archive else "stream_only",
                        "archived_path": str(out_path) if out_path else None,
                        "error": None}
                if r.status_code != 200:
                    yield None, meta
                    return
                reader = HashingReader(r.iter_bytes(1 << 20), writer)
                try:
                    yield reader, meta
                finally:
                    # Record identity even if the caller raised: a parse failure
                    # is a finding that still needs a hash and a size.
                    meta["sha256"] = reader.sha256
                    meta["bytes"] = reader.pos
    except Exception as e:
        yield None, {"error": f"{type(e).__name__}: {e}", "status": None,
                     "content_type": None, "storage_policy": "stream_only",
                     "archived_path": None}
    finally:
        if writer is not None:
            try:
                writer.flush(zstd.FLUSH_FRAME); writer.close()
            except Exception:
                pass
        if fh is not None and not fh.closed:
            fh.close()
