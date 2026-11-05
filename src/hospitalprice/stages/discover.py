"""Stage 2a - MRF discovery.

Order is deliberate and each attempt is recorded even when it fails, because
'how the file was found' is itself a compliance finding: a hospital whose MRF is
only reachable by hand-searching the footer has not met the same standard as one
with a valid cms-hpt.txt.

  1. https://<domain>/cms-hpt.txt   (CMS-mandated pointer, CY2024 OPPS)
  2. homepage + standard footer paths for price-transparency language
  3. aggregator index
  4. manual queue
"""
from __future__ import annotations
import csv
import json
import re
from datetime import datetime, timezone

from ..config import CACHE
from ..db import connect, record_attrition
from ..http import get_text

MRF_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
MRF_EXT_RE = re.compile(r"\.(json|csv|xml)(\.gz|\.zip)?(\?|$)", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

FOOTER_PATHS = [
    "/", "/price-transparency", "/pricing-transparency", "/patients/price-transparency",
    "/patients-visitors/billing/price-transparency", "/billing/price-transparency",
    "/standard-charges", "/machine-readable-file", "/patients/billing/pricing",
    "/about/price-transparency", "/pricing", "/charges",
]
PT_LINK_RE = re.compile(
    r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(?:(?!</a>).){0,300}?"
    r"(price\s*transparency|standard\s*charges|machine[\s-]*readable|chargemaster|"
    r"charge\s*master|price\s*list)", re.I | re.S)

WALL_MARKERS = [
    (re.compile(r"\b(sign in|log in|username|password)\b", re.I), "login"),
    (re.compile(r"recaptcha|hcaptcha|cf-turnstile", re.I), "captcha"),
    (re.compile(r"\b(i agree|accept the terms|terms of use.{0,80}agree)\b", re.I), "clickthrough"),
]


CMS_KEYS = {"location-name", "source-page-url", "mrf-url", "contact-name", "contact-email"}
KV_RE = re.compile(r"^\s*([A-Za-z][\w \-]{2,40}?)\s*[:=]\s*(.*)$")


def parse_cms_hpt(text: str) -> dict:
    """Parse cms-hpt.txt into its per-location BLOCKS.

    CMS specifies the keys location-name / source-page-url / mrf-url /
    contact-name / contact-email, and a system publishes one block per hospital.
    Parsing blocks rather than scraping URLs is what lets Stage 3 attribute a
    specific MRF to a specific CCN instead of crediting every hospital on the
    domain -- the single largest lever on the headline compliance number.

    URL detection must NOT rely on a .json/.csv extension: real files use
    Sitecore .ashx handlers and extensionless vendor endpoints (Panacea, Serif),
    and an extension test silently scores those hospitals non-compliant.
    """
    blocks: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur); cur = {}
            continue
        m = KV_RE.match(line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2).strip()
        # a repeated location-name starts a new block even without a blank line
        if k == "location-name" and "location-name" in cur:
            blocks.append(cur); cur = {}
        cur[k] = v
    if cur:
        blocks.append(cur)

    locations = []
    for b in blocks:
        url = b.get("mrf-url") or b.get("mrf url") or b.get("location")
        if not url:
            cand = [u for u in MRF_URL_RE.findall(" ".join(b.values()))
                    if not re.search(r"source[-_ ]?page", u, re.I)]
            url = cand[0] if cand else None
        if not url:
            continue
        locations.append({
            "location_name": b.get("location-name") or b.get("hospital name") or b.get("name"),
            "mrf_url": url.strip(),
            "source_page_url": b.get("source-page-url"),
            "contact_email": b.get("contact-email") or next(
                iter(EMAIL_RE.findall(" ".join(b.values()))), None),
        })

    # Fallback for files that ignore the CMS key convention entirely.
    if not locations:
        urls = [u for u in MRF_URL_RE.findall(text) if MRF_EXT_RE.search(u)]
        emails = EMAIL_RE.findall(text)
        if urls:
            locations = [{"location_name": None, "mrf_url": urls[0],
                          "source_page_url": None,
                          "contact_email": emails[0] if emails else None}]

    uses_cms_keys = any(k in CMS_KEYS for b in blocks for k in b)
    return {
        "raw_len": len(text),
        "locations": locations,
        "mrf_urls": [l["mrf_url"] for l in locations],
        "emails": EMAIL_RE.findall(text),
        "fields": {k: v for b in blocks for k, v in b.items()},
        "uses_cms_keys": uses_cms_keys,
        # Well-formed = it does the job it exists to do: point at a file and
        # give a contact. Layout is not scored; substance is.
        "wellformed": bool(locations) and any(l["contact_email"] for l in locations),
    }


