"""Join pipeline output to building footprints -> final GeoJSON for the map.

Pure and re-runnable. Reads from data/raw/ and data/processed/, writes to data/processed/.
"""

import json
import math
import re
from datetime import date, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED, is_hotel_class
from src import provenance

TODAY = date.today().strftime("%Y%m%d")

# Manhattan community districts + target BK/QN districts → neighborhood names
CD_TO_NEIGHBORHOOD = {
    "101": "Financial District / Tribeca",
    "102": "Greenwich Village / SoHo",
    "103": "Lower East Side / Chinatown",
    "104": "Chelsea / Hudson Yards",
    "105": "Midtown West",
    "106": "Midtown East / Murray Hill",
    "107": "Upper West Side",
    "108": "Upper East Side",
    "109": "Morningside Heights / Harlem",
    "110": "Central Harlem",
    "111": "East Harlem",
    "112": "Washington Heights / Inwood",
    "301": "Williamsburg / Greenpoint",
    "302": "Downtown Brooklyn / Fort Greene",
    "306": "Park Slope / Red Hook",
    "401": "Astoria / Long Island City",
}


BRANDED_CHAINS = [
    "marriott", "sheraton", "westin", "w hotel", "w new york",
    "st. regis", "st regis", "ritz-carlton",
    "courtyard", "residence inn", "springhill", "fairfield", "aloft", "moxy",
    "ac hotel", "le meridien", "four points", "autograph collection",
    "tribute portfolio", "renaissance", "delta hotels",
    "hilton", "hampton", "doubletree", "embassy suites", "homewood", "home2",
    "waldorf", "conrad", "canopy", "curio", "tapestry", "tru by hilton",
    "hyatt", "andaz", "thompson", "grand hyatt", "park hyatt", "hyatt place",
    "hyatt house", "hyatt centric", "caption by hyatt",
    "ihg", "intercontinental", "holiday inn", "crowne plaza", "indigo",
    "even hotel", "staybridge", "candlewood",
    "wyndham", "ramada", "days inn", "super 8", "howard johnson", "travelodge",
    "wingate", "baymont", "la quinta", "tryp",
    "best western",
    "accor", "novotel", "sofitel", "ibis", "fairmont", "raffles", "swissotel",
    "choice", "comfort inn", "comfort suites", "quality inn", "clarion",
    "sleep inn", "econo lodge", "rodeway", "cambria",
    "four seasons", "mandarin oriental", "peninsula", "aman",
    "rosewood", "langham", "lotte", "shangri-la",
    "radisson", "park inn", "country inn",
    "citizenm", "pod hotel", "yotel", "moto",
    "kimpton", "viceroy", "dream hotel", "1 hotel", "1hotel",
    "standard hotel", "the standard", "ace hotel",
    "virgin hotel", "hard rock", "riu", "meliá", "melia",
    "arlo", "dream downtown", "dream midtown",
    "baccarat", "taj hotel", "the pierre", "pendry", "equinox hotel",
    "the hoxton", "the quin", "the dominick", "mondrian",
    "plaza athénée", "plaza athenee", "the mark", "the lowell", "the surrey",
    "park lane hotel", "aka hotel", "aka central", "aka times",
    "the new yorker hotel", "the london", "the muse",
    "the benjamin", "millennium", "m social", "omni",
    "warwick", "sonesta", "westgate", "club quarters",
    "eurostars", "citadines", "ascend collection",
    "nh collection", "nh hotel", "executive hotel", "staypineapple",
    "pod 39", "pod 51", "pod times", "pod brooklyn",
    "park central", "generator", "luma hotel",
    # Private members' clubs
    "soho house", "athletic club", "yacht club", "harvard club",
    "yale club", "knickerbocker club", "lotos club", "cosmopolitan club",
    "hilton club", "club wyndham", "marriott vacation club",
]

MANUAL_BRANDED_BBLS = {
    "1012747504",  # 768 5 Ave — The Plaza (Fairmont/Accor), operator shows condo mgmt
}


# Hotels that closed after the 9 Dec 2021 special-permit cutoff, which the panel
# draws in red as a reversion opportunity.
#
# All of this was hand-typed prose with no source, presented as established.
# Cross-checking against the DOF sale and ACRIS deed this pipeline already pulls
# found one figure simply wrong: 371 7th Avenue was written as $255M where the
# city records $260M.
#
# Each entry now says what would corroborate it. `sale_price` is the figure city
# records must show; the build compares it to the DOF sale on the record and
# marks the entry verified or not, every run. Entries claiming only a closure
# carry None -- a hotel closing is not a recorded transaction, nothing here can
# confirm it, and the panel must stop implying otherwise.
POST_2021_REVERSIONS = {
    "1001060017": {
        "former_hotel": "Hampton Inn Manhattan-Seaport",
        "closure_year": 2023,
        "note": "Sold Dec 2023 to Slate Property Group for $24.1M. Hotel closed prior to sale.",
        "sale_price": 24_125_000,
        "source": "DOF rolling sales, corroborated by the ACRIS deed on this BBL",
    },
    # Lot 0071 became billing lot 7505 when the building was condominiumised,
    # which is what a conversion does to a tax lot and is why this entry
    # matched nothing for as long as anyone read the count as six. 130 East
    # 39th Street, RC, 15 floors.
    "1008947505": {
        "former_hotel": "W New York - The Court (St. Giles)",
        "closure_year": 2020,
        "note": "Closed during pandemic ~2020. Sold Jan 2023 for $50M. Currently migrant shelter.",
        "sale_price": 50_000_000,
        "source": "DOF rolling sales, corroborated by the ACRIS deed on this BBL",
    },
    # Lot 0034 became billing lot 7504 on the same conversion the note below
    # describes. 234 East 46th Street, R4, 20 floors.
    "1013197504": {
        "former_hotel": "AKA United Nations",
        "closure_year": 2024,
        # The only sale on record is unit-sized, consistent with a condo
        # conversion and not evidence of one.
        "note": "Converted to The Perrie condominiums (~95 units). Post-2021 conversion.",
        "sale_price": None,
        "source": None,
    },
    "1008060076": {
        "former_hotel": "Stewart Hotel",
        "closure_year": 2022,
        # Was "$255M"; city records say $260M.
        "note": "Closed 2022, used as migrant shelter. Acquired Dec 2025 for $260M per city records. Reported as a Slate + Breaking Ground purchase converting to 579 affordable apartments.",
        "sale_price": 260_000_000,
        "source": "DOF rolling sales, corroborated by the ACRIS deed on this BBL",
    },
    "1010167501": {
        "former_hotel": "Row NYC",
        "closure_year": 2025,
        "note": "Last NYC migrant hotel, closed Aug 2025. 1,332 rooms. Conversion status TBD — may reopen as hotel or convert to residential.",
        "sale_price": None,
        "source": None,
    },
    "1010487502": {
        "former_hotel": "Hudson Hotel",
        "closure_year": 2020,
        "note": "Closed Nov 2020 during COVID. 959 rooms. Slated for conversion to 438 below-market apartments.",
        "sale_price": None,
        "source": None,
    },
}


EXCLUDED_BLDG_CLASSES = {
    "E1", "E9",  # warehouse
    "F2",        # factory
    "G1", "G2", "G6", "G7",  # garage
    "V1",        # vacant land
    "T9",        # transportation
    "Q1",        # outdoor recreation
    "J4",        # market
    "U0",        # utility
    "Z9",        # miscellaneous
    "R5",        # apartment hotel (residential co-ops, not hotel targets)
}

NON_TARGET_KEYWORDS = [
    # Educational
    "university", "college", "school", "academy", "seminary", "institute",
    "yeshiva", "dormitor",
    "nyu ", "nyu hospitals", "cuny", "suny", "f i t ",
    # Shelters / homeless
    "homeless", "shelter",
    # HDFC / supportive housing
    "housing development fund", "hdfc", "supportive housing",
    "common ground", "breaking ground",
    # Religious / charitable
    "salvation army", "bowery mission", "ymca", "ywca",
    # Medical
    "hospital", "nursing home",
]

NON_TARGET_SAFE_WORDS = ["hospitality"]


INSTITUTIONAL_BLDG_CLASSES = {"I4", "I7", "I9", "N2", "N4", "N9", "M2", "M4", "M9", "P3", "P5", "W5", "W6", "W7"}


FLEX_OPERATORS = (
    "sonder", "placemakr", "kasa", "mint house", "blueground",
    "sentral", "whyhotel", "locale", "frontdesk", "landing",
)
# Deliberately excludes purpose-built extended-stay hotel brands (Residence
# Inn, Element, Hyatt House). Those sell hotel product; these take over
# apartment inventory, which is the distinction that matters for sourcing.


