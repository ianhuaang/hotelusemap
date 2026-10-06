"""Ask Google Places what a building is being used for today.

The pipeline infers use from city records, which describe what a building is
permitted to be, not what it is. 569 Lexington Avenue carries 730 HPD Class B
units and tiers as legal_transient; it is a student dormitory. Certificates of
occupancy, HPD registrations and building class all agree with each other and
all miss it.

This asks a different question — "what is at this address right now?" — and
classifies the answer into the uses that matter for sourcing: student housing,
supportive housing, nonprofit institutional lodging, religious, medical. One
lookup covers every category of noise the regulatory review flagged, because
they share a root cause rather than a data source.

Two guards keep the answer honest:

  Address verification. A text search for an address returns the nearest
  matching business, not the building. The existing hotel-name cache has
  "60 PINE STREET" resolving to "Mint House 70 Pine by Kasa" — a real hotel,
  the wrong building. Candidates whose house number disagrees are rejected.

  Ground-floor tenants. The strongest Places result at a residential tower is
  often the bodega in its base. Retail and food types are labelled as tenants,
  never as the building's use.

Usage:
    GOOGLE_API_KEY=... python3 src/enrich_current_use.py --estimate
    GOOGLE_API_KEY=... python3 src/enrich_current_use.py --bbl 1013057501
    GOOGLE_API_KEY=... python3 src/enrich_current_use.py --limit 50
    GOOGLE_API_KEY=... python3 src/enrich_current_use.py --tiers legal_transient,partial
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED

CTX = ssl.create_default_context(cafile=certifi.where())
TODAY = date.today().strftime("%Y%m%d")
API_KEY = os.environ.get("GOOGLE_API_KEY", "")
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Metres from the footprint centroid. Wide enough to reach a tower's entrance,
# tight enough that address verification can throw out the neighbours it pulls
# in. At 569 Lexington a 45m circle returns ten places, of which five carry the
# building's own house number and one survives tenant suppression.
NEARBY_RADIUS_M = 45.0

CACHE_FILE = DATA_RAW / "google_current_use_cache.json"
OUTPUT_FILE = DATA_RAW / f"google_current_use_{TODAY}.json"
REVIEW_FILE = DATA_PROCESSED / f"current_use_review_{TODAY}.csv"

# Nearby Search Pro — the tier this field mask lands in, because displayName,
# types, formattedAddress, businessStatus and primaryTypeDisplayName are all
# Pro fields. Since March 2025 each SKU carries its own monthly free cap
# instead of the old shared $200 credit: Pro gets 5,000 calls a month free,
# then $32 per 1,000. Essentials (IDs only) is unlimited and free, Enterprise
# is 1,000 free then $35.
#
# 4,465 buildings therefore costs nothing, provided nothing else on the
# account spends Text Search Pro calls in the same month. The earlier hotel
# name runs were free for the same reason, not because the API is free.
COST_PER_1K_USD = 32.0
FREE_CALLS_PER_MONTH = 5000

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.primaryType",
    "places.primaryTypeDisplayName",
    "places.types",
    "places.formattedAddress",
    "places.businessStatus",
    "places.location",
])


# --- Use classification -----------------------------------------------------
#
# Ordered most specific first; the first rule that fires wins. Type hits are
# decisive because Google assigned them; name hits are suggestive because
# "Temple Court" is an office building and "The Mission" is a bar. A rule that
# fires on name alone is downgraded to medium and lands in the review file.

TYPE_RULES = [
    # Education is not student housing, and the two were conflated here at
    # first. Google types a tennis coach as `school`, which turned "Ana
    # Gabriela Canahuate Torres - Tennis Coach" into student accommodation.
    # An education type now means education; student housing is established
    # by name alone, because a residential building for students says so.
    ("education", "School / university", (
        "university", "college", "school", "primary_school", "secondary_school",
    )),
    # Whole-building care only. An individual practitioner is a tenant with a
    # suite, no different from the barber downstairs, and "Lai Victor T MD"
    # was being read as the use of a Financial District tower.
    ("medical", "Medical / care facility", (
        "hospital", "nursing_home", "assisted_living_facility",
        "rehabilitation_center", "hospice",
    )),
    ("religious", "Religious institution", (
        "church", "synagogue", "mosque", "hindu_temple", "place_of_worship",
    )),
    ("government", "Government / civic", (
        "city_hall", "local_government_office", "government_office",
        "courthouse", "police", "fire_station", "embassy", "post_office",
    )),
    ("hostel", "Hostel", (
        "hostel",
    )),
    ("hotel", "Hotel", (
        "hotel", "motel", "resort_hotel", "extended_stay_hotel", "inn",
        "bed_and_breakfast", "guest_house", "budget_japanese_inn",
        "japanese_inn", "private_guest_room", "lodging", "cottage", "farmstay",
    )),
    ("residential", "Residential", (
        "apartment_building", "apartment_complex", "condominium_complex",
        "housing_complex",
    )),
    ("office", "Office", (
        "corporate_office", "office",
    )),
]

NAME_RULES = [
    ("student_housing", "Student housing", (
        "residence hall", "residence halls", "dormitory", " dorm", "dorms",
        "student housing", "student residence", "student living",
        "university housing", "college house", "educational housing",
        "hall of residence", "international house",
        # Operator brands, because the category is the one place where the
        # name reliably fails to describe the use. 569 Lexington trades as
        # "FOUND Study Midtown East" — no street, no university, and not the
        # word student anywhere in it. A brand list is brittle and needs
        # adding to; it is still the only thing that reads these buildings.
        "found study", "vita student", "outpost club", "tripalink",
        "ehs ", "the statesman", "nest student", "student castle",
        "scape ", "unite students", "yugo ", "hines student",
    )),
    ("supportive_housing", "Supportive / transitional housing", (
        "supportive housing", "transitional housing", "safe haven",
        "drop-in center", "single room occupancy", "housing development fund",
        "hdfc", "breaking ground", "common ground", "project renewal",
        "homeless", "shelter", "women's residence", "mens shelter",
        "men's shelter", "halfway house", "recovery residence",
    )),
    ("institutional_lodging", "Nonprofit / institutional lodging", (
        "ymca", "ywca", "salvation army", "seafarer", "retreat center",
        "bowery mission", "catholic charities", "boys club", "girls club",
    )),
    # Found while auditing the licence split: the New York Athletic Club,
    # Harvard Club, Knickerbocker, Lotos, Cosmopolitan and New York Yacht Club
    # all register Class B rooms and all tier as legal_transient. Member
    # sleeping rooms are transient capacity on paper and unbuyable in practice.
    # The review did not name this category; the data did.
    ("private_club", "Private membership club", (
        "athletic club", "yacht club", "country club", "university club",
        "social club", "private club", "harvard club", "princeton club",
        "cornell club", "penn club", "yale club", "players club",
        "union league", "racquet club", "explorers club", "knickerbocker club",
        "lotos club", "cosmopolitan club", "friars club", "century association",
    )),
    ("religious", "Religious institution", (
        "church", "synagogue", "cathedral", "parish", "convent", "monastery",
        "rectory", "diocese", "archdiocese", "chabad", "yeshiva", "mosque",
        "watchtower", "jehovah", "ministries", "congregation",
    )),
    ("medical", "Medical / care facility", (
        "hospital", "medical center", "nursing home", "rehabilitation",
        "hospice", "assisted living", "senior living", "adult care",
    )),
    ("education", "School / university", (
        "university", "college", "yeshiva", "seminary", "academy",
    )),
]

# A restaurant at the base of a 40-storey tower says nothing about the tower.
# Google types that carry no information about who is in the building.
# A listing whose name is a street address is Google holding a pin on a door.
# It carries no information about what is inside, and it was carrying plenty:
# 360 of them, of which "20 Broad" and "55 Wall Street" were classified
# residential and four more as hotels — so an address pin was setting the
# building's use and reporting it as established.
#
# Matched on shape rather than against this building's own addresses, which is
# what the app was doing: 182 11th Avenue listed "182 11th Ave" and "184 11th
# Ave", and only the first was its own.
BARE_ADDRESS = re.compile(r"""
    ^\s*\d+[A-Za-z]?(\s*[-\u2013]\s*\d+)?\s+
    (E|W|N|S|East|West|North|South)?\s*\d*\s*
    [A-Za-z0-9.\s']*?\s*
    (st|street|ave|avenue|av|blvd|boulevard|rd|road|pl|place|dr|drive|ct|court|
     ln|lane|pkwy|parkway|ter|terrace|way|sq|square|slip|row|broadway|bowery)\.?
    (\s+(N|S|E|W|North|South|East|West))?\s*$
""", re.I | re.X)


def _is_bare_address(name: str) -> bool:
    return bool(BARE_ADDRESS.match((name or "").strip()))


# Types whose listing agrees with registered transient rooms. Read off
# TYPE_RULES rather than written out again, so a type added to the hotel or
# hostel rule is corroborating here without anybody remembering to say so.
CORROBORATING_TYPES = frozenset(
    t for use, _label, types in TYPE_RULES if use in ("hotel", "hostel") for t in types
)


NO_INFORMATION_TYPES = frozenset({
    "premise", "subpremise", "point_of_interest", "establishment",
    "geocode", "street_address", "route", "political",
})

TENANT_TYPES = frozenset({
    # Street-level trades the list was missing. Each one was reaching
    # _occupancy_state as a building occupant: "service" alone accounted for
    # 283 of them, and it is the type behind the bike shop at 859 7th Avenue.
    "service", "art_gallery", "finance", "event_venue",
    "performing_arts_theater", "transportation_service",
    "electric_vehicle_charging_station", "massage", "massage_spa",
    "restaurant", "cafe", "coffee_shop", "bar", "bakery", "meal_takeaway",
    "meal_delivery", "store", "clothing_store", "convenience_store",
    "grocery_store", "supermarket", "drugstore", "pharmacy", "gym",
    "fitness_center", "beauty_salon", "hair_salon", "nail_salon", "spa",
    "bank", "atm", "parking", "gas_station", "real_estate_agency",
    "insurance_agency", "travel_agency", "laundry", "night_club", "liquor_store",
    # Street furniture and landmarks stand near a building, not in it. Matching
    # against every alternate address let these through — a Citi Bike dock
    # outside 35-02 37 Avenue became the building's use, and bus stops, a
    # skate park and the Wall of New Amsterdam did the same elsewhere. They
    # were never suppressed before because the stricter address test happened
    # to reject them for the wrong reason.
    "tourist_attraction", "historical_landmark", "cultural_landmark", "landmark",
    "monument", "sculpture", "park", "plaza", "skateboard_park", "dog_park",
    "bus_stop", "transit_station", "train_station", "subway_station",
    "light_rail_station", "bike_sharing_station", "bicycle_rental",
    # Practitioners rent a suite; they do not define the building.
    "health", "doctor", "dentist", "dental_clinic", "physiotherapist", "medical_lab",
    "wellness_center", "chiropractor", "psychologist", "veterinary_care",
    "lawyer", "accounting", "consultant", "storage", "gift_shop",
    "deli", "sandwich_shop", "juice_shop", "ice_cream_shop", "florist",
})

# Tiers where a non-transient current use is a contradiction worth surfacing.
TRANSIENT_TIERS = ("legal_transient", "partial")
# Which occupants mean the building is not a sourcing target.
#
# Narrowed on the team's instruction: they want to see SROs, hostels, members
# clubs and dormitories, and not shelters. All four were being treated the same
# as a church or a clinic — flagged as a conflict, penalised 35 points and
# pushed off the list — when a dormitory or a members club is a building with a
# tenant to negotiate out, not a building that cannot be a hotel.
#
# So student_housing, private_club and hostel come out. What stays is the set
# nobody is converting: shelters and transitional housing, and the civic uses.
# institutional_lodging stays with them because it is now missions and charity
# lodging only — Bowery Mission, Salvation Army — with hostels moved out.
#
# Surfaced is not the same as unflagged. The occupant is still named in the
# building's Current use section; it simply no longer decides the building's
# fate on its own.
NON_TRANSIENT_USES = frozenset({
    # The team's designation, September 2026: SROs, hostels, members clubs and
    # dormitories stay on the list — they are legally transient stock with a
    # tenant — and shelters come off it. A shelter is established by the web
    # source, never by Places, which has no listing for one.
    "shelter",
    "supportive_housing", "institutional_lodging",
    "religious", "medical", "government", "education",
})


def restate(row: dict) -> bool:
    """Re-read a stored sweep row under the current rules. True if it changed.

    A stored row keeps the place Google returned but not the reading we made
    of it, because the rules move. Splitting hostels out of institutional
    lodging left 15 of them still labelled "Nonprofit / institutional
    lodging" — a use the score penalises — months after the team asked to be
    shown hostels. The place is data; the classification is a derivation, and
    it is derived again every time it is used.
    """
    if not (row.get("primary_type") or row.get("google_name")):
        return False
    use, label, confidence, basis = classify({
        "displayName": {"text": row.get("google_name") or ""},
        "primaryType": row.get("primary_type") or "",
        "types": row.get("types") or [],
        "businessStatus": row.get("business_status") or "",
        # A stored row carries the building it came from, so a re-read knows
        # whether a door pin's type agrees with the register.
    }, class_b=int(row.get("hpd_class_b") or 0))
    if use == row.get("current_use"):
        return False
    # Only ever a correction, never an erasure. A stored row that carries a
    # name but no place type re-derives to "unknown", and letting that land
    # would quietly blank a reading that was made when the full place was in
    # hand. Restating is for a rule that changed, not for data we no longer
    # hold.
    if use in ("", "unknown") and row.get("current_use") not in ("", "unknown"):
        return False
    row["current_use"] = use
    row["current_use_label"] = label
    row["use_confidence"] = confidence
    row["basis"] = basis
    return True


def classify(place: dict, class_b: int = 0) -> tuple[str, str, str, str]:
    """Return (use, label, confidence, basis) for one Places result.

    class_b is the building's registered transient rooms, and it is here for
    one purpose: to tell a door pin that agrees with the register from one
    that argues with it. Zero when the caller has no building to hand, which
    is the old behaviour exactly.
    """
    raw_name = place.get("displayName", {}).get("text") or ""
    name = raw_name.lower()
    types = [t.lower() for t in place.get("types", [])]
    primary = (place.get("primaryType") or "").lower()
    type_set = set(types) | ({primary} if primary else set())

    # Before anything else. A pin on a door tells us nothing, and the type is
    # often wrong in the flattering direction — "20 Broad" came back
    # residential, and 57 more pins carry condominium or apartment types that
    # would read as the building's use if they were believed.
    #
    # Unless the register already says the same thing. 138 Bowery is 48
    # registered Class B rooms and an operating hotel, and its listing is
    # named after the door it sits on, so this discarded a type=hotel on a
    # building whose own HPD registration corroborates it — then reported
    # "Unknown", and the segment chain read no operator. A BD person googles
    # the address and sees a hotel in ten seconds.
    #
    # So the test is corroboration, not trust: a lodging type survives only
    # where the city has registered transient rooms to agree with it.
    #
    # An apartment type on those same rooms is still discarded, and the reason
    # matters because it is easy to state wrongly in either direction.
    #
    # It is not that apartments are impossible there. Nor is it that Class B
    # rooms may simply be run as apartments: under the Multiple Dwelling Law a
    # Class A unit shall only be used for permanent residence, and moving
    # rooms from Class B to Class A means filed plans and a new certificate of
    # occupancy. It is a conversion, not a choice made week to week.
    #
    # It is that a single listing named after a door cannot tell us which of
    # three things it found. A lawful conversion that has been through plans
    # and a new C of O. An unlawful occupancy of rooms still registered as
    # transient. Or a noisy pin that means nothing at all. Those have very
    # different consequences for somebody sourcing the building, and nothing
    # in one Places result separates them.
    #
    # So the honest output is unknown. Not "apartments", which asserts the
    # first; not "transient", which asserts against all three.
    if _is_bare_address(raw_name) and not (class_b > 0 and type_set & CORROBORATING_TYPES):
        return "unknown", "Unknown", "low", "name is a street address"

    # primaryType is Google's own answer to "what is this place"; the types
    # array is a bag that collects strays — a doctor's listing carrying
    # "school" is enough to misfile the building if the bag is trusted
    # equally. Decide on primaryType when there is one, and only fall back
    # to the bag when there is not.
    for pass_set in ([primary] if primary else [], type_set):
        if not pass_set:
            continue
        hit_found = False
        for use, label, keys in TYPE_RULES:
            hit = set(pass_set).intersection(keys)
            if hit:
                hit_found = True
                break
        if hit_found:
            break
    else:
        pass_set = set()

    for use, label, keys in TYPE_RULES:
        hit = set(pass_set).intersection(keys)
        if hit:
            # A name that contradicts the type wins — Google types a dorm run by
            # a university as "university", but types many of them as "lodging".
            for n_use, n_label, n_keys in NAME_RULES:
                if n_use in ("student_housing", "supportive_housing") and any(k in name for k in n_keys):
                    return n_use, n_label, "high", f"type={sorted(hit)[0]} + name"
            return use, label, "high", f"type={sorted(hit)[0]}"

    for use, label, keys in NAME_RULES:
        matched = next((k for k in keys if k in name), None)
        if matched:
            return use, label, "medium", f"name~{matched.strip()}"

    if type_set & TENANT_TYPES:
        return "ground_floor_tenant", "Ground-floor tenant only", "low", f"type={sorted(type_set & TENANT_TYPES)[0]}"

    # Types that describe Google's index rather than an occupant: "premise"
    # means the listing is an address, "point of interest" a pin with no
    # category, "establishment" that it is a business and nothing more. They
    # used to land in "other", which the occupancy state read as a finding —
    # so a building whose only hit was its own street address reported as
    # established.
    if primary in NO_INFORMATION_TYPES:
        return "unknown", "Unknown", "low", f"type={primary}"

    if primary:
        return "other", place.get("primaryTypeDisplayName", {}).get("text", primary), "low", f"type={primary}"

    return "unknown", "Unknown", "low", "no types"


# --- Address verification ---------------------------------------------------

_STREET_ABBREV = {
    "AVENUE": "AVE", "STREET": "ST", "ROAD": "RD", "BOULEVARD": "BLVD",
    "PLACE": "PL", "DRIVE": "DR", "COURT": "CT", "LANE": "LN",
    "TERRACE": "TER", "PARKWAY": "PKWY", "SQUARE": "SQ", "HIGHWAY": "HWY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_ORDINAL = re.compile(r"\b(\d+)(ST|ND|RD|TH)\b")


def _normalize_street(text: str) -> str:
    out = re.sub(r"[.,]", " ", (text or "").upper())
    out = _ORDINAL.sub(r"\1", out)
    words = [_STREET_ABBREV.get(w, w) for w in out.split()]
    return " ".join(words)


def _house_number(text: str) -> str:
    m = re.match(r"\s*(\d+)", text or "")
    return m.group(1) if m else ""


def address_match(query_addr, result_addr: str) -> str:
    """high / medium / low confidence that the result is the same building.

    query_addr may be a list. A building is often known by more than one
    address and the city files it under whichever it likes: 35-02 37 Avenue is
    37-06 36 Street to DOB, so a business standing in it and formatted under
    36 Street failed the street test and was thrown out. Matching against every
    known address fixes that; the best result wins.
    """
    if isinstance(query_addr, (list, tuple, set)):
        ranks = {"high": 0, "medium": 1, "low": 2}
        best = "low"
        for one in query_addr:
            got = address_match(one, result_addr)
            if ranks[got] < ranks[best]:
                best = got
        return best

    q_num, r_num = _house_number(query_addr), _house_number(result_addr)
    q_street = _normalize_street(query_addr)
    r_street = _normalize_street(result_addr)

    # Street tokens after the house number, e.g. "569 LEXINGTON AVE" -> {LEXINGTON, AVE}
    q_tokens = set(q_street.split()[1:]) if q_num else set(q_street.split())
    r_tokens = set(r_street.split())
    street_ok = bool(q_tokens) and q_tokens.issubset(r_tokens)

    if not street_ok:
        return "low"
    if q_num and r_num:
        return "high" if q_num == r_num else "low"
    return "medium"


# --- Footprint containment -------------------------------------------------
#
# The honest test of "is this business in this building" is whether it stands
# inside the building. House-number matching was a proxy for that, and a poor
# one: the Wythe Hotel occupies 75 North 11th Street and registers as 80 Wythe
# Avenue, so the proxy threw it out. Its coordinates sit inside the footprint.
#
# 177 hotel names were rejected on the house-number test alone. The footprint
# is already in the GeoJSON we read, so none of that had to be lost.

# How far outside the outline a pin may sit and still be the building.
#
# Google pins a business to its street entrance, which is on the pavement — a
# few metres outside the DOB polygon. Requiring a pin strictly inside the
# outline therefore rejected the building's own occupants: at 156 Tillary
# Street every one of the ten places the probe returned was thrown away, the
# Hampton Inn among them, because its pin sits 12.8m off the edge and Google
# files it under 125 Flatbush Ave Ext, an address the city's own alt-address
# list does not carry. The building read as never checked.
#
# 15m is drawn from that case: the whole complex sits at 12.3-12.8m, and the
# nearest genuinely different buildings are at 20.3m. Containment still beats a
# buffered hit in ranking, so this widens what is considered, not what wins.
FOOTPRINT_BUFFER_M = 15.0


def _metres_from_footprint(lon: float, lat: float, coordinates: list) -> float:
    """Shortest distance from a point to any outer ring, in metres."""
    import math
    best = float("inf")
    mlon = 111320.0 * math.cos(math.radians(lat))
    for poly in coordinates or []:
        for ring in (poly or [])[:1]:
            for i in range(len(ring) - 1):
                ax = (ring[i][0] - lon) * mlon
                ay = (ring[i][1] - lat) * 111320.0
                bx = (ring[i + 1][0] - lon) * mlon
                by = (ring[i + 1][1] - lat) * 111320.0
                dx, dy = bx - ax, by - ay
                t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
                best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def near_footprint(lon: float, lat: float, coordinates: list) -> bool:
    """Inside the outline, or within a doorway's distance of it."""
    if in_footprint(lon, lat, coordinates):
        return True
    return _metres_from_footprint(lon, lat, coordinates) <= FOOTPRINT_BUFFER_M


def in_footprint(lon: float, lat: float, coordinates: list) -> bool:
    """Ray casting over each polygon's outer ring."""
    for poly in coordinates:
        if not poly:
            continue
        ring = poly[0]
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        if inside:
            return True
    return False


# --- Places call ------------------------------------------------------------

def places_nearby(lat: float, lon: float) -> list[dict]:
    """Everything Places knows inside a small circle on the building.

    Nearby rather than text search, because a text search for an address
    returns the address. "569 Lexington Avenue, New York, NY" resolves to a
    premise — Google's record of the postal address — and stops there, while
    the student housing occupying the building sits in the same dataset under
    a name that contains neither the street nor the word student. Asking what
    is at a point returns occupants; asking about an address returns the
    address.
    """
    body = json.dumps({
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon},
                       "radius": NEARBY_RADIUS_M}
        },
        "maxResultCount": 20,
    }).encode()
    req = urllib.request.Request(
        NEARBY_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=20) as resp:
                return json.loads(resp.read()).get("places", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (TimeoutError, urllib.error.URLError):
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return []


def places_text(query: str) -> list[dict]:
    """Ask by address as well as by point.

    places_nearby answers "what is at this spot", which is the right question
    and an incomplete one. It only returns what Google has geocoded inside a
    45m circle on the footprint centroid, so it misses an occupant pinned to
    another corner of a large building, and it misses anything filed under one
    of the building's other addresses entirely.

    That is not hypothetical. 35-02 37 Avenue is filed five ways, is the former
    Paper Factory Hotel and is now a shelter, and the point probe returned one
    thing: a Citi Bike dock. 156 Tillary Street, 145 registered rooms in
    Downtown Brooklyn, returned nothing at all.

    A text search for a bare address does resolve to the postal premise, which
    is why this supplements the point probe rather than replacing it — but it
    also returns establishments Google files at that address, which is the half
    that was missing.
    """
    body = json.dumps({"textQuery": query, "maxResultCount": 20}).encode()
    req = urllib.request.Request(
        SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=20) as resp:
                return json.loads(resp.read()).get("places", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if e.code == 400:
                return []
            raise
        except (TimeoutError, urllib.error.URLError):
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return []


# A venue trades under a name; a company registers one. "Hotel Beds USA Inc"
# is a travel wholesaler with an office at 569 Lexington, and Google types it
# `hotel` — indistinguishable from a real hotel by type, and by name too
# unless the suffix is read. Real hotels almost never trade as "X Inc".
_CORPORATE_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|ltd|limited|lp|l\.p|"
    r"holdings|partners|associates|management|realty|properties)\.?$",
    re.I,
)


def _is_corporate_entity(name: str) -> bool:
    return bool(_CORPORATE_SUFFIX.search((name or "").strip().rstrip(".")))


# Google types that name a building rather than something inside one. This is
# the distinction the first ranking got backwards: it promoted the distinctive
# categories, on the theory that a church can only mean a church. But a church
# can also be a congregation renting the third floor, and a traffic court can
# sit inside an office block. Sorted the other way round, the building says
# what it is and everything else is a tenant.
#
# 20 Exchange Place carries "Twenty Exchange", an apartment_complex, and a
# tennis coach Google types `school`. 50 West Street carries "50 West Condo"
# and a doctor. 10 South Street carries Casa Cipriani, a working hotel, and a
# medical clinic. In each case the building was on the list all along.
BUILDING_IDENTITY_TYPES = frozenset({
    "apartment_building", "apartment_complex", "condominium_complex",
    "housing_complex", "corporate_office", "business_center",
    "hotel", "motel", "resort_hotel", "extended_stay_hotel", "inn",
    "bed_and_breakfast", "guest_house", "lodging", "hostel",
})


def _names_the_building(place: dict) -> bool:
    primary = (place.get("primaryType") or "").lower()
    return primary in BUILDING_IDENTITY_TYPES


# Within the tenants, a category that can only describe a whole occupancy
# still beats a shop. This ordering only decides among places that are NOT
# building-identity, so it never overrules the building itself.
_USE_PRIORITY = {
    "student_housing": 0, "supportive_housing": 0, "institutional_lodging": 0,
    "religious": 1, "medical": 1, "private_club": 1, "education": 2,
    "government": 2,
    "hotel": 3, "residential": 3, "office": 4,
    "other": 5, "ground_floor_tenant": 6, "unknown": 7,
}


def candidates(address, lat: float, lon: float, footprint: list = None,
               class_b: int = 0) -> list[dict]:
    """Every address-verified occupant, classified but not yet ranked.

    Split from the ranking so the cache can hold candidates rather than a
    verdict. Re-ordering used to mean re-querying all 2,540 buildings; the
    first ranking change after the sweep cost exactly that, and handed 569
    Lexington back to a travel wholesaler in the process.
    """
    places = places_nearby(lat, lon)

    # Every address the building is filed under, asked by name as well as by
    # point. `address` already arrives as the full list from main(); it was
    # only ever used to verify a match, never to look one up, which left the
    # multi-address buildings answering with whatever happened to sit within
    # 45m of their centroid.
    queries = [address] if isinstance(address, str) else list(address or [])
    seen = {p.get("id") for p in places if p.get("id")}
    for q in queries:
        if not q:
            continue
        for p in places_text(f"{q}, New York, NY"):
            pid = p.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                places.append(p)

    if not places:
        return []

    scored = []
    for p in places:
        loc = p.get("location") or {}
        has_loc = footprint and loc.get("longitude") is not None
        strict = bool(has_loc and in_footprint(loc["longitude"], loc["latitude"], footprint))
        # Admitted on the buffered test, ranked on the strict one.
        contained = bool(strict or (has_loc and near_footprint(loc["longitude"], loc["latitude"], footprint)))
        match = address_match(address, p.get("formattedAddress", ""))
        # Standing in the building settles it. The house number is the
        # fallback for the handful of places Google has not located precisely.
        if contained:
            match = "high"
        elif match == "low":
            continue
        use, label, conf, basis = classify(p, class_b=class_b)
        scored.append({
            "place_id": p.get("id", ""),
            "google_name": p.get("displayName", {}).get("text", ""),
            "formatted_address": p.get("formattedAddress", ""),
            "primary_type": p.get("primaryType", ""),
            "primary_type_display": p.get("primaryTypeDisplayName", {}).get("text", ""),
            "types": p.get("types", []),
            "business_status": p.get("businessStatus", ""),
            "address_match": match,
            "current_use": use,
            "current_use_label": label,
            "use_confidence": conf,
            "basis": basis,
            "names_building": _names_the_building(p),
            "in_footprint": strict,
            "near_footprint": contained,
        })

    return scored


def rank_candidates(scored: list[dict], places_seen: int = 0) -> dict | None:
    """Pick the occupant that describes the building."""
    if not scored:
        return None
    places = range(places_seen or len(scored))

    # A circle drawn on a Manhattan block catches the neighbours and the whole
    # retail parade in the base, so ordering decides the answer. Exact house
    # number first, then anything that describes the building over a shop
    # inside it, then confidence. At 569 Lexington that walks past the
    # Benjamin Royal Sonesta next door, a deli, a barbershop, a supermarket
    # and a gyro counter to reach FOUND Study Midtown East.
    rank = {"high": 0, "medium": 1}
    # Corporate demotion comes first, ahead of the building test. "Hotel Beds
    # USA Inc" is typed `hotel`, which makes it look like a building-identity
    # record, and putting the building test first handed 569 Lexington back to
    # a travel wholesaler. A registered company is never the building, whatever
    # Google types it.
    ranked = sorted(scored, key=lambda r: (
        rank[r["address_match"]],
        _is_corporate_entity(r["google_name"]),
        not r["names_building"],
        _USE_PRIORITY.get(r["current_use"], 9),
        {"high": 0, "medium": 1, "low": 2}[r["use_confidence"]],
    ))
    at_address = [r for r in scored if r["address_match"] == "high"] or scored

    # Everything at the address, kept whole. A tower has ten businesses in it
    # and no single one of them is the answer; a person reading the panel can
    # see what is actually there, which is what was asked for.
    best = dict(ranked[0])
    best["occupants"] = [
        {"name": r["google_name"], "type": r["primary_type"], "use": r["current_use"]}
        for r in at_address[:8]
    ]
    best["candidates_at_address"] = len(at_address)
    best["candidates_seen"] = len(places)

    # Two different building-level uses at one address is not something to
    # resolve by ranking. Say so and let a person look.
    building_uses = {r["current_use"] for r in ranked
                     if r["names_building"]
                     or _USE_PRIORITY.get(r["current_use"], 9) == 0}
    best["needs_review"] = len(building_uses) > 1
    return best


# --- Driver -----------------------------------------------------------------

def load_alt_addresses() -> dict:
    """BBL -> every address the building is filed under.

    Produced by pull_alt_addresses.py, which is already in the refresh
    sequence, and read by build_geojson for display. This step never looked at
    it, which is why 539 buildings — 83% of them carrying more than one
    address — came back with no occupant at all.
    """
    files = sorted(DATA_PROCESSED.glob("alt_addresses_*.json"), reverse=True)
    if not files:
        print("  no alt_addresses file — run src/pull_alt_addresses.py first, "
              "or buildings filed under a second address will not resolve")
        return {}
    return json.loads(files[0].read_text())


def load_buildings() -> list[dict]:
    """Buildings with geometry, since the lookup now needs coordinates.

    The built GeoJSON rather than the pipeline JSON: it is the only artefact
    carrying footprints, and its 2,615 features are already the set that
    reaches the map, which is the set worth spending lookups on.
    """
    files = sorted(DATA_PROCESSED.glob("buildings_*.geojson"), reverse=True)
    if not files:
        sys.exit("No buildings_*.geojson in data/processed/ — run src/build_geojson.py")
    print(f"Buildings: {files[0].name}")
    g = json.loads(files[0].read_text())

    rows = []
    for f in g["features"]:
        # Two buildings have no geometry at all: DOB has no footprint for them
        # and no geocode could be trusted, so build_geojson keeps them with a
        # null geometry rather than inventing a location. This read straight
        # through to ["coordinates"] and crashed the whole sweep on them — so
        # since that data landed, no sweep has run at all.
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords:
            continue
        # A geocoded point rather than a footprint: usable for the probe, but
        # there is no outline to test containment against.
        if geom.get("type") == "Point":
            r = dict(f["properties"])
            r["lon"], r["lat"] = coords[0], coords[1]
            r["footprint"] = None
            rows.append(r)
            continue
        c = _centroid(coords)
        if not c:
            continue
        r = dict(f["properties"])
        r["lon"], r["lat"] = c
        r["footprint"] = coords
        rows.append(r)
    return rows


def _centroid(coordinates: list) -> tuple[float, float] | None:
    xs = ys = 0.0
    n = 0
    for poly in coordinates:
        for ring in poly:
            for pt in ring:
                xs += pt[0]
                ys += pt[1]
                n += 1
    return (xs / n, ys / n) if n else None


def select_targets(rows: list[dict], tiers: list[str], bbls: list[str]) -> list[dict]:
    if bbls:
        return [r for r in rows if r.get("bbl") in set(bbls)]
    return [r for r in rows if r.get("tier") in set(tiers) and r.get("address")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tiers", default="legal_transient,partial",
                    help="comma-separated tiers to look up")
    ap.add_argument("--bbl", default="", help="comma-separated BBLs, overrides --tiers")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new lookups")
    ap.add_argument("--estimate", action="store_true",
                    help="report the target count and API cost, call nothing")
    ap.add_argument("--rescue", action="store_true",
                    help="re-query cached buildings that produced no occupant, "
                         "or only a ground-floor tenant, using the address "
                         "searches as well as the point probe")
    args = ap.parse_args()

    rows = load_buildings()
    targets = select_targets(rows, args.tiers.split(","), [b for b in args.bbl.split(",") if b])
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    # A cached answer that describes nothing about the building is not an
    # answer. Before the address searches existed these were the buildings the
    # point probe could not reach — a Citi Bike dock at 35-02 37 Avenue, and
    # nothing at all at 156 Tillary — and they are the ones worth paying to ask
    # again. Everything else stays cached and costs nothing.
    def thin(entry, class_b=0):
        for c in (entry or []):
            use, _, _, _ = classify({
                "displayName": {"text": c.get("google_name", "")},
                "types": c.get("types") or [],
                "primaryType": c.get("primary_type") or "",
                "primaryTypeDisplayName": {"text": c.get("primary_type_display", "")},
            }, class_b=class_b)
            if use not in ("", "ground_floor_tenant", "unknown", "other"):
                return False
        return True

    def needs_lookup(t):
        key = f"{t['bbl']}|{t.get('address','')}"
        if key not in cache:
            return True
        return args.rescue and thin(cache[key], int(t.get("hpd_class_b") or 0))

    uncached = [t for t in targets if needs_lookup(t)]

    print(f"Targets: {len(targets)}  cached: {len(targets) - len(uncached)}  to look up: {len(uncached)}")
    billable = max(0, len(uncached) - FREE_CALLS_PER_MONTH)
    cost = billable * COST_PER_1K_USD / 1000
    if billable:
        print(f"Estimated cost: ${cost:,.2f} — {len(uncached)} calls, "
              f"{FREE_CALLS_PER_MONTH:,} free this month, {billable:,} billable "
              f"at ${COST_PER_1K_USD:.0f}/1k")
    else:
        print(f"Estimated cost: $0.00 — {len(uncached)} calls fits inside the "
              f"{FREE_CALLS_PER_MONTH:,}/month Nearby Search Pro free cap")
        print("  (assumes nothing else on the account spends Pro calls this month)")
    if args.estimate:
        return
    if not API_KEY:
        sys.exit("Set GOOGLE_API_KEY")

    alt_addresses = load_alt_addresses()

    results, new, hits, misses = [], 0, 0, 0
    for i, rec in enumerate(targets):
        bbl, addr = rec["bbl"], rec.get("address", "")
        known = [addr] + [a for a in alt_addresses.get(bbl, []) if a and a != addr]
        if not addr:
            continue
        key = f"{bbl}|{addr}"

        # The cache holds candidates, never the verdict. Ranking is applied on
        # every run, so changing how the winner is chosen costs nothing.
        if key in cache and not (args.rescue and thin(cache[key],
                                                      int(rec.get("hpd_class_b") or 0))):
            # Re-classify on read. The cache is meant to hold candidates rather
            # than verdicts, but classification was being baked in at write
            # time, so a change to TYPE_RULES or TENANT_TYPES reached only
            # buildings that had never been looked up. Suppressing Citi Bike
            # docks and bus stops changed nothing until this did. The cached
            # fields carry primary_type and types, so it costs no API call.
            scored = [dict(c) for c in cache[key]]
            for c in scored:
                use, label, conf, basis = classify({
                    "displayName": {"text": c.get("google_name", "")},
                    "types": c.get("types") or [],
                    "primaryType": c.get("primary_type") or "",
                    "primaryTypeDisplayName": {"text": c.get("primary_type_display", "")},
                }, class_b=int(rec.get("hpd_class_b") or 0))
                c["current_use"], c["current_use_label"] = use, label
                c["use_confidence"], c["basis"] = conf, basis
        else:
            if args.limit and new >= args.limit:
                break
            try:
                scored = candidates(known, rec["lat"], rec["lon"], rec.get("footprint"),
                                class_b=int(rec.get("hpd_class_b") or 0))
            except Exception as e:
                print(f"  ERROR {addr}: {e}")
                continue
            cache[key] = scored
            new += 1
            time.sleep(0.15)
            if new % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache, indent=2))
                print(f"  {new} new lookups ({hits} identified, {misses} no match)")

        found = rank_candidates(scored or [])
        if not found:
            misses += 1
            # A row all the same. Emitting nothing made a swept building
            # indistinguishable from an unswept one downstream, and 183
            # buildings were reading as "never checked" when most of them had
            # been asked about and simply come back empty.
            results.append({
                "bbl": bbl, "address": addr, "tier": rec.get("tier", ""),
                "bldgclass": rec.get("bldgclass", ""),
                "hpd_class_b": rec.get("hpd_class_b", 0),
                "contradicts_tier": False,
                "swept": True, "current_use": "", "current_use_label": "",
                "google_name": "", "use_confidence": "", "basis": "no match",
                "occupants": [],
            })
            continue
        hits += 1
        contradicts = (rec.get("tier") in TRANSIENT_TIERS
                       and found["current_use"] in NON_TRANSIENT_USES)
        results.append({
            "bbl": bbl, "address": addr, "tier": rec.get("tier", ""),
            "bldgclass": rec.get("bldgclass", ""),
            "hpd_class_b": rec.get("hpd_class_b", 0),
            "contradicts_tier": contradicts, "swept": True, **found,
        })

    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    # Merged onto the previous run, not written over it. enrich.py reads the
    # newest dated file, so a targeted run — one BBL, or a --limit that stops
    # early — used to publish a file holding only the buildings it touched, and
    # every other building silently lost its current use on the next build. A
    # one-BBL test wrote a one-row file over a 2,451-row sweep.
    previous = {}
    prior_files = sorted(DATA_RAW.glob("google_current_use_[0-9]*.json"), reverse=True)
    for f in prior_files:
        if f == OUTPUT_FILE:
            continue
        try:
            previous = {r["bbl"]: r for r in json.loads(f.read_text()) if r.get("bbl")}
        except (json.JSONDecodeError, OSError):
            continue
        break
    # A carried row keeps the place Google returned but not the reading we made
    # of it, because the rules move. Splitting hostels out of institutional
    # lodging left 15 of them still labelled "Nonprofit / institutional
    # lodging" — a use the score penalises — months after the team asked to be
    # shown hostels. The place data is stored; the classification is derived
    # again from it every time.
    restated = sum(restate(row) for row in previous.values())
    if restated:
        print(f"  Re-read {restated} carried rows under the current rules")

    merged = {**previous, **{r["bbl"]: r for r in results if r.get("bbl")}}
    kept = len(merged) - len(results)
    if kept > 0:
        print(f"Carried {kept:,} buildings forward from {prior_files[0].name if prior_files else 'nothing'}")
    OUTPUT_FILE.write_text(json.dumps(list(merged.values()), indent=2))

    flagged = [r for r in results if r["contradicts_tier"]]
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    cols = ["bbl", "address", "tier", "bldgclass", "hpd_class_b", "google_name",
            "current_use", "current_use_label", "use_confidence", "basis",
            "address_match", "primary_type", "formatted_address"]
    with REVIEW_FILE.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(flagged, key=lambda r: -int(r.get("hpd_class_b") or 0)))

    from collections import Counter
    print(f"\nIdentified: {hits}   no verified match: {misses}   new API calls: {new}")
    print("By current use:")
    for use, n in Counter(r["current_use"] for r in results).most_common():
        print(f"  {use:24} {n}")
    print(f"\nContradicts its tier: {len(flagged)} buildings -> {REVIEW_FILE}")
    print(f"Saved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