def _classify_wall(html: str) -> str:
    for rx, label in WALL_MARKERS:
        if rx.search(html):
            return label
    if len(html) < 2000 and re.search(r"__NEXT_DATA__|ng-app|<div id=[\"']root[\"']", html):
        return "js_render"
    return "none"


def _norm_name(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(the|inc|llc|lp|ltd|corp|co|of|at|and|hospital|hospitals|medical|"
               r"center|centre|health|healthcare|system|campus|regional|community|"
               r"memorial|general|main)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def match_locations_to_ccns(locations: list[dict], hospitals: list[tuple]) -> dict:
    """Map cms-hpt.txt location blocks to CCNs by name overlap.

    Deliberately conservative: a CCN is matched only on a clear token-overlap
    win. Unmatched CCNs are left unmatched rather than being assigned the
    system's first file, because a wrong attribution manufactures both a
    compliance credit and a set of prices that belong to another hospital.
    """
    out: dict[str, dict] = {}
    if not locations:
        return out
    for ccn, name, dba in hospitals:
        h_tokens = set(_norm_name(f"{name or ''} {dba or ''}").split())
        if not h_tokens:
            continue
        best, best_score = None, 0.0
        for loc in locations:
            l_tokens = set(_norm_name(loc.get("location_name")).split())
            if not l_tokens:
                continue
            inter = h_tokens & l_tokens
            if not inter:
                continue
            score = len(inter) / len(h_tokens | l_tokens)
            if score > best_score:
                best, best_score = loc, score
        if best is not None and best_score >= 0.34:
            out[ccn] = dict(best, match_score=round(best_score, 3),
                            attribution="named_in_cms_hpt")
    return out


def probe_cms_hpt(domain: str) -> tuple[dict | None, int | None, str]:
    for scheme in ("https", "http"):
        r = get_text(f"{scheme}://{domain}/cms-hpt.txt", max_bytes=400_000)
        if r.ok and "html" not in (r.content_type or "").lower():
            body = r.text()
            if body.strip():
                return parse_cms_hpt(body), r.status, ""
        if r.status is not None:
            return None, r.status, (r.error or "")
    return None, None, "unreachable"


def scan_footer(domain: str) -> dict:
    """Fallback discovery. Records the wall type even when no URL is found --
    'behind a click-through agreement' is a rubric element (R10), not a
    crawling nuisance."""
    res = {"url": None, "wall_type": "none", "status": None, "content_type": None}
    for path in FOOTER_PATHS:
        r = get_text(f"https://{domain}{path}", max_bytes=1_500_000)
        res["status"] = r.status or res["status"]
        if not r.ok:
            continue
        html = r.text()
        res["content_type"] = r.content_type
        wall = _classify_wall(html)
        if wall != "none" and res["wall_type"] == "none":
            res["wall_type"] = wall
        cands = []
        for href, _lbl in PT_LINK_RE.findall(html):
            cands.append(href if href.startswith("http")
                         else f"https://{domain}/{href.lstrip('/')}")
        cands += [u for u in MRF_URL_RE.findall(html)
                  if re.search(r"standard.?charge|machine.?readable|transparen", u, re.I)]
        for u in cands:
            if MRF_EXT_RE.search(u) or re.search(r"standard.?charge|machine.?readable", u, re.I):
                res["url"] = u
                return res
    return res


def run(limit: int | None = None, resume: bool = True) -> dict:
    """resume=True (default) skips domains already probed, so an interrupted
    crawl continues instead of re-hitting every hospital's server."""
    con = connect()
    now = datetime.now(timezone.utc)
    if not resume:
        con.execute("DELETE FROM discovery")

    doms = con.execute(
        "SELECT d.domain, count(*) FROM hospital_domain d "
        "WHERE NOT ? OR d.domain NOT IN ("
        "  SELECT d2.domain FROM hospital_domain d2 JOIN discovery x ON x.ccn=d2.ccn) "
        "GROUP BY 1 ORDER BY 2 DESC", [resume]).fetchall()
    if limit:
        doms = doms[:limit]

    for domain, _n in doms:
        hosps = con.execute(
            "SELECT h.ccn,h.name,h.dba_name FROM hospital h JOIN hospital_domain d USING(ccn) "
            "WHERE d.domain=?", [domain]).fetchall()
        parsed, status, err = probe_cms_hpt(domain)
        present = parsed is not None
        wellformed = bool(parsed and parsed["wellformed"])
        matches = match_locations_to_ccns(parsed["locations"], hosps) if parsed else {}
        footer = None

        for ccn, name, dba in hosps:
            m = matches.get(ccn)
            url, method, note = None, None, ""
            if m:
                url, method = m["mrf_url"], "cms_hpt_txt"
                note = f"matched location='{m.get('location_name')}' score={m['match_score']}"
            elif parsed and len(parsed["locations"]) == 1 and len(hosps) == 1:
                url, method = parsed["locations"][0]["mrf_url"], "cms_hpt_txt"
                note = "single-location file on single-hospital domain"
            else:
                if footer is None:
                    footer = scan_footer(domain)
                if footer["url"]:
                    url, method = footer["url"], "footer"
                    note = ("domain-level file; NOT named in cms-hpt.txt"
                            if present else "no cms-hpt.txt; footer scan")
                else:
                    note = ("cms-hpt.txt present but this CCN not named in it"
                            if present else (err or "no cms-hpt.txt and no footer link"))
            con.execute("INSERT INTO discovery VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [ccn, url, url is not None, method, present, wellformed,
                         json.dumps({"locations": len(parsed["locations"]),
                                     "uses_cms_keys": parsed["uses_cms_keys"]})[:2000] if parsed else None,
                         status, (footer or {}).get("content_type"),
                         (footer or {}).get("wall_type", "none"), now, note])

    n_frame = con.execute("SELECT count(*) FROM hospital").fetchone()[0]
    g = lambda w: con.execute(f"SELECT count(DISTINCT ccn) FROM discovery WHERE {w}").fetchone()[0]
    stats = {"frame": n_frame, "probed_domains": len(doms),
             "cms_hpt_present": g("cms_hpt_present"),
             "cms_hpt_wellformed": g("cms_hpt_wellformed"),
             "url_via_cms_hpt": g("discovery_method='cms_hpt_txt'"),
             "url_via_footer": g("discovery_method='footer'"),
             "url_found_any": g("url_found")}
    record_attrition(con, "2_discovery", "cms_hpt_present", stats["cms_hpt_present"], n_frame,
                     "CY2024 OPPS root pointer file reachable")
    record_attrition(con, "2_discovery", "cms_hpt_wellformed", stats["cms_hpt_wellformed"], n_frame,
                     "names a file and gives a contact")
    record_attrition(con, "2_discovery", "url_via_cms_hpt", stats["url_via_cms_hpt"], n_frame,
                     "hospital NAMED in cms-hpt.txt with its own MRF url")
    record_attrition(con, "2_discovery", "url_via_footer", stats["url_via_footer"], n_frame,
                     "fallback; domain-level attribution only")
    record_attrition(con, "2_discovery", "mrf_url_found_any_method", stats["url_found_any"], n_frame, "")
    con.close()
    return stats