def _current_flex_operator(record: dict) -> str:
    """Name of a flex-stay operator running this building now, if any."""
    hotel = (record.get("hotel_name") or "").strip()
    # operator_name is only evidence of the present when something in the
    # present produced it. enrich.py falls back to the prior-operator ground
    # truth when Google, DCWP and HPD all come up empty, stamping
    # operator_source "ground_truth" -- a record that a flex company USED to be
    # here. Reading that name back turned every departure into an arrival, and
    # is why 20 Broad Street reported Sonder as operating months after they
    # left. Google's own sweep of that address finds Los Tacos, Blue Bottle and
    # PLG, and no Sonder.
    operator = "" if record.get("operator_source") == "ground_truth" else \
        (record.get("operator_name") or "").strip()
    # A live DCWP licence in a flex operator's name is present-tense evidence
    # and was not being read. 37 West 24 Street is licenced to "Sonder Henri
    # on 24", issued May 2025 and Ready for Renewal, while hotel_name still
    # carries "Wyndham Garden Manhattan Chelsea West" from a pull dated 2
    # September and Google's sweep finds "Hotel Henri NY". Sonder is the
    # operator now — the sequence runs Wyndham Garden, then Hotel Henri, then
    # Sonder — and the panel called them a prior operator, which is the
    # opposite of true.
    #
    # Only while the licence is live. A surrendered one is exactly the
    # departure the prior-operator branch is for.
    licenced = (record.get("hotel_license_name") or "").strip()
    if record.get("hotel_license_status") not in ("Active", "Ready for Renewal"):
        licenced = ""

    # These are usually the same string; only join the ones that differ.
    seen, parts = set(), []
    for candidate in (hotel, operator, licenced):
        key = candidate.lower()
        if candidate and key not in seen:
            seen.add(key)
            parts.append(candidate)
    # Whichever part is the flex operator is the name to show — joining a
    # Wyndham to a Sonder would read as one operator with a double-barrelled
    # name.
    for part in parts:
        if _is_flex_name(part):
            return part
    return ""


def _is_flex_name(name: str) -> bool:
    """Is this the name of a flex-stay operator, as the list defines one?

    The current-operator path has always asked this. The former-operator path
    did not, and took whatever the ground truth held — so the exclusion three
    lines above, which names Residence Inn specifically, was written and then
    bypassed. 19 of the 38 buildings wearing the Flex operators badge were
    former Residence Inns, LuxUrbans, AKAs, a Yotel and a Citadines: hotel
    product and serviced apartments, none of them a flex operator taking over
    apartment inventory.

    has_prior_op is untouched. A building that used to be a hotel is still a
    building that used to be a hotel; it is just not a flex one.
    """
    return any(op in (name or "").lower() for op in FLEX_OPERATORS)


# Whether anybody has established what is inside, decided once and published.
#
# This lived in the browser as occupancyKey, and the map's own filter had to
# restate it as a MapLibre expression — a second implementation of one rule,
# which this file has already paid for twice. It cannot be restated a third
# time anyway: the residential-class test is a regex and MapLibre has no
# regex. So it is computed here and read there.
#
# The state it replaces called 269 buildings unknown. 239 of them are
# registered multiple dwellings with a managing agent, or carry residential
# units in PLUTO, or hold a C of O with a dwelling-unit count — the records
# answered the question and nothing was asking them. Of the remaining 30,
# nearly all are RC and its siblings: condominium billing lots, which register
# per unit rather than per lot, so the class is the answer.
RESIDENTIAL_CLASS_PREFIX = re.compile(r"^(R[A-Z0-9]|[ABCD]\d)")


def _city_records_know_use(record: dict) -> str:
    """What the records say is inside, or "" if they say nothing."""
    if (record.get("hpd_class_a") or 0) > 0 or (record.get("hpd_class_b") or 0) > 0:
        return "HPD registers dwelling units here"
    if (record.get("unitsres") or 0) > 0:
        return "PLUTO records residential units here"
    if record.get("coo_dwelling_units"):
        return "the certificate of occupancy carries a dwelling-unit count"
    if record.get("hpd_managing_agent"):
        return "HPD registers a managing agent for it"
    if RESIDENTIAL_CLASS_PREFIX.match(str(record.get("bldgclass") or "").upper()):
        return "the building class is residential"
    return ""


def _occupancy_state(record: dict) -> str:
    """occupied | clear | onrecord | thin | unchecked."""
    if record.get("current_use_conflict"):
        return "occupied"
    occupants = record.get("current_use_occupants") or []
    if any(o.get("use") not in ("ground_floor_tenant", "unknown") for o in occupants):
        return "clear"
    if _city_records_know_use(record):
        return "onrecord"
    return "thin" if record.get("current_use_checked") else "unchecked"


# Nothing is dropped any more. A building that one of these rules would have
# removed is kept and marked with which rule and why, because a building that
# is absent cannot be searched for, linked to, or argued with — and the only
# way anyone found the licensed hotels the zoning rule was eating was by
# reading the pipeline by hand.
#
# The rules themselves are unchanged: what was a filter is now a label.
# Work on the guest rooms themselves, filed recently. This is the one signal
# that separates a hotel shut for refurbishment from a hotel nobody is coming
# back to, and the two were indistinguishable: 2 Lexington Avenue is the
# Gramercy Park Hotel, closed since 2020, and it sat in "Transient, no
# operator" at a legal score of 80 while MCR spent $13.2m putting floors 3-16
# back together.
#
# Deliberately narrow. The obvious wording — anything mentioning a hotel or a
# lobby — catches 83 buildings, most of them blocks of flats redoing an
# entrance. Rooms or an explicit change to hotel use catches 10, nine of which
# are already trading and doing normal upkeep, leaving exactly one that the
# segment was describing wrongly.
GUESTROOM_WORK = re.compile(
    r"guest ?rooms?|key count|hotel rooms?|convert.{0,30}\bhotel\b|\bhotel\b.{0,20}convert",
    re.I)
WORKS_LOOKBACK_MONTHS = 18


def _guestroom_works(record: dict) -> dict | None:
    """The largest recent permit that is work on the rooms, or None."""
    cutoff = (date.today() - timedelta(days=WORKS_LOOKBACK_MONTHS * 30)).isoformat()
    best = None
    for q in record.get("permits") or []:
        when = q.get("action_date") or ""
        if when < cutoff or not GUESTROOM_WORK.search(q.get("description") or ""):
            continue
        if best is None or (q.get("cost") or 0) > (best.get("cost") or 0):
            best = q
    if best is None:
        return None
    return {
        "cost": best.get("cost") or 0,
        "filed_on": best.get("action_date") or "",
        "description": (best.get("description") or "")[:200],
        "status": best.get("status") or "",
    }


# Something about this building, other than the code the city stamps on the
# lot, points at transient use.
#
# "Possibly transient" was 1,769 buildings of which 1,542 carried exactly one
# reason code — bldg_class_RM, or RC, or RD. That family sometimes contains
# transient use, which is why it is pulled in, and a block of flats sharing a
# code with a hotel is not a lead. On by default, it was most of what the map
# showed on open: ~1,990 dots of which maybe 448 meant anything.
#
# Class B rooms and R-1 occupancy are deliberately absent from this list —
# a building carrying either is legal_transient and never reaches the partial
# branch. J-1 is what actually distinguishes these: 124 of the 227.
def _has_transient_evidence(record: dict) -> bool:
    return bool(
        (record.get("hpd_class_b") or 0) > 0
        or record.get("dob_has_r1")
        or record.get("dob_has_j1")
        or record.get("has_hotel_license")
        or record.get("hotel_name")
        or (record.get("permit_transient_strong") or 0) > 0
    )


def _set_out_of_scope(records, test, code, reason):
    """Mark the records `test` selects. Returns how many were marked."""
    n = 0
    for r in records:
        if r.get("out_of_scope"):
            continue          # first rule to catch it owns the explanation
        if test(r):
            r["out_of_scope"] = code
            r["out_of_scope_reason"] = reason
            n += 1
    return n


def _fallback_address(record: dict, alt_addresses: dict) -> str:
    """An address for a building PLUTO did not name, from what else we hold."""
    for alt in alt_addresses.get(record["bbl"], []):
        if str(alt or "").strip():
            return str(alt).strip()
    return ""


def _is_non_target(record: dict) -> bool:
    bldg_class = record.get("bldgclass", "")
    if bldg_class in EXCLUDED_BLDG_CLASSES:
        return True

    is_institutional_class = bldg_class in INSTITUTIONAL_BLDG_CLASSES

    combined = " ".join([
        record.get("ownername", ""),
        record.get("operator_name", ""),
        record.get("managing_agent", ""),
    ]).lower()
    if any(safe in combined for safe in NON_TARGET_SAFE_WORDS):
        combined = combined.replace("hospitality", "")
    has_keyword = any(kw in combined for kw in NON_TARGET_KEYWORDS)

    if not is_institutional_class and not has_keyword:
        return False

    if record.get("has_hotel_license"):
        return False
    hotel = (record.get("hotel_name") or "").lower()
    if hotel and not any(kw in hotel for kw in NON_TARGET_KEYWORDS):
        return False
    return True


