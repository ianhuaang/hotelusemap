"""Join pipeline output to building footprints -> final GeoJSON for the map.

Pure and re-runnable. Reads from data/raw/ and data/processed/, writes to data/processed/.
"""

import json
import math
import re
from datetime import date
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED

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


POST_2021_REVERSIONS = {
    "1001060017": {
        "former_hotel": "Hampton Inn Manhattan-Seaport",
        "closure_year": 2023,
        "note": "Sold Dec 2023 to Slate Property Group for $24.1M. Hotel closed prior to sale.",
    },
    "1008940071": {
        "former_hotel": "W New York - The Court (St. Giles)",
        "closure_year": 2020,
        "note": "Closed during pandemic ~2020. Sold Jan 2023 for $50M. Currently migrant shelter.",
    },
    "1013190034": {
        "former_hotel": "AKA United Nations",
        "closure_year": 2024,
        "note": "Converted to The Perrie condominiums (~95 units). Post-2021 conversion.",
    },
    "1008060076": {
        "former_hotel": "Stewart Hotel",
        "closure_year": 2022,
        "note": "Closed 2022, used as migrant shelter. Acquired by Slate + Breaking Ground Dec 2025 for $255M. Converting to 579 affordable apartments.",
    },
    "1010167501": {
        "former_hotel": "Row NYC",
        "closure_year": 2025,
        "note": "Last NYC migrant hotel, closed Aug 2025. 1,332 rooms. Conversion status TBD — may reopen as hotel or convert to residential.",
    },
    "1010487502": {
        "former_hotel": "Hudson Hotel",
        "closure_year": 2020,
        "note": "Closed Nov 2020 during COVID. 959 rooms. Slated for conversion to 438 below-market apartments.",
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
    operator = (record.get("operator_name") or "").strip()
    # These two are usually the same string; only join when they differ.
    name = hotel if hotel.lower() == operator.lower() else " / ".join(x for x in (hotel, operator) if x)
    return name if any(op in name.lower() for op in FLEX_OPERATORS) else ""


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
    """Is this coordinate inside the building itself?"""
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


def _centroid(coordinates: list) -> tuple[float, float] | None:
    """Mean vertex of a MultiPolygon ring set."""
    xs, ys, n = 0.0, 0.0, 0
    for poly in coordinates:
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
        c = _centroid(f["geometry"]["coordinates"])
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
    pipeline = [r for r in pipeline if r["tier"] != "unknown" or r.get("prior_operator")]

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
        return False

    # Drop buildings in incompatible zoning unless they have HPD Class B rooms
    # (confirmed current transient operation = grandfathered nonconforming use).
    # License/reversion/prior-op alone isn't enough — without Class B, there's
    # no active transient use to grandfather.
    pre_zoning = len(pipeline)
    pipeline = [r for r in pipeline if r.get("zoning_hotel_permitted") == "permitted" or r.get("hpd_class_b", 0) > 0 or r["bbl"] in POST_2021_REVERSIONS]
    print(f"Zoning filter: {pre_zoning} -> {len(pipeline)} (removed {pre_zoning - len(pipeline)} not-permitted/unknown zoning)")

    # Drop hotel-class buildings that aren't actively operating — they'd need
    # a CPC special permit to start new hotel use
    pre_permit = len(pipeline)
    pipeline = [r for r in pipeline if _is_actively_operating(r) or r.get("tier") != "legal_transient"]
    print(f"Special permit filter: {pre_permit} -> {len(pipeline)} (removed {pre_permit - len(pipeline)} not actively operating)")

    # Drop non-target buildings (dorms, shelters, HDFCs, garages, vacant land, etc.)
    pre_inst = len(pipeline)
    # A reversion candidate is exempt here as it is from the zoning and special
    # permit filters. The Stewart Hotel closed in 2022 and its owner is now
    # BG Stewart Housing Development Fund Corporation, so the HDFC test fired
    # and removed 620 Class B rooms from the map — but that ownership is the
    # consequence of the reversion, not a reason to hide it. The legend said
    # six tracked and the map carried five.
    pipeline = [r for r in pipeline
                if r["bbl"] in POST_2021_REVERSIONS or not _is_non_target(r)]
    print(f"Non-target filter: {pre_inst} -> {len(pipeline)} (removed {pre_inst - len(pipeline)} non-target buildings)")

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
    pipeline = [r for r in pipeline if not _is_empty_non_hotel(r)]
    print(f"Empty non-hotel filter: {pre_empty} -> {len(pipeline)} (removed {pre_empty - len(pipeline)} buildings with no units/hotel signals)")

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
    pipeline = [r for r in pipeline if not _negligible_class_b(r)]
    print(f"Negligible Class B filter: {pre_neg} -> {len(pipeline)} (removed {pre_neg - len(pipeline)} residential buildings with <=3 Class B rooms)")

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

    for bbl, record in pipe_by_bbl.items():
        fps = fp_by_bbl.get(bbl, [])
        if not fps:
            unmatched_pipeline += 1
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

    features = []
    for record, fp in best_by_bbl.values():
        properties = {
            "bbl": record["bbl"],
            "address": record["address"],
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
            "current_use_conflict": record.get("current_use_conflict", False),
            "current_use_source": "google" if record.get("current_use") else "",
            "current_use_occupants": record.get("current_use_occupants", []),
            "current_use_needs_review": record.get("current_use_needs_review", False),
            "current_use_checked": record.get("current_use_checked", False),
            # Operator identification
            "operator_name": record.get("operator_name", ""),
            "operator_source": record.get("operator_source", ""),
            "hpd_managing_agent": record.get("hpd_managing_agent", ""),
            "hpd_managing_agent_corp": record.get("hpd_managing_agent_corp", ""),
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
        elif record.get("prior_operator"):
            properties["flex_op_name"] = record["prior_operator"]["name"]
            properties["flex_op_status"] = "former"
        if properties.get("flex_op_name"):
            properties["has_flex_op"] = True
            properties["flex_op_is_kasa"] = bool(
                re.search(r"\bkasa\b", properties["flex_op_name"], re.I)
            )

        reversion_info = POST_2021_REVERSIONS.get(record["bbl"])
        if reversion_info:
            properties["reversion"] = reversion_info
            properties["has_reversion"] = True

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
        if seg_tier == "legal_transient" and has_active_operator:
            properties["segment"] = "active_hotel"
        elif seg_tier == "legal_transient":
            properties["segment"] = "transient"
        elif seg_tier == "partial":
            properties["segment"] = "partial"
        else:
            properties["segment"] = "unknown"

        # Deal sub-scores (each 0-100, combined on frontend with adjustable weights)
        # Legal certainty (max 50 raw pts -> normalized to 0-100)
        legal = 0
        # Must stay identical to computeScore() in map/src/App.jsx, which is what
        # the UI actually displays, sorts and exports. These two had drifted apart
        # and agreed on only 18 of 2,917 rows.
        # Additive, max 93. dob_r1_filing_count is deliberately NOT scored: it
        # counts the same DOB filings that set dob_has_r1, so awarding points for
        # both double-counted one signal.
        bldgclass = record.get("bldgclass") or ""
        has_class_b = (record.get("hpd_class_b") or 0) > 0
        has_h_class = bldgclass.startswith("H")
        if has_class_b:
            legal += 40
        if has_h_class:
            legal += 25
        if record.get("dob_has_r1"):
            legal += 15
        # Mirror of computeScore(): a final C of O outranks a temporary one.
        coo_type = record.get("coo_latest_type") or ""
        if coo_type == "Final" or coo_type.startswith("Renewal"):
            legal += 12
        elif coo_type in ("Temporary", "Initial"):
            legal += 7
        if (record.get("permit_transient_strong") or 0) >= 1:
            legal += 8
        # Zoning penalty applies only where there are no grandfathered rights.
        if record.get("zoning_hotel_permitted") == "not_permitted" and not (has_class_b or has_h_class):
            legal = max(0, legal - 15)
        properties["score_legal"] = min(legal, 100)

        # Availability (max 45 raw pts -> normalized to 0-100)
        avail = 0
        if record.get("prior_operator"):
            avail += 15
        if record.get("has_tax_lien"):
            avail += 8
        if record.get("has_lis_pendens"):
            avail += 8
        sale_date = record.get("last_sale_date") or ""
        if sale_date >= f"{date.today().year - 2}-01-01":
            avail += 5
        if (record.get("ecb_total_balance") or 0) > 10000:
            avail += 4
        properties["score_avail"] = round(min(avail / 45, 1.0) * 100)

        # Building quality (max 15 raw pts -> normalized to 0-100)
        quality = 0
        bldgclass = record.get("bldgclass") or ""
        if not bldgclass.startswith("H"):
            quality += 7
        class_c = record.get("hpd_class_c_violations") or 0
        if class_c < 10:
            quality += 5
        elif class_c < 20:
            quality += 2
        if (record.get("owner_portfolio_size") or 0) > 1:
            quality += 3
        properties["score_quality"] = round(min(quality / 15, 1.0) * 100)

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

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    outpath = DATA_PROCESSED / f"buildings_{TODAY}.geojson"
    outpath.write_text(json.dumps(geojson))
    print(f"Built GeoJSON: {len(features)} features ({matched} BBLs matched, {unmatched_pipeline} unmatched)")
    return outpath


if __name__ == "__main__":
    build_geojson()
