"""Stage 1b - hospital -> web domain resolution.

No CMS dataset contains a hospital website. This is the first place the pipeline
can silently lose hospitals, so it is its own stage with its own N and its own
confidence coding.

`shared_with_n` is load-bearing: in Ohio roughly a dozen systems hold most acute
beds, and one domain (and one cms-hpt.txt) can cover fifteen CCNs. Whether those
fifteen are all credited with compliance is the single largest lever on the
headline number -- see Stage 3's dual attribution rules.
"""
from __future__ import annotations
import csv
import re
from datetime import datetime, timezone

from ..config import CACHE
from ..db import connect, record_attrition

# Curated Ohio system -> domain map. Hand-built and version-controlled: these
# are research decisions, not guesses, and each is inspectable. Matched against
# hospital name/DBA. Order matters; first match wins.
SYSTEM_DOMAINS: list[tuple[str, str, str]] = [
    (r"cleveland clinic|fairview hosp|hillcrest hosp|lutheran hosp|marymount hosp|"
     r"south pointe|euclid hosp|medina hosp|avon hosp|mercy hosp lorain", "clevelandclinic.org", "Cleveland Clinic"),
    (r"university hosp|uh \b|rainbow babies|ahuja|geauga medical|elyria medical|"
     r"parma medical|st\.? john medical|conneaut|geneva medical|samaritan medical", "uhhospitals.org", "University Hospitals"),
    (r"ohiohealth|riverside methodist|grant medical|doctors hosp|dublin methodist|"
     r"grady memorial|marion general|mansfield hosp|shelby hosp|berger", "ohiohealth.com", "OhioHealth"),
    (r"mercy health|bon secours|springfield regional|urbana|lourdes|"
     r"st\.? rita|st\.? charles|st\.? vincent medical|st\.? anne|defiance|tiffin", "mercy.com", "Bon Secours Mercy Health"),
    (r"mount carmel|trinity health", "mountcarmelhealth.com", "Trinity Health (Mount Carmel)"),
    (r"promedica|toledo hosp|flower hosp|bay park|monroe regional|fostoria|"
     r"wildwood|defiance regional", "promedica.org", "ProMedica"),
    (r"kettering|grandview|sycamore|soin|fort hamilton|troy hosp|greene memorial", "ketteringhealth.org", "Kettering Health"),
    (r"premier health|miami valley|atrium medical|upper valley", "premierhealth.com", "Premier Health"),
    (r"trihealth|bethesda north|good samaritan hosp.*cincinnati|mccullough.hyde", "trihealth.com", "TriHealth"),
    (r"uc health|university of cincinnati medical|west chester hosp|daniel drake", "uchealth.com", "UC Health"),
    (r"summa health|akron city hosp|barberton|st\.? thomas hosp", "summahealth.org", "Summa Health"),
    (r"aultman", "aultman.org", "Aultman"),
    (r"metrohealth", "metrohealth.org", "MetroHealth"),
    (r"nationwide children", "nationwidechildrens.org", "Nationwide Children's"),
    (r"cincinnati children", "cincinnatichildrens.org", "Cincinnati Children's"),
    (r"akron children", "akronchildrens.org", "Akron Children's"),
    (r"dayton children", "childrensdayton.org", "Dayton Children's"),
    (r"ohio state|wexner|james cancer", "wexnermedical.osu.edu", "Ohio State Wexner"),
    (r"firelands", "firelands.com", "Firelands"),
    (r"licking memorial", "lmhealth.org", "Licking Memorial"),
    (r"wooster community", "woosterhospital.org", "Wooster Community"),
    (r"adena", "adena.org", "Adena Health"),
    (r"holzer", "holzer.org", "Holzer Health"),
    (r"genesis health|genesis hosp", "genesishcs.org", "Genesis HealthCare System"),
    (r"blanchard valley", "bvhealthsystem.org", "Blanchard Valley"),
    (r"wood county hosp", "woodcountyhospital.org", "Wood County"),
    (r"memorial health.*marysville|memorial hosp.*union", "memorialohio.com", "Memorial Health"),
    (r"encompass health", "encompasshealth.com", "Encompass Health"),
    (r"select specialty|regency hosp", "selectspecialtyhospitals.com", "Select Medical"),
    (r"kindred", "kindredhospitals.com", "Kindred"),
    (r"acadia|glenbeigh|windsor laurelwood|belmont pines|cambridge behavioral", "acadiahealthcare.com", "Acadia Healthcare"),
    (r"lifepoint|ohio valley", "lifepointhealth.net", "Lifepoint"),
    (r"va medical|veterans affairs", "va.gov", "Veterans Health Administration"),
]