def _is_condo(record: dict) -> bool:
    """Condominium, read off the tax lot rather than guessed from a name.

    Finance gives every condominium unit a billing lot numbered 7501 or above,
    and PLUTO carries that lot. It agrees with the R building-class family
    almost exactly — 3,584 R-class buildings in the pipeline, every one on a
    7501+ lot — so either test alone would do, and together they cover the
    handful the other would miss.

    This replaces a substring search for "CONDO" in the owner name, which read
    SECONDO and CONDOR REALTY LLC as condominiums, and a class list of R1, R2
    and R4 that missed RM, RC, RD, RH and eleven other R codes. The old rule
    found 1,118 condos; this finds 3,585, including 1335 Avenue of the
    Americas and 1535 Broadway, two of the largest buildings we hold.
    """
    bbl = str(record.get("bbl") or "")
    if len(bbl) == 10 and bbl[-4:].isdigit() and int(bbl[-4:]) >= 7501:
        return True
    bldgclass = (record.get("bldgclass") or "").upper()
    # RS is single room occupancy, the one R code that is not a condominium.
    return bldgclass.startswith("R") and bldgclass != "RS"


def _is_branded(hotel_name: str, operator_name: str = "", bbl: str = "") -> bool:
    if bbl in MANUAL_BRANDED_BBLS:
        return True
    combined = f"{hotel_name} {operator_name}".lower()
    if not combined.strip():
        return False
    return any(brand in combined for brand in BRANDED_CHAINS)


# --- HTC union roster -------------------------------------------------------

# 60m. The roster geocodes each shop to its public entrance, which drifts from
# the tax lot centroid on corner lots and through-block buildings. Tuned on the
# 2026-09-21 roster against 2,615 buildings:
#
#     40m  223 matched   29 converted-use   5 disagreements
#     60m  237 matched   38 converted-use   6
#     75m  238 matched   39 converted-use   6
#    100m  242 matched   42 converted-use   7
#
# 60m buys nine more converted-use buildings — the ones that matter — for one
# extra disagreement, and it flattens after that. "Disagreements" counts shops
# whose name carries a street number differing from the matched building's,
# which over-reports: "1 Hotel Central Park" and "123 Washington Residences"
# are names, not addresses. Of the 140 shops still unmatched, 96 sit more than
# 2km from any building we hold — New Jersey, Long Island, upstate casinos —
# and are out of scope rather than mismatched.
HTC_MATCH_METRES = 60.0

# Degrees to metres at NYC's latitude.
_M_PER_DEG_LON = 84000.0
_M_PER_DEG_LAT = 111000.0

# A union shop in one of these is a building whose labour agreement outlived
# its hotel operation — the cohort the tool otherwise reads as free capacity.
HTC_CONVERTED_SHOP_TYPES = frozenset({
    "Residence", "Club", "Shelter", "Office", "Restaurant", "Audio Visual",
})

# The shop types that describe the whole building rather than a tenant in it.
# Narrower than HTC_CONVERTED_SHOP_TYPES on purpose: a union restaurant or an
# audio-visual shop is a business inside a building and says nothing about
# whether the building still lets rooms. The Rainbow Room is a Restaurant shop
# in 30 Rockefeller Plaza, and reading it as the building's use would have
# called 30 Rock a restaurant.
HTC_BUILDING_SHOP_TYPES = frozenset({"Residence", "Club", "Shelter"})

# What a shop type is called when it is shown to a person.
HTC_SHOP_TYPE_LABELS = {
    "Residence": "Residential building",
    "Club": "Private membership club",
    "Shelter": "Shelter",
    "Office": "Office",
    "Restaurant": "Restaurant",
    "Audio Visual": "Production facility",
}

