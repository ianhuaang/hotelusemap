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
    ("institutional_lodging", "Nonprofit / institutional lodging", (
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
TENANT_TYPES = frozenset({
    "restaurant", "cafe", "coffee_shop", "bar", "bakery", "meal_takeaway",
    "meal_delivery", "store", "clothing_store", "convenience_store",
    "grocery_store", "supermarket", "drugstore", "pharmacy", "gym",
    "fitness_center", "beauty_salon", "hair_salon", "nail_salon", "spa",
    "bank", "atm", "parking", "gas_station", "real_estate_agency",
    "insurance_agency", "travel_agency", "laundry", "night_club", "liquor_store",
    # Practitioners rent a suite; they do not define the building.
    "health", "doctor", "dentist", "dental_clinic", "physiotherapist", "medical_lab",
    "wellness_center", "chiropractor", "psychologist", "veterinary_care",
    "lawyer", "accounting", "consultant", "storage", "gift_shop",
    "deli", "sandwich_shop", "juice_shop", "ice_cream_shop", "florist",
})

# Tiers where a non-transient current use is a contradiction worth surfacing.
TRANSIENT_TIERS = ("legal_transient", "partial")
NON_TRANSIENT_USES = frozenset({
    "student_housing", "supportive_housing", "institutional_lodging",
    "religious", "medical", "government", "private_club", "education",
})


def classify(place: dict) -> tuple[str, str, str, str]:
    """Return (use, label, confidence, basis) for one Places result."""
    name = (place.get("displayName", {}).get("text") or "").lower()
    types = [t.lower() for t in place.get("types", [])]
    primary = (place.get("primaryType") or "").lower()
    type_set = set(types) | ({primary} if primary else set())

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


def address_match(query_addr: str, result_addr: str) -> str:
    """high / medium / low confidence that the result is the same building."""
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


def candidates(address: str, lat: float, lon: float) -> list[dict]:
    """Every address-verified occupant, classified but not yet ranked.

    Split from the ranking so the cache can hold candidates rather than a
    verdict. Re-ordering used to mean re-querying all 2,540 buildings; the
    first ranking change after the sweep cost exactly that, and handed 569
    Lexington back to a travel wholesaler in the process.
    """
    places = places_nearby(lat, lon)
    if not places:
        return []

    scored = []
    for p in places:
        match = address_match(address, p.get("formattedAddress", ""))
        if match == "low":
            continue
        use, label, conf, basis = classify(p)
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
        c = _centroid(f["geometry"]["coordinates"])
        if not c:
            continue
        r = dict(f["properties"])
        r["lon"], r["lat"] = c
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
    args = ap.parse_args()

    rows = load_buildings()
    targets = select_targets(rows, args.tiers.split(","), [b for b in args.bbl.split(",") if b])
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    uncached = [t for t in targets if f"{t['bbl']}|{t.get('address','')}" not in cache]

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

    results, new, hits, misses = [], 0, 0, 0
    for i, rec in enumerate(targets):
        bbl, addr = rec["bbl"], rec.get("address", "")
        if not addr:
            continue
        key = f"{bbl}|{addr}"

        # The cache holds candidates, never the verdict. Ranking is applied on
        # every run, so changing how the winner is chosen costs nothing.
        if key in cache:
            scored = cache[key]
        else:
            if args.limit and new >= args.limit:
                break
            try:
                scored = candidates(addr, rec["lat"], rec["lon"])
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
            continue
        hits += 1
        contradicts = (rec.get("tier") in TRANSIENT_TIERS
                       and found["current_use"] in NON_TRANSIENT_USES)
        results.append({
            "bbl": bbl, "address": addr, "tier": rec.get("tier", ""),
            "bldgclass": rec.get("bldgclass", ""),
            "hpd_class_b": rec.get("hpd_class_b", 0),
            "contradicts_tier": contradicts, **found,
        })

    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    OUTPUT_FILE.write_text(json.dumps(results, indent=2))

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
