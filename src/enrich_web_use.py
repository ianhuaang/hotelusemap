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
the Queens Post. This reads that corpus through the Claude API's server-side
web search tool and keeps the quotes and URLs it came back with, so every call
this makes can be checked by opening the link.

It does not ask a model what the building is. The model searches and quotes;
CLASSIFY_RULES and corroborate() decide, in Python, over text the model did
not write -- `cited_text` is verbatim source text, not prose about a source.
That line is the point of the field this writes into, and it has moved one
step rather than gone: the model no longer judges the evidence, but it does
choose which passages become evidence. A sentence it declines to quote is a
rule that never fires. SEARCH_SYSTEM is written against exactly that, and
results it found but never quoted are still returned, on their titles.

This replaced Programmable Search, which could only ever search a slice of
the web on our account -- an engine has to be pointed at sites, and "the
entire web" is a setting that approximates one. The cost model inverted with
it: no 100-a-day ceiling, and $10 per 1,000 searches plus tokens instead.

It runs in two modes against the same corpus. The default reads a use for
buildings Places could not see. --corroborate answers a narrower question for
buildings Places answered too confidently: the no-operator buildings that are
occupied on a single Places reading with nothing agreeing with it. There the
question is not "what is this building" but "does anything outside Google say
the same thing", and the answer decides whether the claim closes as occupied,
goes back for review, or leaves the building undetermined.

    ANTHROPIC_API_KEY=... python3 src/enrich_web_use.py
    ... --bbl 4003770013            one building, prints its evidence
    ... --limit 50                  stop after 50 lookups
    ... --all                       re-check buildings Places already answered
    ... --corroborate               put the open Places claims to the web
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
OUTPUT_FILE = DATA_RAW / f"web_current_use_{date.today():%Y%m%d}.json"
CACHE_FILE = DATA_RAW / "web_current_use_cache.json"
CORROBORATE_FILE = DATA_RAW / f"web_corroboration_{date.today():%Y%m%d}.json"
CORROBORATE_CACHE = DATA_RAW / "web_corroboration_cache.json"

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# The model is the search client, not the judge -- see search(). Opus 5
# because the job is reading pages and quoting the right sentence out of
# them, and a miss here is silent: a passage never quoted is a rule that
# never fires, and the building comes back unconfirmed rather than wrong.
MODEL = os.environ.get("WEB_USE_MODEL", "claude-opus-5")

# Basic search on purpose, not for want of a newer one. web_search_20260209
# and _20260318 add dynamic filtering, which runs code to drop irrelevant
# results *before* they reach the context window. That is the right default
# for a chat answer and the wrong one here: this module wants the widest
# possible pool of quotable text, and a filter tuned for relevance to a
# question is not tuned for "any sentence naming what occupies this
# address". Basic search loads every result, which is what we are paying
# for. One line to change if that reasoning stops holding.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 4}

# What the model is for. Not "is this building a shelter" -- that question is
# answered by CLASSIFY_RULES and corroborate(), in Python, on text it did not
# write. It searches and it quotes; the rules decide. Asking it for a verdict
# would put a model's opinion in the evidence chain, which is the one thing
# this module was built not to do.
SEARCH_SYSTEM = (
    "You are a search front-end, not an analyst. Run web searches for the "
    "query you are given and then quote what you found.\n\n"
    "Quote generously and verbatim. For every page that mentions the "
    "address, quote the sentence that says what occupies the building -- its "
    "name, operator, use, or what it was converted into -- even when several "
    "pages say the same thing and even when a page looks unreliable. Quote "
    "sentences that argue the building is ordinary housing too.\n\n"
    "Do not judge, summarise, reconcile sources, or state a conclusion about "
    "what the building is. Do not say a source is wrong or irrelevant. "
    "Something downstream weighs these quotes; your job is to find them and "
    "reproduce them exactly as written."
)

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


# The first digit of a BBL. Derived rather than read off the feature: the
# build has never carried a `borough` property, so both modes below were
# putting an empty string into the query where the borough belongs — a
# search for "2508 BROADWAY  (shelter OR hostel OR ...)" with a double space
# where the one word that disambiguates a Manhattan address from a Brooklyn
# one should be. Neither mode had run when this was found.
BOROUGH_BY_DIGIT = {
    "1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens",
    "5": "Staten Island",
}


def borough_of(bbl: str) -> str:
    return BOROUGH_BY_DIGIT.get(str(bbl or "")[:1], "")


def log(msg: str) -> None:
    print(msg, flush=True)


_client = None