def guess_domain(name: str) -> tuple[str | None, str | None, str]:
    n = (name or "").lower()
    for pat, dom, system in SYSTEM_DOMAINS:
        if re.search(pat, n):
            return dom, system, "system_map"
    return None, None, "unresolved"


def resolve() -> dict:
    con = connect()
    now = datetime.now(timezone.utc)
    con.execute("DELETE FROM hospital_domain")
    rows = con.execute("SELECT ccn, name, dba_name FROM hospital").fetchall()

    resolved = []
    for ccn, name, dba in rows:
        dom, system, method = guess_domain(f"{name or ''} {dba or ''}")
        if dom:
            resolved.append((ccn, dom, method, "probable", system))

    counts: dict[str, int] = {}
    for _, dom, _, _, _ in resolved:
        counts[dom] = counts.get(dom, 0) + 1

    for ccn, dom, method, conf, system in resolved:
        con.execute("INSERT INTO hospital_domain VALUES (?,?,?,?,?,?,?)",
                    [ccn, dom, method, conf, counts[dom], f"system={system}", now])
        con.execute("UPDATE hospital SET system_name=? WHERE ccn=?", [system, ccn])

    n_frame = con.execute("SELECT count(*) FROM hospital").fetchone()[0]
    n_res = len({c for c, *_ in resolved})
    record_attrition(con, "1b_domains", "domain_resolved", n_res, n_frame,
                     "curated system map; unresolved go to the manual queue")
    record_attrition(con, "1b_domains", "domain_unresolved_manual_queue",
                     n_frame - n_res, n_frame, "requires human resolution")

    shared = con.execute(
        "SELECT domain, count(*) c FROM hospital_domain GROUP BY 1 HAVING c>1 ORDER BY c DESC").fetchall()
    record_attrition(con, "1b_domains", "domains_covering_multiple_ccns", len(shared), None,
                     "one cms-hpt.txt may cover many hospitals; drives Stage 3 attribution")

    # Manual queue is a real deliverable, not a TODO comment.
    q = CACHE / "domain_manual_queue.csv"
    with open(q, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ccn", "name", "dba_name", "city", "provider_category", "domain_to_fill", "note"])
        for r in con.execute(
            "SELECT h.ccn,h.name,h.dba_name,h.city,h.provider_category FROM hospital h "
            "LEFT JOIN hospital_domain d USING(ccn) WHERE d.ccn IS NULL ORDER BY h.provider_category,h.name"
        ).fetchall():
            w.writerow(list(r) + ["", ""])
    out = {"frame": n_frame, "resolved": n_res, "unresolved": n_frame - n_res,
           "shared_domains": len(shared), "queue": str(q)}
    con.close()
    return out


def load_manual(path: str) -> int:
    """Ingest the human-completed queue. Manual resolutions are marked 'certain'
    and carry the reviewer's note."""
    con = connect()
    now = datetime.now(timezone.utc)
    n = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            dom = (row.get("domain_to_fill") or "").strip().lower()
            if not dom:
                continue
            dom = re.sub(r"^https?://", "", dom).split("/")[0].removeprefix("www.")
            con.execute("INSERT OR REPLACE INTO hospital_domain VALUES (?,?,?,?,?,?,?)",
                        [row["ccn"], dom, "manual", "certain", 1,
                         row.get("note", ""), now])
            n += 1
    con.execute("UPDATE hospital_domain SET shared_with_n=(SELECT count(*) FROM hospital_domain d2 "
                "WHERE d2.domain=hospital_domain.domain)")
    con.close()
    return n
