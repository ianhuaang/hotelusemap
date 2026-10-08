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

An eleven-building screen against hand-verified buildings put a number on
that worry and found a larger one behind it. The quoting was not the weak
link; the query was. A rule can only fire on text the query surfaced, and the
query had drifted out of step with the table it feeds -- see USE_VOCABULARY.
The same run showed the second failure the rules could not see: a phrase
table cannot tell "this is a hotel" from "this was a hotel", and four of ten
readings rested on the building's past. Hence disposition_for(), and the rule
that only current evidence on a source about the building removes anything.

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

Both modes write a `disposition` alongside what they already wrote:

    remove      current evidence, on a source about the building, that
                something disqualifying occupies it
    confirm     corroborate() only -- the web named the Places claim itself
    flag        a reading a person should look at before anything acts on
                it: past-tense evidence, an untrusted host, or a
                contradiction
    no answer   the web was asked and had nothing

`verdict` is unchanged and still carries confirmed/contradicted/none, because
src/deal_readiness.py reads those. Nothing reads `disposition` yet.

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

# The model is the search client, not the judge -- see search(). Sonnet 5
# because the job is reading pages and quoting the right sentence out of
# them, and a miss here is silent: a passage never quoted is a rule that
# never fires, and the building comes back unconfirmed rather than wrong.
#
# This was Opus 5, on the argument that quoting the right sentence is worth
# the better reader. The eleven-building screen is what changed it: the one
# building the screen missed, 203 West 113 Street, was missed because the
# query never asked for supportive housing -- see USE_VOCABULARY -- and not
# because the model read the results badly. A model that is handed the wrong
# search results cannot quote its way out of them. Sonnet 5 at $3/$15 against
# Opus 5 at $5/$25, with the test set as the check on whether the quoting
# degrades.
MODEL = os.environ.get("WEB_USE_MODEL", "claude-sonnet-5")

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

# The search terms, in one place because they belong to the table above.
#
# A rule can only fire on text the query surfaced, so this list and
# CLASSIFY_RULES have to be read together — and they drifted. The query was
# written inline in two functions as
#
#     (shelter OR hostel OR dormitory OR "single room occupancy" OR club OR hotel)
#
# and when the supportive_housing category was added to the table for 203 West
# 113 Street, nothing added it here. The category existed, its twenty-two
# phrases existed, and the screen never searched for a word of it: the
# eleven-building run found both PropertyShark pages for the address, matched
# them, and had nothing to match against but the ZIP and the tax year. The
# building came back unanswered and the rule written for it never ran.
#
# Not every phrase in the table — a query is not a vocabulary dump, and the
# long clinical phrasings are there to survive a false positive once a page is
# in hand, which is a different job from finding the page. One or two of the
# most distinctive terms per category, and student_housing gains the two terms
# that actually appear in print ("dormitory" alone missed Monarch Heights,
# which surfaced on its title by luck).
USE_VOCABULARY = (
    'shelter OR hostel OR dormitory OR "student housing" OR "residence hall" '
    'OR "single room occupancy" OR "supportive housing" OR "transitional housing" '
    'OR "treatment program" OR club OR hotel'
)

# A hit inside these is about the building. A hit on a booking aggregator or a
# listings farm is about a page that mentions the address, and those pages
# describe whatever the building was whenever the page was written.
#
# The second group is the one the eleven-building screen added. Every verdict
# in that run came back `low` — not one source matched this list, because the
# list was all newsrooms and the pages that actually answer an address question
# are property records and development trades. newyorkyimby.com is a working
# NYC development newsroom; PropertyShark and CompStak are records; LandmarkWest
# is a research archive. Two sources are deliberately absent: pratt.edu, whose
# stale accommodations page is what read 229 Duffield Street as a hotel three
# years after the Webster Apartments bought it, and l3capital.com, an owner
# marketing page written in the past tense.
TRUSTED_HOST_HINTS = (
    "nyc.gov", "crainsnewyork.com", "commercialobserver.com", "therealdeal.com",
    "qns.com", "queenspost.com", "qgazette.com", "amny.com", "gothamist.com",
    "nytimes.com", "ny1.com", "brooklynpaper.com", "patch.com", "6sqft.com",
    "bisnow.com", "curbed.com", "cityandstateny.com", "nypost.com", "bkrea.com",
    "citylimits.org", "documentedny.com", "thecity.nyc",
    "newyorkyimby.com", "propertyshark.com", "landmarkwest.org",
    "compstak.com", "affordablehousing411.com",
)