def client():
    """One Anthropic client, built on first use so imports stay cheap.

    Tests import this module for its pure functions and must not need a key
    on the machine running them.
    """
    global _client
    if _client is None:
        import anthropic
        # max_retries covers 429/5xx with backoff; the loop in search()
        # handles only what the SDK gives up on.
        _client = anthropic.Anthropic(api_key=API_KEY or None, max_retries=4,
                                      timeout=180.0)
    return _client


def results_from_message(message) -> list[dict]:
    """Pull title/snippet/link triples out of one Claude response.

    Two sources, deliberately both:

    Citations (`web_search_result_location`) carry `cited_text` -- up to 150
    characters of the source page, verbatim, which is the same shape of
    evidence a Programmable Search snippet was and is what the phrase rules
    read. These are the ones that can fire a rule.

    Search results (`web_search_result`) carry a title and url and no
    readable body at all; the page text comes back as `encrypted_content`,
    which the API decrypts for the model and never for us. They are included
    with an empty snippet because a title alone sometimes carries the whole
    answer -- "Jack Ryan Residence - Homeless Shelter Directory" satisfies
    both the phrase test and the address test -- and because dropping them
    would mean a page the model found but did not quote vanished without
    trace.

    Pure, and takes a message or a plain dict, so the extraction is tested
    without a key or a network.
    """
    if hasattr(message, "model_dump"):
        message = message.model_dump()
    out, seen = [], set()

    def add(title, link, snippet):
        key = (link, snippet)
        if not link or key in seen:
            return
        seen.add(key)
        out.append({"title": title or "", "snippet": snippet or "",
                    "link": link})

    for block in message.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            for c in block.get("citations") or []:
                if c.get("type") == "web_search_result_location":
                    add(c.get("title"), c.get("url"), c.get("cited_text"))
        elif kind == "web_search_tool_result":
            content = block.get("content")
            # An error comes back as a single object where results are a
            # list -- and as a 200, so nothing raised on the way here.
            if isinstance(content, dict):
                log(f"    web search error: {content.get('error_code')}")
                continue
            for r in content or []:
                if r.get("type") == "web_search_result":
                    add(r.get("title"), r.get("url"), "")
    return out


def search(query: str, retries: int = 3) -> list[dict]:
    """One web search through Claude. Returns title/snippet/link triples.

    The contract is the one Programmable Search had, so classify() and
    corroborate() are unchanged: the model is a search client here, and the
    rules downstream still decide what the results mean.
    """
    import anthropic

    for attempt in range(retries):
        try:
            messages = [{"role": "user", "content":
                         f"Search the web for: {query}"}]
            results, hops = [], 0
            while True:
                message = client().messages.create(
                    model=MODEL,
                    max_tokens=8000,
                    system=SEARCH_SYSTEM,
                    tools=[WEB_SEARCH_TOOL],
                    messages=messages,
                )
                results.extend(results_from_message(message))
                if message.stop_reason == "refusal":
                    log(f"    declined on {query[:50]!r}")
                    break
                # The server paused a long search turn. Send the assistant
                # turn back untouched -- encrypted_content has to survive
                # the round trip or the next request 400s.
                if message.stop_reason != "pause_turn" or hops >= 3:
                    break
                hops += 1
                messages.append({"role": "assistant",
                                 "content": message.content})

            # Dedup across hops; a paused turn repeats nothing but a
            # continuation can re-cite a page the first leg already gave us.
            seen, deduped = set(), []
            for r in results:
                key = (r["link"], r["snippet"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            return deduped

        except anthropic.APIStatusError as exc:
            # The SDK already retried 429 and 5xx. Reaching here means the
            # ceiling is not a per-second one -- an exhausted credit balance
            # or a disabled key -- and the next building will fail the same
            # way, so stop rather than burn the run finding that out 60 more
            # times. Results so far are already on disk.
            if exc.status_code in (401, 403):
                raise SystemExit(
                    f"  the Anthropic key was rejected ({exc.status_code}). "
                    "Check ANTHROPIC_API_KEY and that the workspace has "
                    "web search enabled; results so far are already written."
                )
            if exc.status_code == 429:
                raise SystemExit(
                    "  rate limited past the SDK's own retries. Wait and "
                    "resume -- the cache means a rerun costs only what it "
                    "has not already done."
                )
            if attempt == retries - 1:
                log(f"    HTTP {exc.status_code} on {query[:50]!r}")
                return []
            time.sleep(2 ** attempt)
        except anthropic.APIConnectionError as exc:
            if attempt == retries - 1:
                log(f"    {type(exc).__name__} on {query[:50]!r}")
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


# --- corroboration: putting a lone Places reading to the open web -----------

# Words that do not distinguish one institution from another. Stripped before
# a name is matched against a page, so "Advent Lutheran Church" is tested on
# "advent" and "lutheran" rather than on the word every church shares.
GENERIC_NAME_WORDS = frozenset({
    "the", "of", "and", "at", "for", "in", "on", "a", "an", "to",
    "inc", "incorporated", "llc", "ltd", "lp", "corp", "corporation", "co",
    "company", "nyc", "new", "york", "ny", "manhattan", "brooklyn", "queens",
    "church", "chapel", "parish", "temple", "school", "academy", "college",
    "hotel", "motel", "inn", "house", "building", "center", "centre",
    "institute", "foundation", "society", "association", "saint", "st",
})

# A page that argues the building is housing stock or that the named place is
# gone. Phrased long for the reason the table above is: "closed" on its own
# matches a closed street and an article about a closing, and "apartment"
# matches the one above the shop. Each of these describes a building.
CONTRADICTION_MARKERS = (
    "apartments for rent", "apartment for rent", "units for rent",
    "apartments for lease", "rental building", "no fee apartments",
    "condominium", "condo for sale", "co-op apartment", "cooperative apartment",
    "rent stabilized apartments", "residential condominium",
    "permanently closed", "closed its doors", "has since closed",
    "no longer operates", "no longer located", "relocated to", "has moved to",
    "formerly located at", "sits vacant", "stands vacant", "vacant building",
)


def distinctive_tokens(name: str) -> set[str]:
    """The words in a place's name that could only be this place."""
    words = re.findall(r"[a-z0-9']{2,}", (name or "").lower())
    return {w for w in words if w not in GENERIC_NAME_WORDS}


def names_the_place(blob: str, name: str) -> bool:
    """Does this page name the place Places put in the building.

    Two tokens, or one long enough to be a proper noun on its own. One short
    token is not enough: "Prep For Prep" reduces to {prep}, and a page about
    71st Street using the word prep is not evidence that Prep For Prep is the
    building. Buildings that fail this come back unconfirmed rather than
    wrong, which is the direction this whole module errs in.
    """
    low = blob.lower()
    full = " ".join(sorted(distinctive_tokens(name)))
    if not full:
        return False
    hits = {t for t in distinctive_tokens(name) if re.search(rf"\b{re.escape(t)}", low)}
    return len(hits) >= 2 or any(len(t) >= 8 for t in hits)


def corroborate(results: list[dict], addresses: list[str], claim_name: str) -> dict:
    """Does the open web agree that claim_name is at this address.

    Three verdicts, and the asymmetry between them is deliberate.
    A confirmation beats a contradiction whenever both appear, because a
    listings page for a flat at an address is near-universal and says nothing
    about the other ten floors, while a newsroom or a directory naming the
    institution at that address is about the building. Erring toward confirmed
    keeps a building off the prospect list, which costs a lead; erring the
    other way puts somebody in front of a church, which costs a day.

    A verdict of "none" is a real answer and not a failure: it means the web
    was asked and had nothing, which leaves the building undetermined.
    """
    confirming = contradicting = None
    for r in results:
        blob = f"{r['title']} {r['snippet']}"
        if not mentions_address(blob, addresses):
            continue
        host = urllib.parse.urlparse(r["link"]).netloc.lower()
        if names_the_place(blob, claim_name):
            confirming = confirming or (r, f"web names {claim_name!r} at the address, in {host}")
            continue
        # Somebody running the building under another name still confirms that
        # somebody is running it. The Places reading may be wrong about what
        # it is and right that the rooms are spoken for.
        other = classify([r], addresses)
        if other:
            confirming = confirming or (r, f"web reads the address as {other['current_use_label']} ({other['basis']})")
            continue
        low = blob.lower()
        hit = next((m for m in CONTRADICTION_MARKERS if m in low), None)
        if hit:
            contradicting = contradicting or (r, f"web:{hit!r} in {host}")

    chosen, verdict = (confirming, "confirmed") if confirming else (
        (contradicting, "contradicted") if contradicting else (None, "none"))
    if not chosen:
        return {"verdict": "none", "basis": "the web was asked and named nothing at this address",
                "evidence": []}
    r, basis = chosen
    return {
        "verdict": verdict,
        "basis": basis,
        "evidence": [{"title": r["title"], "snippet": r["snippet"], "link": r["link"]}],
    }


def load_corroboration_targets(only_bbl: str | None = None) -> list[dict]:
    """The buildings whose occupancy rests on a lone Places reading.

    Selected by the predicate in deal_readiness rather than by readiness_state,
    and the difference matters: once that module stops calling these occupied
    they are undetermined like any other unanswered building, and a selector
    keyed on the state would stop finding the very buildings it exists to
    resolve. The predicate is keyed on the evidence, which does not move.
    """
    sys.path.insert(0, str(ROOT))
    from src.deal_readiness import places_claim
    from src.enrich_current_use import load_alt_addresses, load_buildings

    nearby = {}
    files = sorted(DATA_RAW.glob("nearby_use_[0-9]*.json"), reverse=True)
    if files:
        nearby = {str(r["bbl"]): r for r in json.loads(files[0].read_text()) if r.get("bbl")}
        log(f"  Method 2 readings: {files[0].name}, {len(nearby)} rows")
    else:
        log("  no nearby_use file — only the sweep-occupant claims will be found")

    alts = load_alt_addresses()
    out = []
    for b in load_buildings():
        bbl = str(b.get("bbl") or "")
        if not bbl or (only_bbl and bbl != only_bbl):
            continue
        row = nearby.get(bbl, {})
        if not only_bbl and not places_claim(b, row):
            continue
        name = row.get("nearby_use_name") or b.get("current_use_name") or ""
        if not name:
            occs = b.get("current_use_occupants") or []
            name = next((o.get("name") for o in occs
                         if o.get("name") and o.get("use") not in
                         ("ground_floor_tenant", "unknown", "other")), "")
        addr = b.get("address") or ""
        out.append({
            "bbl": bbl,
            "address": addr,
            "borough": b.get("borough") or borough_of(bbl),
            "claim_name": name,
            "claim_basis": row.get("nearby_use_basis") or b.get("current_use_basis") or "",
            "addresses": [addr] + [a for a in alts.get(bbl, []) if a and a != addr],
        })
    return out


def run_corroboration(args) -> None:
    """Ask the web about each open claim and write the verdicts."""
    cache = json.loads(CORROBORATE_CACHE.read_text()) if CORROBORATE_CACHE.exists() else {}
    targets = load_corroboration_targets(args.bbl)
    if args.limit:
        targets = targets[: args.limit]
    log(f"  {len(targets)} building(s) occupied on a lone Places reading")

    rows, tally = [], {"confirmed": 0, "contradicted": 0, "none": 0}
    for i, t in enumerate(targets, 1):
        if not t["claim_name"]:
            # Nothing to search for. Left out of the output entirely so the
            # claim stays unscreened rather than being recorded as a miss.
            log(f"  [{i}/{len(targets)}] {t['address'][:34]:36s} -- no place name to check")
            continue
        if t["bbl"] in cache and not args.bbl:
            verdict = cache[t["bbl"]]
        else:
            verdict = None
            for addr in query_addresses(t["addresses"]):
                q = f'"{addr}" {t["borough"]} "{t["claim_name"]}"'
                v = corroborate(search(q), t["addresses"], t["claim_name"])
                if v["verdict"] != "none":
                    verdict = v
                    break
                verdict = v
            cache[t["bbl"]] = verdict
            if i % 25 == 0:
                CORROBORATE_CACHE.write_text(json.dumps(cache, indent=2))
            time.sleep(0.2)

        tally[verdict["verdict"]] = tally.get(verdict["verdict"], 0) + 1
        rows.append({"bbl": t["bbl"], "address": t["address"],
                     "claim_name": t["claim_name"], "claim_basis": t["claim_basis"],
                     **verdict})
        log(f"  [{i}/{len(targets)}] {t['address'][:34]:36s} -> "
            f"{verdict['verdict']:13s} {verdict['basis'][:48]}")
        if args.bbl:
            for e in verdict["evidence"]:
                log(f"        {e['link']}\n        {e['snippet'][:160]}")

    CORROBORATE_CACHE.write_text(json.dumps(cache, indent=2))
    merged = {}
    if CORROBORATE_FILE.exists():
        merged = {r["bbl"]: r for r in json.loads(CORROBORATE_FILE.read_text())}
    merged.update({r["bbl"]: r for r in rows})
    CORROBORATE_FILE.write_text(json.dumps(list(merged.values()), indent=2))

    log(f"\n  confirmed {tally['confirmed']} (stays occupied), "
        f"contradicted {tally['contradicted']} (back for review), "
        f"nothing found {tally['none']} (undetermined)")
    log(f"  wrote {CORROBORATE_FILE.relative_to(ROOT)} ({len(merged)} rows)")


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
            "borough": b.get("borough") or borough_of(bbl),
            "addresses": [addr] + [a for a in alts.get(bbl, []) if a and a != addr],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbl")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--corroborate", action="store_true",
                    help="put the open Places claims to the web instead of "
                         "reading a use for buildings Places could not see")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit(
            "Set ANTHROPIC_API_KEY.\n"
            "  This is not the Places key — the web screen moved off\n"
            "  Programmable Search, which could not search the whole web on\n"
            "  our account. Web search must also be enabled for the\n"
            "  workspace in the Claude console, or every call 400s.\n"
            "  Budget: $10 per 1,000 searches, plus tokens."
        )

    if args.corroborate:
        run_corroboration(args)
        return

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