# Tiers where a non-transient current use is a contradiction worth surfacing.
# Mirrors enrich_current_use.TRANSIENT_TIERS.
HTC_TRANSIENT_TIERS = ("legal_transient", "partial")


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    """Ray cast. Ring is a GeoJSON linear ring of [lon, lat] pairs."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            denom = (y2 - y1) or 1e-12
            if x < (x2 - x1) * (y - y1) / denom + x1:
                inside = not inside
    return inside


def point_in_footprint(geometry: dict, lon: float, lat: float) -> bool:
    """Is this coordinate inside the building itself?

    False for anything that is not an area. Buildings DOB has no footprint for
    carry a Point or no geometry at all, and neither has an inside — a Point's
    coordinates are two floats, which this read as a ring and tried to take the
    length of.
    """
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return False
    coords = geometry.get("coordinates") or []
    polys = coords if geometry.get("type") == "MultiPolygon" else [coords]
    for poly in polys:
        if poly and _point_in_ring(lon, lat, poly[0]):
            return True
    return False


def load_htc_union() -> list[dict]:
    """Load the HTC union roster, newest file wins."""
    files = sorted(DATA_RAW.glob("htc_union_*.json"), reverse=True)
    if not files:
        return []
    return json.loads(files[0].read_text())


def _centroid(geometry) -> tuple[float, float] | None:
    """A representative point for a feature's geometry, whatever shape it is.

    Takes the geometry rather than its coordinates because there are three
    cases now: a MultiPolygon footprint, a Point for a building DOB has no
    footprint for, and null for one that could not be placed at all. The last
    two arrived with the footprint-less buildings and crashed this on a
    NoneType subscript.
    """
    if not geometry:
        return None
    if geometry.get("type") == "Point":
        lon, lat = geometry.get("coordinates") or (None, None)
        return (lon, lat) if lon is not None else None
    xs, ys, n = 0.0, 0.0, 0
    for poly in geometry.get("coordinates") or []:
        for ring in poly:
            for pt in ring:
                xs += pt[0]
                ys += pt[1]
                n += 1
    return (xs / n, ys / n) if n else None


def match_htc_union(features: list[dict], roster: list[dict]) -> int:
    """Attach the nearest union shop to each building, in place.

    Matched on position because the roster carries no street address — all 377
    records have an empty address field — and because coordinates beat name
    matching anyway: "The Players" and "Row NYC" would never join by string.
    """
    if not roster:
        return 0

    cents = []
    for f in features:
        c = _centroid(f.get("geometry"))
        if c:
            cents.append((c, f["properties"], f["geometry"]))

    hits = 0
    for shop in roster:
        sx, sy = shop["longitude"], shop["latitude"]
        best, best_d, best_geom = None, float("inf"), None
        for (cx, cy), props, geom in cents:
            d = math.hypot((cx - sx) * _M_PER_DEG_LON, (cy - sy) * _M_PER_DEG_LAT)
            if d < best_d:
                best_d, best, best_geom = d, props, geom
        if best is None or best_d > HTC_MATCH_METRES:
            continue
        # One building can host several shops (a hotel and its restaurant).
        # Keep the closest, but let a converted type win over a plain Hotel,
        # since that is the fact that changes how the building reads.
        prior = best.get("htc_union_distance_m")
        prior_converted = best.get("htc_shop_type") in HTC_CONVERTED_SHOP_TYPES
        now_converted = shop["shop_type"] in HTC_CONVERTED_SHOP_TYPES
        if prior is not None and not (now_converted and not prior_converted):
            if prior <= best_d:
                continue
        if prior is None:
            hits += 1
        best["htc_union"] = True
        best["htc_union_name"] = shop["name"]
        best["htc_shop_type"] = shop["shop_type"]
        best["htc_union_status"] = shop["status"]
        best["htc_union_distance_m"] = round(best_d)
        best["htc_converted_use"] = now_converted
        # Nearest-within-60m is not the same as right. The roster's own
        # coordinates are approximate and Midtown blocks are dense, so the
        # nearest building is often the one next door: the Marriott at the
        # Brooklyn Bridge landed on 350 Jay Street, which is the municipal
        # building, and 540 Park Avenue took The Links over Loews Regency.
        # Whether the point falls inside the footprint is what separates the
        # two, and it is the gate on anything downstream that acts on a match.
        best["htc_match_basis"] = (
            "footprint" if point_in_footprint(best_geom, sx, sy) else "proximity"
        )
    return hits


def apply_roster_current_use(features: list[dict]) -> int:
    """Let a trustworthy roster match answer "what is this building now".

    The current-use check asks Google, which has the coverage — 2,048 buildings
    against the roster's 235 — but misses a building whose name gives nothing
    away. The Brook is a private members' club at 111 East 54th Street with ten
    Class B rooms; Google types it association_or_organization, the club rule
    matches on names containing "club", and the building scores 91.

    The union knows, because it staffs it. Where the roster calls a building a
    Residence, Club or Shelter and its point falls inside that building, that
    is a better answer than no answer.

    Gated on containment, never proximity. Of the 38 proximity-only matches,
    enough sit on the wrong building that acting on them would invent
    contradictions — which is the failure this function exists to avoid, not
    to commit in the other direction.
    """
    applied = 0
    for f in features:
        p = f["properties"]
        if p.get("current_use_conflict"):
            continue
        if p.get("htc_shop_type") not in HTC_BUILDING_SHOP_TYPES:
            continue
        # A reversion record beats the roster. It is dated, building-specific
        # research saying the non-hotel use ended — Row NYC was the last
        # migrant hotel and closed in Aug 2025 — where the roster is a standing
        # list with no end dates in it. Saying those 1,332 rooms are "already
        # occupied" would contradict the panel's own reversion box.
        if p.get("has_reversion"):
            continue
        if p.get("htc_match_basis") != "footprint":
            continue
        if p.get("tier") not in HTC_TRANSIENT_TIERS:
            continue
        shop_type = p.get("htc_shop_type", "")
        p["current_use_conflict"] = True
        p["current_use_source"] = "htc_roster"
        p["current_use_label"] = HTC_SHOP_TYPE_LABELS.get(shop_type, shop_type)
        p["current_use_name"] = p.get("htc_union_name", "")
        p["current_use_confidence"] = "high"
        p["current_use_basis"] = "Hotel Trades Council roster, matched inside the building footprint"
        applied += 1
    return applied



# Evidence a building carries, as opposed to the evidence that decided its tier.
#
# reason_codes were appended inside the if/elif chain that picks a tier, and in
# enrich.py only where a signal changed the verdict. That makes them a decision
# log, which is coherent -- and is not what the panel presents. It shows them
# under "Reason Codes" beside Legal Feasibility, where a reader takes them for
# the evidence on the building.
#
# They were not. dob_transient_occupancy appeared on 66 buildings and was true
# of 444, because a mixed-use class is tested before DOB occupancy and claims
# the building first. dcwp_hotel_license appeared on 11 and was true of 492,
# because it is appended only when a licence upgrades the tier -- so a building
# already legal on Class B rooms held an active hotel licence and said nothing
# about it. Click the pin, see why it qualifies, and the strongest thing on the
# record is missing.
#
# This runs last, over final properties, and adds any code whose field is true.
# Nothing is removed: a code the chain set is still there, and order still puts
# the deciding signal first.
# Uses the team asked to keep on the list, which are also uses a business can
# be trading in. A named occupant carrying one of these means the building is
# in possession — not that the use disqualifies it.
OCCUPIED_PRODUCT_USES = frozenset({
    "student_housing", "hostel", "sro", "private_club",
})


EVIDENCE = [
    ("hpd_class_b",             lambda p: (p.get("hpd_class_b") or 0) > 0),
    ("bldg_class_hotel",        lambda p: is_hotel_class(p.get("bldgclass"))),
    # R-1 or J-1: the panel and the methodology bucket both read "R-1/J-1", and
    # 64 buildings carry the J-1 classification and no R-1.
    ("dob_transient_occupancy", lambda p: bool(p.get("dob_has_r1") or p.get("dob_has_j1"))),
    ("current_use_conflict",    lambda p: bool(p.get("current_use_conflict"))),
    # The city printed a shelter notice for this address. It is evidence to
    # read, not a verdict — the notice carries a publication date and no term,
    # so it says a shelter was contracted here, never that one is here now.
    ("city_shelter_notice",     lambda p: bool(p.get("shelter_notice"))),
    ("illegal_transient_violation", lambda p: (p.get("ecb_illegal_transient") or 0) > 0),
    # Split the way DCWP splits it: a lapsed licence is evidence of a different
    # thing from a live one, and collapsing them would read as still licensed.
    ("dcwp_hotel_license",      lambda p: p.get("has_hotel_license")
        and p.get("hotel_license_status") in ("Active", "Ready for Renewal")),
    ("dcwp_license_lapsed",     lambda p: p.get("has_hotel_license")
        and p.get("hotel_license_status") in ("Surrendered", "Failed to Renew")),
]


# A code whose field is its whole definition, so the field is allowed to take it
# back. current_use_conflict is appended when a conflict is detected and the
# field is cleared again further down enrich.py; 16 buildings ended up wearing
# the chip with the field reading False, which is the same contradiction this
# stage exists to remove, pointing the other way.
#
# dob_transient_occupancy is deliberately not in here. It is legitimately set
# from DOB filings the dob_has_r1 flag does not cover.
FIELD_OWNED = {
    "current_use_conflict": lambda p: bool(p.get("current_use_conflict")),
    "city_shelter_notice": lambda p: bool(p.get("shelter_notice")),
}


def complete_reason_codes(features):
    added = {}
    dropped = {}
    for f in features:
        p = f["properties"]
        codes = [c for c in (p.get("reason_codes") or [])
                 if c not in FIELD_OWNED or FIELD_OWNED[c](p)]
        for c in set(p.get("reason_codes") or []) - set(codes):
            dropped[c] = dropped.get(c, 0) + 1
        seen = set(codes)
        for code, test in EVIDENCE:
            if code in seen:
                continue
            try:
                hit = bool(test(p))
            except (TypeError, ValueError):
                hit = False
            if hit:
                codes.append(code)
                seen.add(code)
                added[code] = added.get(code, 0) + 1
        p["reason_codes"] = codes
    if added:
        print("Reason codes completed from the fields they describe:")
        for code, n in sorted(added.items(), key=lambda kv: -kv[1]):
            print(f"  +{n:5} {code}")
    if dropped:
        print("Reason codes dropped where the field they name is false:")
        for code, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"  -{n:5} {code}")

    # Check the work, here rather than in a test. The panel presents these as
    # the evidence on a building, and a code that stops matching its field is
    # invisible -- it reads as a building that simply has less going on. The
    # build is the only place that sees every row, so it is the place to fail.
    mismatches = []
    for code, test in EVIDENCE:
        missing = sum(1 for f in features
                      if test(f["properties"]) and code not in f["properties"]["reason_codes"])
        extra = sum(1 for f in features
                    if code in f["properties"]["reason_codes"] and not test(f["properties"]))
        if missing or extra:
            mismatches.append(f"  {code}: {missing} with the field and no code, "
                              f"{extra} with the code and no field")
    if mismatches:
        print("::error title=Reason codes do not match their fields::")
        print("\n".join(mismatches))
        raise SystemExit(1)
    print(f"Reason codes agree with their fields on all {len(features)} buildings.")
    return added


def place_without_footprints(dropped, best_by_bbl):
    """Give a point to buildings DOB has no footprint for, so they exist at all.

    A record with no footprint was dropped outright, because there was no shape
    to draw. That silently removed 35 buildings from the map and the table
    alike, 11 of them legal_transient, including Hotel Beacon and its 320
    registered Class B rooms -- a building ground_truth.csv names as a
    canonical positive.

    They are injected here with a synthetic footprint rather than built
    separately, so they pass through the identical property pipeline below and
    differ only in geometry.

    GeoSearch is asked for the address, and its answer is only trusted when the
    BBL it returns matches the BBL we asked about. A geocoder that resolves
    "2126 Broadway" to a neighbouring lot would otherwise put a pin on the
    wrong building, and a confident pin in the wrong place is worse than no pin
    -- those keep a null geometry, which is valid GeoJSON, invisible to the map
    and still present for the table.
    """
    if not dropped:
        return
    from src import geocoder

    placed = unplaceable = 0
    for record in dropped:
        record["_no_footprint"] = True
        address = (record.get("address") or "").strip()
        result = None
        if address:
            try:
                result = geocoder.geocode(f"{address}, New York, NY")
            except Exception as e:                       # noqa: BLE001
                print(f"  geocode failed for {address}: {e}")
        trustworthy = result and str(result.bbl or "") == str(record["bbl"])
        if trustworthy:
            record["_location_precision"] = "approximate"
            geom = {"type": "Point", "coordinates": [result.lon, result.lat]}
            placed += 1
        else:
            # Kept, not dropped. The table is where these still have to appear.
            record["_location_precision"] = "unplaceable"
            geom = None
            unplaceable += 1
        best_by_bbl[record["bbl"]] = (
            record,
            {"the_geom": geom, "bin": "", "height_roof": None, "construction_year": None},
        )
    print(f"No DOB footprint: {len(dropped)} buildings kept anyway "
          f"({placed} placed by geocode, {unplaceable} with no trustworthy location)")


def report_contact_coverage(features):
    """How much of the contact problem the free sources actually solve.

    Printed because the number decides whether anyone should be paying Reonomy
    or LightBox for the rest, and it is worth seeing move week to week rather
    than being measured once in a spreadsheet.

    A contact counts only when it has an address. HPD names a head officer for
    almost every registered building, and a name with nowhere to send anything
    is not a contact — counting those would flatter this considerably.
    """
    both = owner_only = agent_only = neither = 0
    by_source = {}
    for f in features:
        cs = [c for c in (f["properties"].get("owner_contacts") or []) if c.get("address")]
        for c in cs:
            by_source[c["source"]] = by_source.get(c["source"], 0) + 1
        kinds = {c["kind"] for c in cs}
        if {"owner", "managing_agent"} <= kinds:
            both += 1
        elif "owner" in kinds:
            owner_only += 1
        elif "managing_agent" in kinds:
            agent_only += 1
        else:
            neither += 1
    n = len(features)
    covered = both + owner_only + agent_only
    print("Free owner contacts (HPD registration + ACRIS):")
    print(f"  owner and managing agent : {both:5}  ({100 * both // n}%)")
    print(f"  owner only               : {owner_only:5}  ({100 * owner_only // n}%)")
    print(f"  managing agent only      : {agent_only:5}  ({100 * agent_only // n}%)")
    print(f"  neither — needs a paid lookup : {neither:5}  ({100 * neither // n}%)")
    print(f"  any free contact         : {covered:5}  ({100 * covered // n}% of {n})")
    for src, c in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    via {src}: {c}")


def check_no_laundered_operators(features):
    """Refuse to publish a departure dressed up as a current operator.

    operator_name falls back to the prior-operator ground truth, which records
    that a flex company has LEFT. Reading it back as current is what had Sonder
    running 20 Broad Street months after they had gone -- and the file said
    "ground_truth" about it, which is the most convincing a wrong record gets.
    """
    bad = [f["properties"] for f in features
           if f["properties"].get("flex_op_status") == "current"
           and f["properties"].get("operator_source") == "ground_truth"]
    if bad:
        print("::error title=Prior operator reported as current::")
        for p in bad:
            print(f"  {p.get('address')} — {p.get('flex_op_name')}")
        raise SystemExit(1)
    current = sum(1 for f in features if f["properties"].get("flex_op_status") == "current")
    print(f"Flex operators: {current} current, each with present-tense evidence.")


def build_geojson(
    pipeline_path: Path = None,
    footprints_path: Path = None,
) -> Path:
    if pipeline_path is None:
        pipeline_path = DATA_PROCESSED / f"pipeline_{TODAY}.json"
        if not pipeline_path.exists():
            files = sorted(DATA_PROCESSED.glob("pipeline_*.json"), reverse=True)
            if files:
                pipeline_path = files[0]
    if footprints_path is None:
        footprints_path = DATA_RAW / f"footprints_{TODAY}.json"
        if not footprints_path.exists():
            files = sorted(DATA_RAW.glob("footprints_*.json"), reverse=True)
            if files:
                footprints_path = files[0]

    pipeline = json.loads(pipeline_path.read_text())
    footprints = json.loads(footprints_path.read_text())

    alt_addr_path = DATA_PROCESSED / f"alt_addresses_{TODAY}.json"
    if not alt_addr_path.exists():
        files = sorted(DATA_PROCESSED.glob("alt_addresses_*.json"), reverse=True)
        if files:
            alt_addr_path = files[0]
    alt_addresses = json.loads(alt_addr_path.read_text()) if alt_addr_path.exists() else {}

    # Drop unknown-tier buildings — no transient signal, just noise
    # But keep any building with a prior_operator tag
    # A hand-curated reversion survives this the same way it survives the
    # zoning filter below. 234 East 46th Street is AKA United Nations, now the
    # Perrie condominiums — 95 units, which is the figure the curated note
    # records — and it arrives with tier "unknown" because HPD has nothing on
    # a condominium billing lot. It was dropped here, two filters before the
    # exemption that was meant to keep it.
    pipeline = [r for r in pipeline
                if r["tier"] != "unknown"
                or r.get("prior_operator")
                or r["bbl"] in POST_2021_REVERSIONS]

    # A building is "actively operating" if it has evidence of current hotel use.
    # Buildings without this evidence would need a CPC special permit (2021 text
    # amendment) — filter those out entirely.
    def _is_actively_operating(r):
        if r.get("hpd_class_b", 0) > 0:
            return True
        if r.get("has_hotel_license"):
            return True
        if r.get("prior_operator"):
            return True
        if r["bbl"] in POST_2021_REVERSIONS:
            return True
        if (r.get("reversion_window") or {}).get("pre_2021_use"):
            return True
        return False

    # Drop buildings in incompatible zoning unless they have HPD Class B rooms
    # (confirmed current transient operation = grandfathered nonconforming use).
    # License/reversion/prior-op alone isn't enough — without Class B, there's
    # no active transient use to grandfather.
    #
    # The reversion exemption reads the hand-curated list and now also an
    # evidenced derived window. A reversion candidate has no Class B rooms by
    # definition — that is the "residential now" half of the test — so every
    # clause above rejected it, and 37 of 42 candidates were dropped here
    # having been found two stages earlier. Only evidenced ones are exempt:
    # without evidence the use reached the 2021 cutoff there is nothing to
    # grandfather, which is what this filter is for.
    def _evidenced_reversion(r):
        return bool((r.get("reversion_window") or {}).get("pre_2021_use"))

    pre_zoning = len(pipeline)
    # A building already operating as a hotel cannot be disqualified by a
    # zoning district that would not permit a new one — that is what a
    # pre-existing nonconforming use is. The rule let HPD Class B stand for
    # "already transient", which misses every hotel that does not register
    # with HPD: 449 West 36th (Casamia 36), 319 West 38th (Hotel 38, Tapestry
    # Collection by Hilton), 24 East 39th (The William) and 56 Irving Place
    # (The Inn at Irving Place) all hold live DCWP hotel licences and were
    # being dropped off the map before anyone could see them.
    def _already_transient(r):
        return (r.get("hpd_class_b", 0) > 0
                or r.get("has_hotel_license")
                or str(r.get("bldgclass") or "").upper().startswith("H")
                or r.get("dob_has_r1"))

    n = _set_out_of_scope(
        pipeline,
        lambda r: not (r.get("zoning_hotel_permitted") == "permitted"
                       or _already_transient(r)
                       or r["bbl"] in POST_2021_REVERSIONS
                       or _evidenced_reversion(r)),
        "zoning",
        "Zoning does not permit hotel use here and the building carries no "
        "existing transient right to grandfather.")
    print(f"Zoning: marked {n} of {pre_zoning} out of scope (none dropped)")

    # A hand-curated BBL that matches nothing does nothing, and says so to
    # nobody. Two of the six — the W New York / St Giles and AKA United
    # Nations — name lots PLUTO no longer carries, almost certainly merged or
    # renumbered on conversion, and had been inert for as long as anyone had
    # been reading the count as six.
    present = {r["bbl"] for r in pipeline}
    orphans = [b for b in POST_2021_REVERSIONS if b not in present]
    if orphans:
        print(f"Curated reversions matching no building: {len(orphans)} of {len(POST_2021_REVERSIONS)}")
        for b in orphans:
            print(f"  {b} — {POST_2021_REVERSIONS[b].get('former_hotel', '?')}")

    # Drop hotel-class buildings that aren't actively operating — they'd need
    # a CPC special permit to start new hotel use
    pre_permit = len(pipeline)
    n = _set_out_of_scope(
        pipeline,
        lambda r: not (_is_actively_operating(r) or r.get("tier") != "legal_transient"),
        "special_permit",
        "Hotel-class but not operating, so restarting transient use would need "
        "a CPC special permit under the December 2021 rule.")
    print(f"Special permit: marked {n} of {pre_permit} out of scope (none dropped)")

    # Drop non-target buildings (dorms, shelters, HDFCs, garages, vacant land, etc.)
    pre_inst = len(pipeline)
    # A reversion candidate is exempt here as it is from the zoning and special
    # permit filters. The Stewart Hotel closed in 2022 and its owner is now
    # BG Stewart Housing Development Fund Corporation, so the HDFC test fired
    # and removed 620 Class B rooms from the map — but that ownership is the
    # consequence of the reversion, not a reason to hide it. The legend said
    # six tracked and the map carried five.
    n = _set_out_of_scope(
        pipeline,
        lambda r: r["bbl"] not in POST_2021_REVERSIONS and _is_non_target(r),
        "non_target",
        "Institutional or non-residential use — school, shelter, garage, "
        "warehouse or similar.")
    print(f"Non-target: marked {n} of {pre_inst} out of scope (none dropped)")

    # Drop non-residential buildings with no units and no hotel signals
    MIXED_RES_CLASSES = {"RC", "RD", "RM", "RH", "RK", "RI", "RR", "RX", "RW", "RB", "RZ", "R1", "R4"}
    def _is_empty_non_hotel(r):
        cls = r.get("bldgclass", "")
        if cls.startswith("H") or cls in MIXED_RES_CLASSES:
            return False
        if r.get("unitsres", 0) > 0 or r.get("hpd_class_b", 0) > 0:
            return False
        if r.get("has_hotel_license") or r.get("hotel_name"):
            return False
        return True
    pre_empty = len(pipeline)
    n = _set_out_of_scope(
        pipeline, _is_empty_non_hotel, "no_units",
        "No residential units, no Class B rooms and no hotel signal of any kind.")
    print(f"Empty non-hotel: marked {n} of {pre_empty} out of scope (none dropped)")

    # Drop non-H buildings where Class B is negligible relative to Class A
    def _negligible_class_b(r):
        if r.get("bldgclass", "").startswith("H"):
            return False
        if r.get("has_hotel_license") or r.get("hotel_name"):
            return False
        class_b = r.get("hpd_class_b", 0) or 0
        class_a = r.get("hpd_class_a", 0) or 0
        if class_b == 0 or class_a < 20:
            return False
        return class_b <= 3 and class_b / (class_a + class_b) < 0.05
    pre_neg = len(pipeline)
    n = _set_out_of_scope(
        pipeline, _negligible_class_b, "negligible_class_b",
        "Three or fewer Class B rooms in a residential building of twenty or "
        "more units — too few to be the point of a deal.")
    print(f"Negligible Class B: marked {n} of {pre_neg} out of scope (none dropped)")

    # Index pipeline by BBL
    pipe_by_bbl = {r["bbl"]: r for r in pipeline}

    # Index footprints by both base_bbl and mappluto_bbl
    fp_by_bbl: dict[str, list[dict]] = {}
    for fp in footprints:
        for key in ("base_bbl", "mappluto_bbl"):
            bbl = str(fp.get(key, "")).strip()
            if bbl:
                fp_by_bbl.setdefault(bbl, []).append(fp)

    TIER_PRIORITY = {
        "legal_transient": 0, "partial": 1, "unknown": 2, "excluded": 3,
    }

    # Deduplicate: one feature per footprint (doitt_id), highest-tier record wins
    # Multiple condo lots can map to the same footprint — pick the best signal
    best_by_doitt: dict[str, tuple[dict, dict]] = {}  # doitt_id -> (record, fp)
    matched = 0
    unmatched_pipeline = 0

    dropped_no_geometry = []
    for bbl, record in pipe_by_bbl.items():
        fps = fp_by_bbl.get(bbl, [])
        if not fps:
            # Dropped for want of a shape to draw. Recorded rather than only
            # counted: 27 of these are legal_transient, and one is Hotel Beacon
            # with 320 Class B rooms -- a building ground_truth.csv names as a
            # canonical positive. A number on its own never made that visible.
            unmatched_pipeline += 1
            dropped_no_geometry.append(record)
            continue

        matched += 1
        for fp in fps:
            geom = fp.get("the_geom")
            if not geom:
                continue

            doitt_id = str(fp.get("doitt_id", ""))
            existing = best_by_doitt.get(doitt_id)
            rec_priority = TIER_PRIORITY.get(record["tier"], 99)

            if existing is None:
                best_by_doitt[doitt_id] = (record, fp)
            else:
                existing_priority = TIER_PRIORITY.get(existing[0]["tier"], 99)
                if rec_priority < existing_priority:
                    best_by_doitt[doitt_id] = (record, fp)
                elif rec_priority == existing_priority and record.get("prior_operator"):
                    best_by_doitt[doitt_id] = (record, fp)

    # Collapse to one feature per PROPERTY, not per structure. A single tax lot
    # can carry several building footprints — 17 Battery Place has two BINs, and
    # one lot has fourteen. Emitting a feature each produced 302 duplicate
    # features over 205 BBLs, every copy identical but for bin/height_roof/
    # construction_year. That inflated counts and double-counted exports.
    # Merge the footprints into one MultiPolygon; the tallest structure supplies
    # the representative bin, height and year.
    def _height(fp_):
        try:
            return float(fp_.get("height_roof") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _polygons(geom):
        if not geom:
            return []
        if geom.get("type") == "Polygon":
            return [geom["coordinates"]]
        if geom.get("type") == "MultiPolygon":
            return list(geom["coordinates"])
        return []

    best_by_bbl: dict[str, tuple[dict, dict]] = {}
    for record, fp in best_by_doitt.values():
        bbl = record["bbl"]
        existing = best_by_bbl.get(bbl)
        if existing is None:
            merged = dict(fp)
            merged["the_geom"] = {
                "type": "MultiPolygon",
                "coordinates": _polygons(fp.get("the_geom")),
            }
            best_by_bbl[bbl] = (record, merged)
            continue
        merged = existing[1]
        merged["the_geom"]["coordinates"].extend(_polygons(fp.get("the_geom")))
        if _height(fp) > _height(merged):
            for k in ("bin", "height_roof", "construction_year"):
                merged[k] = fp.get(k)

    place_without_footprints(dropped_no_geometry, best_by_bbl)

    features = []
    for record, fp in best_by_bbl.values():
        properties = {
            "bbl": record["bbl"],
            # PLUTO carries no address for some condominium billing lots —
            # 1008397501 is the 420 Fifth Avenue condominium and comes through
            # with an empty string. It was unsearchable, unexportable and
            # unvisitable, and the alt-address list had six spellings of it
            # sitting right there.
            "address": record["address"] or _fallback_address(record, alt_addresses),
            # True where DOB has no footprint for this lot, so the map draws a
            # point at a geocoded address instead of the building's outline.
            "no_footprint": bool(record.get("_no_footprint")),
            # Why a building that the old filters would have deleted is still
            # here. Empty on everything in scope.
            "out_of_scope": record.get("out_of_scope") or "",
            "out_of_scope_reason": record.get("out_of_scope_reason") or "",
            "location_precision": record.get("_location_precision", "footprint"),
            # Hotel Trades Council roster; filled in by match_htc_union below.
            "htc_union": False,
            "htc_union_name": "",
            "htc_shop_type": "",
            "htc_union_status": "",
            "htc_converted_use": False,
            "htc_match_basis": "",
            "alt_addresses": alt_addresses.get(record["bbl"], []),
            "bldgclass": record["bldgclass"],
            "unitsres": record["unitsres"],
            "unitstotal": record["unitstotal"],
            "numfloors": record["numfloors"],
            "tier": record["tier"],
            "confidence": record["confidence"],
            "reason_codes": record["reason_codes"],
            "blockers": record["blockers"],
            "hpd_class_a": record["hpd_class_a"],
            "hpd_class_b": record["hpd_class_b"],
            "restricted_class": record.get("restricted_class", False),
            "restricted_class_reason": record.get("restricted_class_reason", ""),
            "rent_stabilized_units": record.get("rent_stabilized_units", 0),
            "rent_stab_class_b_exposure": record.get("rent_stab_class_b_exposure", 0),
            "hpd_dob_class": record["hpd_dob_class"],
            "ownername": record["ownername"],
            "cd": record.get("cd", ""),
            "neighborhood": CD_TO_NEIGHBORHOOD.get(record.get("cd", ""), ""),
            "zonedist1": record["zonedist1"],
            "height_roof": fp.get("height_roof"),
            "construction_year": fp.get("construction_year"),
            "bin": fp.get("bin", ""),
            "source_pulled_on": record["source_pulled_on"],
            "last_sale_date": record.get("last_sale_date"),
            "last_sale_price": record.get("last_sale_price"),
            "sale_count": record.get("sale_count", 0),
            "permit_count": record.get("permit_count", 0),
            "owner_canonical": record.get("owner_canonical", record.get("ownername", "")),
            "owner_portfolio_size": record.get("owner_portfolio_size", 0),
            "coo_count": record.get("coo_count", 0),
            "coo_latest_date": record.get("coo_latest_date"),
            "reversion_unverified": record.get("reversion_unverified", False),
            # Why, in the detector's own words. See enrich.py.
            "reversion_unverified_reason": record.get("reversion_unverified_reason", ""),
            "ecb_illegal_transient": record.get("ecb_illegal_transient", 0),
            "fisp_applicable": record.get("fisp_applicable", False),
            # Safe Hotels Act thresholds
            "safe_hotels_guest_rooms": record.get("safe_hotels_guest_rooms", 0),
            "safe_hotels_room_basis": record.get("safe_hotels_room_basis", ""),
            "safe_hotels_direct_employment": record.get("safe_hotels_direct_employment", False),
            "safe_hotels_large_hotel": record.get("safe_hotels_large_hotel", False),
            "coo_latest_type": record.get("coo_latest_type"),
            "coo_has_temporary": record.get("coo_has_temporary", False),
            "coo_dwelling_units": record.get("coo_dwelling_units"),
            # Distress signals
            "hpd_open_violations": record.get("hpd_open_violations", 0),
            "hpd_class_c_violations": record.get("hpd_class_c_violations", 0),
            "ecb_open_violations": record.get("ecb_open_violations", 0),
            "ecb_total_balance": record.get("ecb_total_balance", 0),
            "has_tax_lien": record.get("has_tax_lien", False),
            "has_lis_pendens": record.get("has_lis_pendens", False),
            "lis_pendens_count": record.get("lis_pendens_count", 0),
            # ACRIS owner identification
            "acris_deed_owner": record.get("acris_deed_owner", ""),
            "acris_deed_date": record.get("acris_deed_date", ""),
            "acris_deed_address": record.get("acris_deed_address", ""),
            "acris_borrower": record.get("acris_borrower", ""),
            "acris_lender": record.get("acris_lender", ""),
            # Hotel info
            "hotel_name": record.get("hotel_name", ""),
            "hotel_phone": record.get("hotel_phone", ""),
            "hotel_website": record.get("hotel_website", ""),
            "is_branded": _is_branded(record.get("hotel_name", ""), record.get("operator_name", ""), bbl),
            # DOB occupancy classification
            "dob_has_r1": record.get("dob_has_r1", False),
            "dob_has_j1": record.get("dob_has_j1", False),
            "dob_r1_filing_count": record.get("dob_r1_filing_count", 0),
            "dob_transient_units": record.get("dob_transient_units", 0),
            # Permit description transient signals
            "permit_transient_keywords": record.get("permit_transient_keywords", []),
            "permit_transient_strong": record.get("permit_transient_strong", 0),
            # DCWP hotel license — licensure, kept distinct from operation
            "has_hotel_license": record.get("has_hotel_license", False),
            "hotel_license_name": record.get("hotel_license_name", ""),
            "hotel_license_status": record.get("hotel_license_status", ""),
            "safe_hotels_licensed": record.get("safe_hotels_licensed", False),
            "hotel_license_created": record.get("hotel_license_created", ""),
            "hotel_license_term_years": record.get("hotel_license_term_years"),
            "coo_temp_only": record.get("coo_temp_only", False),
            "special_permit_required": "special_permit_required" in record.get("reason_codes", []),
            # Current use on the ground (Google Places, address-verified)
            "current_use": record.get("current_use", ""),
            "current_use_label": record.get("current_use_label", ""),
            "current_use_name": record.get("current_use_name", ""),
            "current_use_confidence": record.get("current_use_confidence", ""),
            "current_use_basis": record.get("current_use_basis", ""),
            "current_use_place_id": record.get("current_use_place_id", ""),
            "current_use_conflict": record.get("current_use_conflict", False),
            "current_use_source": "google" if record.get("current_use") else "",
            "current_use_occupants": record.get("current_use_occupants", []),
            "current_use_needs_review": record.get("current_use_needs_review", False),
            "current_use_checked": record.get("current_use_checked", False),
            # Published by the city, carried whole so a reader can open it.
            "occupancy_state": _occupancy_state(record),
            # Only where the register is what decided it. On a building Places
            # named an occupant for, this read "the certificate of occupancy
            # carries a dwelling-unit count" beside a state that had nothing to
            # do with the C of O.
            "occupancy_basis": (_city_records_know_use(record)
                                if _occupancy_state(record) == "onrecord" else ""),
            "shelter_notice": record.get("shelter_notice", False),
            "shelter_status": record.get("shelter_status", ""),
            "shelter_status_basis": record.get("shelter_status_basis", ""),
            "shelter_notice_date": record.get("shelter_notice_date", ""),
            "shelter_notice_count": record.get("shelter_notice_count", 0),
            "shelter_notice_evidence": record.get("shelter_notice_evidence", []),
            # Operator identification
            "operator_name": record.get("operator_name", ""),
            "operator_source": record.get("operator_source", ""),
            "hpd_managing_agent": record.get("hpd_managing_agent", ""),
            "hpd_managing_agent_corp": record.get("hpd_managing_agent_corp", ""),
            # Who to write to, from HPD registration and ACRIS. Each entry
            # carries its own source and pull date; see enrich.py.
            "owner_contacts": record.get("owner_contacts", []),
            "has_free_contact": record.get("has_free_contact", False),
            "hpd_owner_corp": record.get("hpd_owner_corp", ""),
            "hpd_head_officer": record.get("hpd_head_officer", ""),
            # Mortgage maturity
            "mortgage_age_years": record.get("mortgage_age_years"),
            "mortgage_amount": record.get("mortgage_amount", ""),
            "mortgage_approaching_maturity": record.get("mortgage_approaching_maturity", False),
            "acris_mtge_date": record.get("acris_mtge_date", ""),
            # LPC landmarks / historic districts
            "is_landmark": record.get("is_landmark", False),
            "landmark_name": record.get("landmark_name", ""),
            "is_historic_district": record.get("is_historic_district", False),
            "historic_district": record.get("historic_district", ""),

            # Zoning compatibility
            "zoning_hotel_permitted": record.get("zoning_hotel_permitted", "unknown"),
            "zoning_hotel_detail": record.get("zoning_hotel_detail", ""),
            # Ownership structure
            "is_condo": _is_condo(record),
        }

        # Rooms being rebuilt right now. Emitted whatever the segment, so the
        # nine operating hotels doing upkeep carry it too — it is a fact about
        # the building either way.
        works = _guestroom_works(record)
        if works:
            properties["guestroom_works"] = works

        # Include top 3 permits (trimmed to save space)
        permits = record.get("permits", [])
        if permits:
            properties["permits"] = [
                {k: p[k] for k in ("job_type_label", "description", "action_date", "cost", "status") if p.get(k)}
                for p in permits[:3]
            ]

        # Include top 3 C of O records
        coos = record.get("coo_records", [])
        if coos:
            properties["coo_records"] = [
                {k: c[k] for k in ("issue_date", "job_type", "co_type", "dwelling_units") if c.get(k)}
                for c in coos[:3]
            ]

        if record.get("prior_operator"):
            properties["prior_operator"] = record["prior_operator"]
            properties["has_prior_op"] = True

        # Flex-stay operator, current as well as former. has_prior_op only ever
        # meant "a flex company has left this building"; one running it right
        # now matters just as much for sourcing, and the overlay missed those.
        # has_prior_op is left untouched so the detail panel and export category
        # keep meaning "former".
        current_flex = _current_flex_operator(record)
        if current_flex:
            properties["flex_op_name"] = current_flex
            properties["flex_op_status"] = "current"
        # `.get(key, {})` hands back None when the key is present holding
        # None, which most records here do.
        elif _is_flex_name((record.get("prior_operator") or {}).get("name", "")):
            properties["flex_op_name"] = record["prior_operator"]["name"]
            properties["flex_op_status"] = "former"
        if properties.get("flex_op_name"):
            properties["has_flex_op"] = True
            properties["flex_op_is_kasa"] = bool(
                re.search(r"\bkasa\b", properties["flex_op_name"], re.I)
            )

        # What kind of reversion this is, for everyone, derived from the data.
        #
        # Two mechanisms had grown up beside each other and they never met:
        # has_reversion from the hand-curated list, and the reversion_window
        # reason code from the rule. Zero overlap, and the line between them
        # was hpd_class_b all along.
        #
        # A closed hotel still registering Class B rooms has not converted to
        # anything — the rooms are legally transient and there is nothing to
        # undo. Row NYC is 1,332 of them, the Hudson 959, the Stewart 620.
        # A building with none has gone residential and needs the window to
        # come back. Both are worth sourcing and they are not the same job,
        # so they no longer share a word.
        if record.get("reversion_window") or POST_2021_REVERSIONS.get(record["bbl"]):
            properties["reversion_kind"] = (
                "closed" if (record.get("hpd_class_b") or 0) > 0 else "converted"
            )

        reversion_info = POST_2021_REVERSIONS.get(record["bbl"])
        if reversion_info:
            # Checked against the record every run rather than asserted once.
            # The figure that was wrong stayed wrong for as long as nobody
            # re-read it.
            claimed = reversion_info.get("sale_price")
            actual = record.get("last_sale_price")
            verified = bool(claimed and actual and abs(int(actual) - claimed) <= 1000)
            properties["reversion"] = {
                **reversion_info,
                "verified": verified,
                "verified_against": (
                    f"DOF sale {record.get('last_sale_date')} of ${int(actual):,}"
                    if verified and actual else None
                ),
            }
            properties["has_reversion"] = True
            properties["reversion_unverified"] = not verified

        if record.get("class_b_split"):
            properties["class_b_split"] = record["class_b_split"]

        # Segment: subdivide legal_transient for prospecting
        # Reversion is an overlay, not a segment — building keeps its real segment
        seg_tier = record["tier"]
        # Licensure and operation are distinct facts and are now stored as
        # distinct fields — see the DCWP block in enrich.py. The segment below
        # deliberately still counts a licence as operator evidence, which is
        # the looser of the two readings, because dropping it was measured and
        # made the map worse: 34 buildings moved to "available capacity",
        # among them The London NYC, the Ritz-Carlton Central Park and Sonder
        # 1 Platt. Their Places lookup had never resolved a hotel name, so the
        # licence was the only thing standing between an operating hotel and a
        # sourcing target.
        #
        # The signal that should carry this is current_use, which asks who is
        # in the building rather than who holds paper on it. Once the Places
        # sweep has run, licensure can come out of this test.
        op_name = (record.get("operator_name") or "").lower()
        op_looks_like_hotel = any(
            w in op_name for w in ("hotel", "inn ", "suites", "hostel", "motel")
        )
        has_active_operator = bool(
            record.get("hotel_name")
            or record.get("has_hotel_license")
            or op_looks_like_hotel
            or record.get("current_use") == "hotel"
        )
        # The same question one step wider. has_active_operator only knows how
        # to see a hotel, so a building with a dormitory, hostel, SRO or club
        # trading in it read as having nobody in it at all: 99 Washington
        # Street is 492 Class B rooms running as FOUND Study Financial
        # District and sat in "no operator" scoring 85.
        #
        # The team asked to see these product types, which is about what the
        # building is. Whether someone is in possession is a different fact,
        # and the penalty that used to carry it was doing both jobs at once.
        # A dorm-classed building with Class B rooms and nobody trading in it
        # is still a target and stays one — 68 of them do.
        occupant_name = (record.get("current_use_name") or "").strip()
        has_other_operator = bool(
            occupant_name and record.get("current_use") in OCCUPIED_PRODUCT_USES
        )
        # Out of scope is its own segment, so the view can leave it off by
        # default without the building being absent from the file. It is
        # tested first: a warehouse that happens to hold a Class B room is
        # still a warehouse, and the reason it is out of scope is the more
        # useful thing to show.
        if record.get("out_of_scope"):
            properties["segment"] = "out_of_scope"
        elif seg_tier == "legal_transient" and has_active_operator:
            properties["segment"] = "active_hotel"
        elif seg_tier == "legal_transient" and has_other_operator:
            properties["segment"] = "active_other"
        elif seg_tier == "legal_transient":
            properties["segment"] = "transient"
        elif seg_tier == "partial":
            properties["segment"] = (
                "partial" if _has_transient_evidence(record) else "class_only")
        else:
            properties["segment"] = "unknown"

        # Deal sub-scores. Only availability is computed here.
        #
        # score_legal used to live here too, with a comment insisting it "must
        # stay identical to computeScore() in map/src/App.jsx". It could not.
        # The app lets the deal team retune every weight per user, so no number
        # baked in at build time can be the score anyone is looking at -- and
        # the two drifted twice, first to agreeing on 18 rows of 2,917, then to
        # disagreeing on 572 of 2,592 once penalties for current-use conflict
        # and union coverage were added here and never here.
        #
        # It is not recomputed, it is gone. Nothing read it: the app computes
        # its own, and the export to HubSpot was dropped once the two numbers
        # were found sitting on the same record. The score has one definition
        # now, SCORE_SIGNALS in the app, and that file is where it belongs
        # because that is where the weights are editable.
        #
        # score_quality went the same way: nothing read it, and it scored any
        # H-class as a hotel -- including the H8 dormitories and HR SROs this
        # pipeline excludes by name three hundred lines up.

        # Availability. Kept: the app has no counterpart to contradict, and
        # HubSpot exports it.
        #
        # The divisor was 45 and the five terms sum to 40, so 100 was
        # unreachable by construction — the best a building could do was 89,
        # and the highest in the set is 62. On a 0-100 scale displayed beside
        # a legal score that does reach 100, that reads as "barely available"
        # for a building carrying every distress signal we track. It is shown
        # on the Independent Hotels tab and exported to HubSpot, so the number
        # went out wrong in both.
        #
        # Derived from the weights rather than written down twice, so adding a
        # term cannot leave the divisor behind again.
        AVAIL_SIGNALS = (
            (15, bool(record.get("prior_operator"))),
            (8, bool(record.get("has_tax_lien"))),
            (8, bool(record.get("has_lis_pendens"))),
            (5, (record.get("last_sale_date") or "") >= f"{date.today().year - 2}-01-01"),
            (4, (record.get("ecb_total_balance") or 0) > 10000),
        )
        avail = sum(points for points, fired in AVAIL_SIGNALS if fired)
        avail_max = sum(points for points, _ in AVAIL_SIGNALS)
        properties["score_avail"] = round(avail / avail_max * 100)

        feature = {
            "type": "Feature",
            "geometry": fp["the_geom"],
            "properties": properties,
        }
        features.append(feature)

    roster = load_htc_union()
    union_hits = match_htc_union(features, roster)
    converted = sum(1 for f in features if f["properties"].get("htc_converted_use"))
    roster_conflicts = apply_roster_current_use(features)
    if roster:
        inside = sum(1 for f in features
                     if f["properties"].get("htc_match_basis") == "footprint")
        print(f"HTC union roster: {len(roster)} shops, {union_hits} matched to buildings")
        print(f"  matched inside the footprint: {inside}, on proximity alone: {union_hits - inside}")
        print(f"  covered but no longer a hotel (Residence/Club/Shelter/...): {converted}")
        print(f"  current-use conflicts the roster caught and Google did not: {roster_conflicts}")
    else:
        print("HTC union roster: not found — run src/pull_htc_union.py")

    complete_reason_codes(features)
    check_no_laundered_operators(features)
    report_contact_coverage(features)

    # Per collection, not per feature. Every building in a build reads the same
    # pull of the same source, so stamping 2,592 features with 22 dates each
    # would add megabytes to say one thing. source_pulled_on stays on the
    # feature because the app reads it; this is what it leaves out.
    sources = provenance.manifest()
    print("Sources this build read:")
    print(provenance.summarise(sources))
    stale = [k for k, v in sources.items() if not v["refreshed_by_ci"] and v["pulled_on"]]
    if stale:
        print(f"  note: {len(stale)} source(s) above are refreshed by hand, not by "
              f"refresh-data.yml. Their dates do not move when this runs.")
    missing = [k for k, v in sources.items() if not v["pulled_on"]]
    if missing:
        print(f"  warning: no file found for {', '.join(sorted(missing))}")

    geojson = {
        "type": "FeatureCollection",
        # What this build actually read, and which of it a weekly run keeps
        # current. Without it the file carries one date -- the newest PLUTO
        # pull -- over sources that were not pulled with PLUTO at all.
        "sources": sources,
        "features": features,
    }

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    outpath = DATA_PROCESSED / f"buildings_{TODAY}.geojson"
    outpath.write_text(json.dumps(geojson))
    if dropped_no_geometry:
        lost = [r for r in dropped_no_geometry if r.get("tier") == "legal_transient"]
        print(f"No DOB footprint: {len(dropped_no_geometry)} buildings, drawn as "
              f"points at a geocoded address ({len(lost)} of them legal_transient)")
        for r in sorted(lost, key=lambda r: -(r.get("hpd_class_b") or 0))[:5]:
            print(f"  {r['bbl']}  {r.get('address', '')[:34]:36} "
                  f"Class B {r.get('hpd_class_b') or 0}")
        if len(lost) > 5:
            print(f"  ...and {len(lost) - 5} more")

    print(f"Built GeoJSON: {len(features)} features ({matched} BBLs matched, {unmatched_pipeline} unmatched)")

    # Count what reached what, every build. Things fell through the cracks
    # because nothing was looking at the cracks: a source that returns nothing
    # and a source that was never asked look identical downstream, and the
    # sweep was silently skipping 173 buildings for a week before anyone
    # noticed. This will not tell you a source is wrong. It will tell you one
    # stopped arriving, which is the failure that actually keeps happening.
    from src.coverage import integrity, report

    report(features)
    breached = integrity(features)
    if breached:
        print(f"\n  {breached} integrity check(s) failed — see above.")

    return outpath


if __name__ == "__main__":
    build_geojson()
