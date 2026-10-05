#!/usr/bin/env python3
"""Find the buildings the city has published a shelter notice for.

The City Record is where New York prints its procurement, and DHS prints the
address of every shelter it contracts for. 35-02 37 Avenue is there in plain
words — "Shelter facillities for Homeless Single Adults at 37-06 36th Street,
Long Island City, NY 11101", Destination Tomorrow, $65,781,607 — while Google
Places had only the Citi Bike dock at the kerb.

This flags. It never sets current use, and the reason is in the dates: the
dataset's start_date and end_date are publication dates, identical in 92% of
records, and only 21% of notices carry a contract term anywhere in the body.
A 2008 notice and a 2025 one are indistinguishable here. 17 Battery Place
carries a DHS notice from December 2008 and is an operating hotel today, so a
source that spoke with authority would quietly delete it from the list.

    SOCRATA_APP_TOKEN=... python3 src/enrich_city_record.py
    ... --verbose        print every match with the sentence it came from
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
OUTPUT_FILE = DATA_RAW / f"city_record_shelter_{date.today():%Y%m%d}.json"
DATASET = "dg92-zbpx"  # City Record Online
AWARDS = "qyyg-4tf5"   # the same notices, filtered to contract awards, with
                       # the vendor, the value and the kind of notice attached

# How old a shelter notice can be before it stops describing today. DHS adult
# and family shelter contracts run three to nine years, so an award inside
# five is very likely still running and a notice with nothing behind it for a
# decade is history. 17 Battery Place last appeared in 2008 and is a working
# hotel; 1 Hoyt Street was awarded to the African American Planning Commission
# for $51.9m in 2026 and plainly is not.
ACTIVE_AWARD_YEARS = 5
ACTIVE_NOTICE_YEARS = 3
HISTORICAL_AFTER_YEARS = 10

# DHS and HRA site shelters. Everything else that says "shelter" is a design
# commission agenda, a landmarks filing or an animal shelter, and each of them
# carries addresses that have nothing to do with the word.
SHELTER_AGENCIES = frozenset({
    "Homeless Services",
    "Dept. of Social Svcs/Human Resources Administration",
    "Human Resources Administration",
    "Social Services",
})

_SUFFIX = (r"(?:street|st|avenue|ave|av|road|rd|boulevard|blvd|place|pl|drive|dr"
           r"|parkway|pkwy|court|ct|lane|ln|terrace|way|broadway|concourse)")

# The address has to be introduced as a site. Without this the parser returns
# every address in a multi-item notice: a first pass matched 81 buildings, of
# which the great majority were sidewalk reconstruction agendas and the Law
# Department's own mailing address for bid submissions.
SITE_ADDRESS = re.compile(
    rf"(?:located\s+at|facility\s+at|shelter\s+at|site\s+at|program\s+at"
    rf"|premises\s+at|operate\s+(?:a\s+|the\s+)?[\w\s]{{0,40}}?at)\s+"
    rf"(\d+(?:-\d+)?)\s+((?:[A-Za-z0-9'\.]+\s+){{0,3}}?{_SUFFIX})\b",
    re.I,
)

# "term of 7/1/2025 - 6/30/2030". Only about a fifth of notices carry one, but
# where it exists it beats every guess below it.
TERM = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to|through|\u2013)\s*(\d{1,2}/\d{1,2}/\d{2,4})")


# Boilerplate that always precedes somewhere to post a bid, not somewhere a
# building is.
MAILING = re.compile(
    r"(ACCO|Contracting Officer|proposals?\s+(?:must|should)|bid\s+(?:documents|submission)|Attn:)",
    re.I,
)


def log(msg: str) -> None:
    print(msg, flush=True)


def strip_html(raw: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()


def normalise(addr: str | None) -> str:
    """One spelling for an address, so the city's and ours can be compared."""
    a = (addr or "").lower()
    a = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", a)
    a = re.sub(r"\b(street|str)\b", "st", a)
    a = re.sub(r"\b(avenue|av)\b", "ave", a)
    a = re.sub(r"\b(boulevard|blvd)\b", "bl", a)
    a = re.sub(r"\b(road)\b", "rd", a)
    a = re.sub(r"\b(place)\b", "pl", a)
    return re.sub(r"[^a-z0-9]", "", a)


def _iso(us_date: str) -> str:
    """m/d/yy or m/d/yyyy as written in a notice, to an ISO date."""
    try:
        mm, dd, yy = (int(x) for x in us_date.split("/"))
    except ValueError:
        return ""
    if yy < 100:
        yy += 2000 if yy < 70 else 1900
    try:
        return date(yy, mm, dd).isoformat()
    except ValueError:
        return ""


def fetch_awards(request_ids: list[str], ctx) -> dict[str, dict]:
    """The award record behind a notice: who won it, for how much, what kind.

    An Award means a contract was signed, which a solicitation does not.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(request_ids), 50):
        chunk = request_ids[i:i + 50]
        where = "request_id in(" + ",".join(f"'{r}'" for r in chunk) + ")"
        params = urllib.parse.urlencode({
            "$select": "request_id,type_of_notice_description,vendor_name,contract_amount",
            "$where": where, "$limit": 500,
        })
        url = f"https://data.cityofnewyork.us/resource/{AWARDS}.json?{params}"
        try:
            with urllib.request.urlopen(url, timeout=60, context=ctx) as fh:
                for r in json.loads(fh.read()):
                    out[r["request_id"]] = r
        except Exception as exc:  # noqa: BLE001
            log(f"    award lookup failed: {type(exc).__name__}")
    return out


def grade(notices: list[dict], today: date) -> tuple[str, str]:
    """Is this address a shelter now, or was it one once?

    Returns the status and the sentence that decided it. Nothing here is
    certainty — the city publishes no live register of shelter addresses, and
    Checkbook, which holds the contract terms, is behind bot protection. This
    is the strongest reading the open records support, and it is graded rather
    than binary so that nobody reads it as more than that.
    """
    terms = [n["term_end"] for n in notices if n.get("term_end")]
    if terms:
        latest = max(terms)
        if latest >= today.isoformat():
            return "active", f"contract term runs to {latest}"
        return "historical", f"contract term ended {latest}"

    awards = [n for n in notices if n.get("notice_kind") == "Award"]
    if awards:
        newest = max(n["published"] for n in awards)
        years = (today - date.fromisoformat(newest)).days / 365.25
        if years <= ACTIVE_AWARD_YEARS:
            who = next((n.get("vendor") for n in awards if n.get("vendor")), "")
            return "active", f"awarded {newest}" + (f" to {who}" if who else "")

    newest = max(n["published"] for n in notices)
    years = (today - date.fromisoformat(newest)).days / 365.25
    if years <= ACTIVE_NOTICE_YEARS:
        return "active", f"notice published {newest}"
    if years >= HISTORICAL_AFTER_YEARS:
        return "historical", f"nothing published since {newest}"
    return "uncertain", f"last published {newest}, no award on file"


PAGE_SIZE = 1000
# The search returns a few thousand notices. 200 pages is two hundred thousand,
# far above anything this query can return, so reaching it means the walk is not
# terminating and the run should fail rather than publish a partial sweep.
MAX_PAGES = 200


def _fetch_page(params: dict) -> list[dict]:
    """One page from Socrata, retried."""
    token = os.environ.get("SOCRATA_APP_TOKEN", "")
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        f"https://data.cityofnewyork.us/resource/{DATASET}.json?"
        f"{urllib.parse.urlencode(params)}",
        headers={"X-App-Token": token} if token else {},
    )
    # Socrata drops a long page under load often enough that a single timeout
    # would otherwise cost the whole pull.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as fh:
                return json.loads(fh.read())
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                raise
            log(f"    retrying page at offset {params.get('$offset')} after "
                f"{type(exc).__name__}")
            time.sleep(2 ** attempt)
    return []


def fetch_notices(fetch=_fetch_page) -> list[dict]:
    """Every City Record notice mentioning a shelter, 2003 to now.

    Paged on a stable sort key, which it was not. The query was $q="shelter"
    walked by $offset with no $order at all, and Socrata promises no consistent
    row order between unordered requests -- so rows move between pages while the
    walk is in progress, and an offset that assumes they stay put reads some
    twice and never reads others.

    It showed as a sweep that returned a different number of buildings every
    run with nothing in the code changing: 20 on 29 Sep, 14 on 1 Oct, fewer on
    5 Oct, and no notices withdrawn by the city in between. 17 Battery Place
    lost a 2008 notice that way, and 317 West 45th Street lost a 2023 one
    graded active -- which took a building running as a shelter back out of the
    default-hidden set and into the sourcing list.

    :id is Socrata's own row identifier, present on every dataset and unique,
    so ordering on it makes the walk deterministic without this needing to know
    anything about the City Record's own columns.

    Two more things the walk got wrong, both of which silently shortened it:

    It advanced by the page size rather than by the rows it actually received,
    so a short page -- which Socrata will return under load without being at
    the end of the data -- skipped the difference. It advances by len(batch)
    now, which cannot skip whatever the server did not send.

    And it stopped on any short page, treating "fewer than asked for" as
    "that was the last of them". Those are different things. It stops on an
    empty page instead, which costs one extra request per run and is the only
    answer the server gives that means end-of-data.
    """
    out: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        batch = fetch({
            "$q": "shelter",
            "$order": ":id",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        })
        if not batch:
            return out
        out += batch
        offset += len(batch)
    raise RuntimeError(
        f"City Record paging did not terminate after {MAX_PAGES} pages "
        f"({len(out)} rows); refusing to publish a partial sweep")


def facility_sites(notices: list[dict]) -> dict[str, list[dict]]:
    """Addresses a shelter notice places a facility at, keyed by spelling."""
    sites: dict[str, list[dict]] = {}
    for n in notices:
        if n.get("agency_name") not in SHELTER_AGENCIES:
            continue
        text = strip_html(n.get("additional_description_1"))
        for m in SITE_ADDRESS.finditer(text):
            if MAILING.search(text[max(0, m.start() - 160):m.start()]):
                continue
            addr = f"{m.group(1)} {m.group(2)}"
            term = TERM.search(text)
            sites.setdefault(normalise(addr), []).append({
                "term_end": _iso(term.group(2)) if term else "",
                "agency": n.get("agency_name"),
                "published": (n.get("start_date") or "")[:10],
                "title": (n.get("short_title") or "").strip(),
                "request_id": n.get("request_id"),
                "address_as_published": addr,
                "sentence": text[max(0, m.start() - 90):m.end() + 90].strip(),
            })
    return sites


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from src.enrich_current_use import load_alt_addresses, load_buildings

    log("  pulling City Record notices…")
    notices = fetch_notices()
    sites = facility_sites(notices)
    log(f"  {len(notices)} notices mention a shelter; "
        f"{len(sites)} distinct DHS/HRA facility addresses")

    # Attach the award record to every notice that has one, before grading.
    ctx = ssl.create_default_context(cafile=certifi.where())
    ids = sorted({e["request_id"] for ev in sites.values() for e in ev if e.get("request_id")})
    awards = fetch_awards(ids, ctx)
    for ev in sites.values():
        for e in ev:
            a = awards.get(e.get("request_id")) or {}
            e["notice_kind"] = a.get("type_of_notice_description", "")
            e["vendor"] = a.get("vendor_name", "")
            e["contract_amount"] = a.get("contract_amount", "")
    log(f"  {len(awards)} of {len(ids)} notices resolve to a contract award")

    today = date.today()
    alts = load_alt_addresses()
    rows = []
    for b in load_buildings():
        bbl = str(b.get("bbl") or "")
        if not bbl:
            continue
        for addr in [b.get("address")] + alts.get(bbl, []):
            key = normalise(addr)
            if key not in sites:
                continue
            ev = sorted(sites[key], key=lambda e: e["published"], reverse=True)
            status, because = grade(ev, today)
            rows.append({
                "shelter_status": status,
                "shelter_status_basis": because,
                "bbl": bbl,
                "address": b.get("address"),
                "matched_address": addr,
                # The most recent notice. Publication, not contract term —
                # the dataset does not carry one, which is the whole reason
                # this flags rather than decides.
                "latest_notice": ev[0]["published"],
                "notice_count": len(ev),
                "notices": ev[:5],
            })
            break

    OUTPUT_FILE.write_text(json.dumps(rows, indent=2))
    log(f"\n  {len(rows)} buildings carry a DHS/HRA shelter notice")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["shelter_status"]] = counts.get(r["shelter_status"], 0) + 1
    log("  " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    for r in sorted(rows, key=lambda r: r["latest_notice"], reverse=True):
        log(f"    {r['shelter_status']:10s} {r['address'][:32]:34s} {r['shelter_status_basis'][:56]}")
        if args.verbose:
            log(f"        {r['notices'][0]['sentence'][:150]}")
    log(f"\n  wrote {OUTPUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