# A building-class field, not a sentence about the building. PropertyShark and
# the DOF records print the class as a run of categories joined by slashes —
# "Hotel/Motel/Hostel/B&B" — and the hostel rule is tested before the hotel
# rule, so the Comfort Inn at 37-35 21 Street came back a hostel on a string
# that names four things it might be. Stripped before the phrases are matched:
# a classification code is not evidence of what occupies the building, whatever
# words it happens to contain.
CLASS_WORD = (
    r"(?:hotels?|motels?|hostels?|b&b|bed\s*&\s*breakfast|inns?|apartments?|"
    r"condos?|co-?ops?|offices?|retail|warehouses?|garages?|lofts?|"
    r"walk-?ups?|elevators?|dwellings?|stores?|factory|industrial|"
    r"one\s+family|two\s+family|multi-?family|mixed\s+use|vacant\s+land)"
)
CLASS_STRING_RE = re.compile(
    rf"\b{CLASS_WORD}(?:\s*/\s*{CLASS_WORD})+\b", re.I)

# Evidence that describes the building in the past. A hit here does not throw
# the reading away — it stops it short of removing the building and sends it to
# a person instead, because "this was a hotel" and "this is a hotel" are the
# same sentence to a phrase table and opposite answers to the question the list
# is asking. Four of the ten answered verdicts in the eleven-building run rested
# on evidence like this: the Hotel Commander at 240 West 73 Street (former), the
# restaurant and hotel at 893 Broadway (2015, pre-redevelopment), and the thirty
# years of transitional housing at 50 Nevins Street that preceded the building
# standing there now.
STALE_MARKERS = (
    "formerly", "former", "previously", "used to be", "once was", "once a",
    "was converted", "was occupied", "was a", "was the", "had been",
    "operated as", "operated the building", "replaced", "closed in",
    "no longer", "since closed", "renamed", "until 19", "until 20",
    "prior to", "originally",
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
                    # Cached across every building in a run. The breakpoint
                    # sits on the system block, and tools render before
                    # system, so the entry covers both — which is where the
                    # tokens are: the prompt is 702 characters and the
                    # web_search schema takes the prefix to ~3,000 tokens,
                    # over Sonnet 5's 1,024-token floor with room to spare.
                    # The query is in messages, after the breakpoint, so it
                    # varies without invalidating anything.
                    #
                    # This is the small half of the bill. The search results
                    # come back into context on the second hop at roughly
                    # 45,000 tokens a building and are unique to each query,
                    # so nothing can cache them; that is what basic search
                    # loading every result costs, and it is deliberate.
                    system=[{"type": "text", "text": SEARCH_SYSTEM,
                             "cache_control": {"type": "ephemeral"}}],
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


def strip_class_strings(text: str) -> str:
    """Drop building-class runs before the phrases are matched.

    See CLASS_STRING_RE. "Hotel/Motel/Hostel/B&B at 37-35 21st Street" is the
    assessor's category for the lot, not a sentence about what trades there,
    and it satisfies four rules at once.
    """
    return CLASS_STRING_RE.sub(" ", text)


def stale_evidence(text: str, phrase: str) -> str | None:
    """The past-tense marker attached to `phrase`, if there is one.

    Read around the matched phrase rather than over the whole result, because
    the two are routinely about different uses. "Former Hotel Sold for $34.75m
    ... Converted into Homeless Shelter" is the strongest current-use evidence
    the 35-02 37 Avenue case has, and a marker test over the whole string
    reads its "Former" — which belongs to the hotel the building stopped being
    — and flags the sentence that establishes it is a shelter now.

    So: a window before the phrase, where a qualifier would sit ("was occupied
    by a restaurant and hotel", "operated the building as an OMH-licensed
    transitional housing facility"), and a short one after, where a trailing
    one would ("Hotel Commander (former); the Tempo (current)").
    """
    low, want = text.lower(), phrase.lower()
    i = low.find(want)
    if i < 0:
        return None
    window = low[max(0, i - 60): i + len(want) + 25]
    return next(
        (m for m in STALE_MARKERS if re.search(rf"\b{re.escape(m)}", window)), None)


def disposition_for(trusted: bool, stale: str | None) -> str:
    """What a reading licenses: dropping the building, or a person looking.

    Only evidence that describes the building now, on a source that is about
    the building, removes it from the available list. Everything else that
    read as something is a flag — the reading is probably right and nothing
    downstream should act on "probably" without a person.

    Both halves are load-bearing and they catch different failures. The tense
    test catches 893 Broadway, where the sentence says the building *was*
    occupied by a hotel in 2015. The host test catches 229 Duffield Street,
    where the sentence has no tense problem at all — "Hotel Indigo Brooklyn
    229 Duffield Street" on a university's accommodations page — and is simply
    three years out of date. A tense test alone would have removed a building
    the Webster Apartments runs as housing.
    """
    return "remove" if (trusted and not stale) else "flag"


def classify(results: list[dict], addresses: list[str]) -> dict | None:
    """Read the results for a use, keeping what the reading rests on."""
    for use, label, transient_ok, phrases in CLASSIFY_RULES:
        for r in results:
            blob = strip_class_strings(f"{r['title']} {r['snippet']}")
            low = blob.lower()
            hit = next((p for p in phrases if p in low), None)
            if not hit:
                continue
            if not mentions_address(blob, addresses):
                continue
            host = urllib.parse.urlparse(r["link"]).netloc.lower()
            trusted = any(h in host for h in TRUSTED_HOST_HINTS)
            stale = stale_evidence(blob, hit)
            return {
                "current_use": use,
                "current_use_label": label,
                "transient_ok": transient_ok,
                "use_confidence": "medium" if trusted else "low",
                "disposition": disposition_for(trusted, stale),
                "stale_marker": stale,
                "basis": (f"web:{hit!r} in {host}"
                          + (f", past tense ({stale!r})" if stale else "")),
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

    `verdict` keeps its three values because src/deal_readiness.py reads them.
    `disposition` is the separate field, and it splits the confirming case in
    two, because the two halves are not the same answer:

        confirm   the web named the Places claim at the address
        remove    the web named something else running the building

    35-02 37 Avenue is why. The Places claim there is "Citi Bike: 37 Ave & 35
    St" — the dock at the kerb — and the web answers with a 125-room former
    hotel under a city shelter contract. Nothing corroborated the bike dock.
    Reporting that as `confirm` says the Places reading was checked and stood,
    when what happened is that the reading was useless and the building is
    disqualified for a reason Places never saw. Both outcomes keep the building
    off the prospect list, which is why the distinction survived this long
    unnoticed; they are different sentences to the person reading the column.
    """
    confirming = naming = contradicting = None
    for r in results:
        blob = f"{r['title']} {r['snippet']}"
        if not mentions_address(blob, addresses):
            continue
        host = urllib.parse.urlparse(r["link"]).netloc.lower()
        if names_the_place(blob, claim_name):
            naming = naming or (r, f"web names {claim_name!r} at the address, in {host}")
            continue
        # Somebody running the building under another name still answers the
        # question the list is asking. The Places reading may be wrong about
        # what it is and right that the rooms are spoken for.
        other = classify([r], addresses)
        if other:
            confirming = confirming or (
                r,
                f"web reads the address as {other['current_use_label']} ({other['basis']})",
                other["disposition"],
            )
            continue
        low = blob.lower()
        hit = next((m for m in CONTRADICTION_MARKERS if m in low), None)
        if hit:
            contradicting = contradicting or (r, f"web:{hit!r} in {host}")

    if naming:
        r, basis = naming
        verdict, disposition = "confirmed", "confirm"
    elif confirming:
        r, basis, disposition = confirming
        verdict = "confirmed"
    elif contradicting:
        r, basis = contradicting
        verdict, disposition = "contradicted", "flag"
    else:
        return {"verdict": "none", "disposition": "no answer",
                "basis": "the web was asked and named nothing at this address",
                "evidence": []}
    return {
        "verdict": verdict,
        "disposition": disposition,
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

    rows, tally = [], {"remove": 0, "flag": 0, "confirm": 0, "no answer": 0}
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
                # The claim name or any use vocabulary: corroborate() answers
                # on either, and a query for the name alone only ever surfaces
                # pages that already agree with Places.
                q = (f'"{addr}" {t["borough"]} '
                     f'("{t["claim_name"]}" OR {USE_VOCABULARY})')
                v = corroborate(search(q), t["addresses"], t["claim_name"])
                if v["verdict"] != "none":
                    verdict = v
                    break
                verdict = v
            cache[t["bbl"]] = verdict
            if i % 25 == 0:
                CORROBORATE_CACHE.write_text(json.dumps(cache, indent=2))
            time.sleep(0.2)

        tally[verdict["disposition"]] = tally.get(verdict["disposition"], 0) + 1
        rows.append({"bbl": t["bbl"], "address": t["address"],
                     "claim_name": t["claim_name"], "claim_basis": t["claim_basis"],
                     **verdict})
        log(f"  [{i}/{len(targets)}] {t['address'][:34]:36s} -> "
            f"{verdict['disposition']:10s} {verdict['basis'][:52]}")
        if args.bbl:
            for e in verdict["evidence"]:
                log(f"        {e['link']}\n        {e['snippet'][:160]}")

    CORROBORATE_CACHE.write_text(json.dumps(cache, indent=2))
    merged = {}
    if CORROBORATE_FILE.exists():
        merged = {r["bbl"]: r for r in json.loads(CORROBORATE_FILE.read_text())}
    merged.update({r["bbl"]: r for r in rows})
    CORROBORATE_FILE.write_text(json.dumps(list(merged.values()), indent=2))

    log(f"\n  remove {tally['remove']} (something else runs the building), "
        f"confirm {tally['confirm']} (the Places claim stood), "
        f"flag {tally['flag']} (needs a person), "
        f"no answer {tally['no answer']} (undetermined)")
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
                q = f'"{addr}" {t["borough"]} ({USE_VOCABULARY})'
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
