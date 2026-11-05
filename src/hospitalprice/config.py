"""Global configuration. Everything deterministic hangs off SEED."""
from __future__ import annotations
import os
from pathlib import Path

SEED = 20260902

ROOT = Path(os.environ.get("HOSPITALPRICE_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
RAW = DATA / "raw"
CACHE = DATA / "cache"
DB_PATH = DATA / "db" / "hospitalprice.duckdb"
OUT = ROOT / "out"
TABLES = OUT / "tables"
FIGURES = OUT / "figures"
PAYERS_DIR = Path(__file__).resolve().parent / "payers"

STATE = "OH"

# --- Crawler identity -------------------------------------------------------
# Honest identification is a condition of running this at all. Do not anonymize.
# Set HOSPITALPRICE_CONTACT to a working email before crawling. There is no default.
CONTACT_EMAIL = os.environ.get("HOSPITALPRICE_CONTACT", "").strip()


def user_agent() -> str:
    """Crawler User-Agent. Raises if HOSPITALPRICE_CONTACT is unset, so no request goes out anonymous."""
    email = os.environ.get("HOSPITALPRICE_CONTACT", "").strip()
    if not email:
        raise RuntimeError("HOSPITALPRICE_CONTACT is not set. The crawler must identify itself with a "
                           "working contact email. Set it first: export HOSPITALPRICE_CONTACT=you@example.com")
    return ("hospitalprice-research/0.1 (academic study of 45 CFR 180 compliance; "
            f"contact: {email})")

# --- Politeness -------------------------------------------------------------
PER_DOMAIN_DELAY_S = 5.0      # seconds between request *initiations* per domain
MAX_CONCURRENT_PER_DOMAIN = 1  # never two transfers against one host
MAX_CONCURRENT_TOTAL = 6
CONNECT_TIMEOUT_S = 20.0
READ_TIMEOUT_S = 120.0
MAX_RETRIES = 3

# --- Storage policy ---------------------------------------------------------
# The build machine has <5GB free against ~300GB of raw MRFs. Default is
# stream-only: hash and parse in flight, discard raw bytes. See PREREGISTRATION §9.
ARCHIVE_RAW = os.environ.get("HOSPITALPRICE_ARCHIVE", "0") == "1"
MIN_FREE_BYTES_FOR_ARCHIVE = 150 * 1024**3
EVIDENCE_EXCERPT_CHARS = 400   # bounded provenance kept per extracted row

for _p in (RAW, CACHE, DB_PATH.parent, TABLES, FIGURES):
    _p.mkdir(parents=True, exist_ok=True)


def free_bytes(path: Path = DATA) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize
