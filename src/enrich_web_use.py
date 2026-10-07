#!/usr/bin/env python3
"""Establish current use from the open web, where Places cannot see it.

Google Places is a business directory. A building only appears in it if
something inside it registered as a business, so the uses that never register
are invisible: a shelter has no storefront, no hours and nothing to book.
35-02 37 Avenue is the case that forced this. It is a 125-room former hotel
operating under a $65.8m city contract as a shelter for single adults through
June 2030, reported in the trade and local press at the time of the sale. The
only thing Places knows about it is the Citi Bike dock at the kerb, so the
sweep recorded `type=bike_sharing_station` and the building kept a clean
target badge.

The web knows. That conversion is in Crain's, the Commercial Observer, QNS and
the Queens Post. This reads that corpus through the Programmable Search JSON
API and keeps the snippets and URLs it decided on, so every call this makes
can be checked by opening the link. It does not ask a model what the building
is; a model's opinion is not evidence, and evidence is the whole point of the
field this writes into.

    GOOGLE_API_KEY=... GOOGLE_CSE_ID=... python3 src/enrich_web_use.py
    ... --bbl 4003770013            one building, prints its evidence
    ... --limit 50                  stop after 50 lookups
    ... --all                       re-check buildings Places already answered
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
OUTPUT_FILE = DATA_RAW / f"web_current_use_{date.today():%Y%m%d}.json"
CACHE_FILE = DATA_RAW / "web_current_use_cache.json"

API_KEY = os.environ.get("GOOGLE_API_KEY", "")
CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
ENDPOINT = "https://www.googleapis.com/customsearch/v1"

# Places answered these with something, but not with the building. A shop at
# street level says nothing about the 120 rooms above it, and a blank says
# nothing at all — those two are the whole blind spot.
WEAK_PLACES_USES = {"", "ground_floor_tenant", "other"}

# Ordered. The first rule that fires wins, so the most consequential and most
# specific reading is tested first: calling a shelter a hostel loses the
# disqualification, calling a hostel a shelter loses a building the team asked
# to see. Each phrase has to be specific enough to survive a news page that
# merely mentions homelessness — "shelter" alone matches a bus shelter and an
# article about the shelter system three neighbourhoods away.
CLASSIFY_RULES = [
    ("shelter", "Shelter", False, (
        "homeless shelter", "migrant shelter", "asylum seeker", "asylum-seeker",
        "emergency shelter", "family shelter", "dhs shelter", "men's shelter",
        "women's shelter", "intake center", "humanitarian emergency response",
        "converted into a shelter", "converted to a shelter", "used as a shelter",
        "operating as a shelter", "now a shelter", "shelter for single adults",
        "shelter for migrants", "housing migrants", "housing asylum",
    )),
    # Treatment programs and on-site clinical services belong here, not in a
    # category of their own: whatever the building is called, somebody is
    # running it and the rooms are spoken for. 203 West 113 Street is the case
    # that forced it — Weston House, an apartment treatment program with
    # restorative services and an ACT team, read as available because the
    # Places sweep found a residential neighbour and nothing in this table
    # matched a word of what the building actually does.
    #
    # Phrased long on purpose. "treatment", "clinic" and "services" on their
    # own match a dentist on the ground floor and an article about the
    # treatment of asylum seekers three neighbourhoods away, which is the
    # failure the rest of this table is already written against. Each phrase
    # here names a programme that occupies a building.
    ("supportive_housing", "Supportive housing", False, (
        "supportive housing", "transitional housing", "halfway house",
        "sober living", "recovery residence", "safe haven",
        "treatment program", "treatment programme", "residential treatment",
        "apartment treatment", "assertive community treatment", "act team",
        "restorative services", "supportive services", "supported housing",
        "mental health services", "behavioral health services",
        "behavioural health services", "substance abuse treatment",
        "rehabilitation program", "adult home", "residential care",
        "serious mental illness", "persistent mental illness",
    )),
    ("sro", "Single room occupancy", True, (
        "single room occupancy", "single-room occupancy", " sro ", "sro hotel",
        "rooming house",
    )),
    ("hostel", "Hostel", True, (
        "hostel", "youth hostel", "backpacker",
    )),
    ("student_housing", "Student housing", True, (
        "dormitory", "residence hall", "student housing", "student residence",
        "university housing", "college dorm",
    )),
    ("private_club", "Private membership club", True, (
        "members club", "members' club", "private club", "social club",
        "private members", "athletic club", "yacht club", "university club",
    )),
    ("hotel", "Hotel", True, (
        "hotel", "inn ", " inn", "boutique hotel", "motel",
    )),
]

# A hit inside these is about the building. A hit on a booking aggregator or a
# listings farm is about a page that mentions the address, and those pages
# describe whatever the building was whenever the page was written.
TRUSTED_HOST_HINTS = (
    "nyc.gov", "crainsnewyork.com", "commercialobserver.com", "therealdeal.com",
    "qns.com", "queenspost.com", "qgazette.com", "amny.com", "gothamist.com",
    "nytimes.com", "ny1.com", "brooklynpaper.com", "patch.com", "6sqft.com",
    "bisnow.com", "curbed.com", "cityandstateny.com", "nypost.com", "bkrea.com",
    "citylimits.org", "documentedny.com", "thecity.nyc",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def search(query: str, retries: int = 3) -> list[dict]:
    """One Programmable Search call. Returns title/snippet/link triples."""
    params = urllib.parse.urlencode({
        "key": API_KEY, "cx": CSE_ID, "q": query, "num": 10,
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{ENDPOINT}?{params}", timeout=30) as fh:
                body = json.loads(fh.read())
            return [
                {
                    "title": it.get("title", ""),
                    "snippet": it.get("snippet", ""),
                    "link": it.get("link", ""),
                }
                for it in body.get("items", [])
            ]
        except urllib.error.HTTPError as exc:
            # 429 is the daily free-tier ceiling, and waiting does not clear it.
            if exc.code == 429:
                raise SystemExit(
                    "  search quota exhausted — the free tier is 100 queries a "
                    "day. Enable billing on the key or resume tomorrow; "
                    "results so far are already written."
                )
            if attempt == retries - 1:
                log(f"    HTTP {exc.code} on {query[:50]!r}")
                return []
            time.sleep(2 ** attempt)
        except Exception as exc:  # noqa: BLE001 — a single failed lookup is not fatal
            if attempt == retries - 1:
                log(f"    {type(exc).__name__} on {query[:50]!r}")
                return []
            time.sleep(2 ** attempt)
    return []


def mentions_address(text: str, addresses: list[str]) -> bool:
    """Does this result actually name one of the building's addresses?

    A search for an address returns pages about the block, the corner and the
    listing next door. Requiring the number and the street name to both appear
    is crude but it is the difference between evidence and adjacency.
    """
    low = text.lower()
    for addr in addresses:
        parts = addr.lower().split()
        if len(parts) < 2:
            continue
        number, street = parts[0], " ".join(parts[1:])
        street = re.sub(r"\b(st|street|ave|avenue|blvd|boulevard|rd|road|pl|place)\b",
                        "", street).strip()
        if not street:
            street = " ".join(parts[1:])
        if number in low and street[:8] in low:
            return True
    return False


def classify(results: list[dict], addresses: list[str]) -> dict | None:
    """Read the results for a use, keeping what the reading rests on."""
    for use, label, transient_ok, phrases in CLASSIFY_RULES:
        for r in results:
            blob = f"{r['title']} {r['snippet']}"
            low = blob.lower()
            hit = next((p for p in phrases if p in low), None)
            if not hit:
                continue
            if not mentions_address(blob, addresses):
                continue
            host = urllib.parse.urlparse(r["link"]).netloc.lower()
            trusted = any(h in host for h in TRUSTED_HOST_HINTS)
            return {
                "current_use": use,
                "current_use_label": label,
                "transient_ok": transient_ok,
                "use_confidence": "medium" if trusted else "low",
                "basis": f"web:{hit!r} in {host}",
                "evidence": [
                    {"title": r["title"], "snippet": r["snippet"], "link": r["link"]}
                ],
            }
    return None


def query_addresses(addresses: list[str], cap: int = 3) -> list[str]:
    """The distinct addresses worth spending a query on.

    A building's alt list is mostly spelling: "37-06 36 STREET", "37-06 36TH
    STREET" and "3706 36TH STREET" are one address written three ways and one
    query answers all three. But they are not all interchangeable to a
    newsroom — the Paper Factory conversion was reported at 37-06 36th Street
    and the building is carried here as 35-02 37 Avenue, so searching only the
    primary finds nothing at all. Collapse the spellings, keep the corners.
    """
    seen, out = set(), []
    for addr in addresses:
        if not addr:
            continue
        # Ordinals first, while the word boundaries still exist. Doing it
        # after squashing the punctuation turns "36 STREET" into "36reet",
        # because the "st" it strips is the one in the word street.
        key = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", addr.lower())
        key = re.sub(r"\b(street|str)\b", "st", key)
        key = re.sub(r"\b(avenue|av)\b", "ave", key)
        key = re.sub(r"\b(boulevard|blvd)\b", "bl", key)
        key = re.sub(r"\b(road)\b", "rd", key)
        key = re.sub(r"\b(place)\b", "pl", key)
        key = re.sub(r"[^a-z0-9]", "", key)
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
        if len(out) >= cap:
            break
    return out


def load_targets(all_buildings: bool, only_bbl: str | None) -> list[dict]:
    sys.path.insert(0, str(ROOT))
    from src.enrich_current_use import load_alt_addresses, load_buildings

    alts = load_alt_addresses()
    places = {}
    files = sorted(DATA_RAW.glob("google_current_use_[0-9]*.json"), reverse=True)
    if files:
        places = {r["bbl"]: r for r in json.loads(files[0].read_text()) if r.get("bbl")}
        log(f"  Places sweep: {files[0].name}, {len(places)} buildings")

    out = []
    for b in load_buildings():
        bbl = str(b.get("bbl") or "")
        if not bbl:
            continue
        if only_bbl and bbl != only_bbl:
            continue
        if not only_bbl and not all_buildings:
            if (places.get(bbl, {}).get("current_use") or "") not in WEAK_PLACES_USES:
                continue
        addr = b.get("address") or ""
        out.append({
            "bbl": bbl,
            "address": addr,
            "borough": b.get("borough") or "",
            "addresses": [addr] + [a for a in alts.get(bbl, []) if a and a != addr],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbl")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not API_KEY or not CSE_ID:
        sys.exit(
            "Set GOOGLE_API_KEY and GOOGLE_CSE_ID.\n"
            "  The key is the one the Places sweep already uses.\n"
            "  The CSE id comes from programmablesearchengine.google.com —\n"
            "  create an engine, set it to search the entire web, copy the id."
        )

    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    targets = load_targets(args.all, args.bbl)
    if args.limit:
        targets = targets[: args.limit]
    log(f"  checking {len(targets)} buildings on the open web")

    rows, found = [], 0
    for i, t in enumerate(targets, 1):
        key = t["bbl"]
        if key in cache and not args.bbl:
            verdict = cache[key]
        else:
            # The address plus the vocabulary of a conversion — a bare
            # address search returns rental listings for the whole block.
            # Stop at the first address that answers; most never need the
            # second query.
            verdict = None
            for addr in query_addresses(t["addresses"]):
                q = (f'"{addr}" {t["borough"]} '
                     f'(shelter OR hostel OR dormitory OR "single room occupancy" '
                     f'OR club OR hotel)')
                verdict = classify(search(q), t["addresses"])
                if verdict:
                    break
            cache[key] = verdict
            if i % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache, indent=2))
            time.sleep(0.2)

        row = {"bbl": key, "address": t["address"], "swept": True}
        if verdict:
            row.update(verdict)
            found += 1
            log(f"  [{i}/{len(targets)}] {t['address'][:34]:36s} -> "
                f"{verdict['current_use_label']}  ({verdict['basis'][:52]})")
            if args.bbl:
                for e in verdict["evidence"]:
                    log(f"        {e['link']}\n        {e['snippet'][:160]}")
        rows.append(row)

    CACHE_FILE.write_text(json.dumps(cache, indent=2))

    merged = {}
    if OUTPUT_FILE.exists():
        merged = {r["bbl"]: r for r in json.loads(OUTPUT_FILE.read_text())}
    merged.update({r["bbl"]: r for r in rows})
    OUTPUT_FILE.write_text(json.dumps(list(merged.values()), indent=2))

    log(f"\n  {found} of {len(targets)} answered by the web")
    log(f"  wrote {OUTPUT_FILE.relative_to(ROOT)} ({len(merged)} rows)")


if __name__ == "__main__":
    main()
