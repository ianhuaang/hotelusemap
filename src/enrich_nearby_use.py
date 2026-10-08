#!/usr/bin/env python3
"""Ask Places what is standing on a building, when nothing else can say.

Method 2 in the deal-readiness escalation. Method 1 is the certificate, which
answers where one exists and is readable; this answers some of what is left.

Measured before it was wired. A 62-building probe over the no-certificate
residual on 2026-10-07 resolved 10 — a 16% rate — and the raw verdicts were
far worse than that before the guards below were applied. Those guards are not
defensive programming; each one is a specific wrong answer the probe returned.

  Text Search is not used at all. Asked for an address it returns the address:
  60 of 62 queries came back a `premise` or an untyped geocode sitting metres
  from the centroid, which is the geocoder answering rather than the business
  directory. One query in 62 carried a usable type. Nearby Search asks the
  question we actually have — what is standing at this point — and runs on its
  own quota metric.

  Generic `lodging` is rejected. 19 of 23 "hotels" the unguarded probe found
  were this type, and reading the names they are individual rental listings:
  "2BR/2BA w/ 4 Beds near Times Square", "South Chelsea NYC 30 Day Stays". One
  was Project Renewal men's shelter in East Williamsburg, which would have
  entered the data as an operating hotel. A listing for a flat is not a hotel
  and a shelter is the opposite of one.

  Professional tenants are rejected. 22 of 34 "residential/institutional"
  readings rested on a doctor, a clinic or a realtor. A physician's suite in
  the base of a 303-room Class B building says nothing about the 303 rooms,
  and reading it as the building is the error this whole dataset exists to
  avoid.

  30 metres, not 150. 14 of the probe's verdicts rested on a place beyond 30m,
  and the ones that could be checked were the neighbour: 37-35 21 Street
  answered "library" on the Queens Public Library 67m away, 14 Sutton Place
  South answered on a listing whose own name says Brooklyn, 54m off.

What it cannot do is say a building is empty. Silence here means Places has no
business registered on the footprint, which is the normal state of a block of
flats. Unresolved stays unresolved.

    GOOGLE_API_KEY=... python3 src/enrich_nearby_use.py
    ... --segment transient      only the no-operator segment (default)
    ... --all                    every building in the build
    ... --bbl 4004060040         one building, prints what it saw
    ... --limit 50
"""

from __future__ import annotations

import argparse
import json
import math
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

# The repo root goes on the path before anything is imported from it. The
# workflow runs this as `python src/enrich_nearby_use.py`, where `src` is not
# importable until it does -- fcb81e1 had the two src imports above this line,
# and the step failed on import under continue-on-error.
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW
from src.build_geojson import point_in_footprint
from src.enrich_current_use import load_alt_addresses

CTX = ssl.create_default_context(cafile=certifi.where())
API_KEY = os.environ.get("GOOGLE_API_KEY", "")
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELD_MASK = ("places.displayName,places.primaryType,places.types,"
              "places.location,places.businessStatus,places.formattedAddress")

OUTPUT_FILE = DATA_RAW / f"nearby_use_{date.today():%Y%m%d}.json"
CACHE_FILE = DATA_RAW / "nearby_use_cache.json"

# Metres from the footprint centroid. See the module docstring: at 150 the
# answer is as often the neighbour as the building.
MAX_DISTANCE_M = 30.0
SEARCH_RADIUS_M = 50.0

# An operator running rooms. `lodging` is deliberately absent — it is the type
# Places gives a single flat on a booking site, and it is also what it gave a
# men's shelter.
LODGING_TYPES = {
    "hotel", "motel", "hostel", "resort_hotel", "extended_stay_hotel",
    "bed_and_breakfast", "guest_house", "inn",
}

# Somebody lives here. These describe a building, not a unit inside one.
RESIDENTIAL_TYPES = {
    "apartment_building", "apartment_complex", "condominium_complex",
    "housing_complex", "housing_development", "dormitory", "student_housing",
}

# An institution occupying the building. Each of these names a premises; none
# of them is a practice or an agency that rents a floor.
INSTITUTIONAL_TYPES = {
    "church", "mosque", "synagogue", "hindu_temple", "place_of_worship",
    "school", "primary_school", "secondary_school", "university", "preschool",
    "hospital", "nursing_home", "assisted_living_facility",
    "psychiatric_hospital", "rehabilitation_center",
    "city_hall", "courthouse", "post_office", "fire_station", "police",
    "library", "embassy", "prison",
}

BUILDING_LEVEL = LODGING_TYPES | RESIDENTIAL_TYPES | INSTITUTIONAL_TYPES

# Guard 4. An institutional primaryType is right about the category and wrong
# about the entity more often than the other two are wrong at all: Places
# types "Digital Piano Review" as a school, "Suffolk Addressing Services" as a
# post office and "Rabbi Zachary Hepner, Mohel" as a place of worship. Each is
# a practice or a business renting space, and each would have taken a building
# off the prospect list.
#
# Corroborating against the building's own current_use was tried first and is
# worthless, because current_use comes from the Google sweep and carries the
# same mistake: 115 East 92 Street reads current_use "education" on
# current_use_name "Digital Piano Review", and 18 West 76 Street reads
# "government" on "Suffolk Addressing Services". Checking Places against
# Places agreed with itself on all 29 and changed nothing.
#
# So the test is the name, which is the one piece of evidence not derived from
# the type. A practice announces itself — a credential, a studio, an agency —
# and an institution announces itself too. Corporate suffixes are deliberately
# not practice markers: Zoe Ministries Inc. is a church.
#
# A rejected reading does not make a building available. It falls through to
# no building-level use at all, which leaves the building undetermined.
PRACTICE_MARKERS = re.compile(
    r"\b(m\.?d|d\.?d\.?s|d\.?o|ph\.?d|lmsw|l\.?c\.?s\.?w|r\.?n|esq|cpa)\b"
    r"|\bdr\.?\s|\bmohel\b|\bstudio\b|\breview\b|\bservices?\b"
    r"|\bassociates\b|\balumni\b|\bconsult|\bagency\b",
    re.I)

INSTITUTION_NOUNS = re.compile(
    r"church|chapel|cathedral|parish|congregation|synagogue|temple|masjid|"
    r"mosque|ministr|miss?i[oó]n|sanctuary|worship|tabernacle|gospel|baptist|"
    r"lutheran|methodist|catholic|presbyterian|episcopal|\bame\b|yeshiva|"
    r"school|escuela|academy|college|university|institute|preschool|montessori|"
    r"seminary|\bprep\b|hospital|medical cent|clinic|nursing|rehab|hospice|"
    r"library|courthouse|city hall|post office|embassy|consulate|precinct|"
    r"fire (station|house)|\bcenter\b|\bcentre\b|foundation|society",
    re.I)


def institution_is_plausible(name: str) -> bool:
    """Does this place plausibly occupy the building, or merely rent in it."""
    if PRACTICE_MARKERS.search(name or ""):
        return False
    return bool(INSTITUTION_NOUNS.search(name or ""))


def log(msg: str) -> None:
    print(msg, flush=True)


def nearby(lat: float, lon: float, retries: int = 5):
    body = json.dumps({
        "maxResultCount": 20,
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {
            "center": {"latitude": lat, "longitude": lon},
            "radius": SEARCH_RADIUS_M}},
    }).encode()
    req = urllib.request.Request(NEARBY_URL, data=body, headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as fh:
                return json.loads(fh.read()).get("places", []), None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()[:200]
            # The daily cap does not clear by retrying. 403 does — it was the
            # billing block on 2026-10-07 and the same call alternated OK and
            # 403 within a minute while the fix propagated.
            if exc.code == 429:
                return [], f"HTTP 429 TERMINAL {raw}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return [], f"HTTP {exc.code} {raw}"
        except Exception as exc:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return [], f"{type(exc).__name__}: {str(exc)[:60]}"
    return [], "exhausted"


def metres(a: tuple, b: tuple) -> float:
    (la1, lo1), (la2, lo2) = a, b
    radius = 6371000
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dlam = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def centroid(geometry: dict):
    if not geometry:
        return None
    if geometry["type"] == "Point":
        lon, lat = geometry["coordinates"]
        return lat, lon
    coords = geometry["coordinates"]
    ring = coords[0][0] if geometry["type"] == "MultiPolygon" else coords[0]
    if not ring:
        return None
    return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)


def _norm_addr(a: str) -> str:
    """An address in the form both datasets can be compared in.

    The compass direction is folded to its initial, never dropped. Dropping it
    reads 205 West 76th Street and 205 East 76th Street as the same address,
    which is two buildings on opposite sides of Central Park -- the precise
    false match this whole test exists to prevent.
    """
    a = re.sub(r"[^A-Z0-9 ]", " ", (a or "").upper().split(",")[0])
    a = re.sub(r"(?<=\d)(ST|ND|RD|TH)\b", "", a)
    for word, initial in (("EAST", "E"), ("WEST", "W"),
                          ("NORTH", "N"), ("SOUTH", "S")):
        a = re.sub(rf"\b{word}\b", initial, a)
    a = re.sub(r"\b(STREET|ST|AVENUE|AVE|PLACE|PL|ROAD|RD|BOULEVARD|BLVD)\b",
               " ", a)
    return " ".join(a.split())


def attaches(pl: dict, geometry: dict, addresses) -> tuple[bool, bool, str]:
    """Does this listing belong to the building: (attached, in_footprint, addr).

    Either the pin is inside the building's own footprint, or the listing's
    address is one the building answers to. Distance is not a third way --
    see _places_reading_is_about_this_building in deal_readiness for why 30m
    of proximity turned out to be no test at all.

    Alternate addresses matter more here than anywhere else in the pipeline.
    A corner building is listed by Google under whichever frontage its tenant
    gave, and a through-block building under either end; matching the primary
    address alone would throw away the listing that is actually about the
    building about as often as it kept it.
    """
    loc = pl.get("location") or {}
    inside = point_in_footprint(geometry, loc.get("longitude"), loc.get("latitude"))
    addr = pl.get("formattedAddress") or ""
    key = _norm_addr(addr)
    matched = bool(key) and any(key == _norm_addr(a) for a in addresses if a)
    return (inside or matched), inside, addr


def read(places: list, ll: tuple, geometry: dict = None, addresses=()) -> dict:
    """The one place that is the building, or a record that none was.

    Only primaryType decides. The `types` array on a Places result is a bag
    that routinely carries `establishment`, `point_of_interest` and `lodging`
    alongside the real one, so matching on it would let the generic types the
    guards exist to reject back in through the side door.
    """
    near = []
    for pl in places:
        loc = pl.get("location") or {}
        if not loc:
            continue
        d = metres(ll, (loc.get("latitude", 0), loc.get("longitude", 0)))
        if d <= MAX_DISTANCE_M:
            near.append((d, pl))
    near.sort(key=lambda x: x[0])

    for d, pl in near:
        primary = pl.get("primaryType") or ""
        if primary not in BUILDING_LEVEL:
            continue
        name = (pl.get("displayName") or {}).get("text", "")
        status = pl.get("businessStatus", "")
        if primary in LODGING_TYPES:
            kind = "lodging" if status in ("", "OPERATIONAL") else "lodging_closed"
        elif primary in RESIDENTIAL_TYPES:
            kind = "residential"
        else:
            if not institution_is_plausible(name):
                continue
            kind = "institutional"
        attached, inside, addr = attaches(pl, geometry, addresses)
        return {
            "nearby_use": kind,
            "nearby_use_name": name,
            "nearby_use_type": primary,
            "nearby_use_distance_m": round(d),
            "nearby_use_status": status,
            "nearby_use_basis": (
                f"{primary} '{name}' "
                + ("inside the footprint" if inside
                   else f"at {addr}" if attached
                   else f"{round(d)}m from the footprint, not this building")),
            "nearby_candidates": len(near),
            "nearby_use_attached": attached,
            "nearby_use_in_footprint": inside,
            "nearby_use_address": addr,
        }
    return {
        "nearby_use": "",
        "nearby_use_basis": (f"{len(near)} place(s) within {int(MAX_DISTANCE_M)}m, "
                             "none a building-level type"),
        "nearby_candidates": len(near),
    }


def load_features(args) -> list:
    path = Path(args.geojson) if args.geojson else None
    if path is None:
        from config import DATA_PROCESSED
        files = sorted(DATA_PROCESSED.glob("buildings_[0-9]*.geojson"), reverse=True)
        if not files:
            sys.exit("no build to read — run src/build_geojson.py first")
        path = files[0]
    log(f"  reading {path.name}")
    feats = json.loads(path.read_text())["features"]
    alts = load_alt_addresses()
    out = []
    for f in feats:
        p = f["properties"]
        bbl = str(p.get("bbl") or "")
        if not bbl:
            continue
        if args.bbl and bbl != args.bbl:
            continue
        if not args.bbl and not args.all and p.get("segment") != args.segment:
            continue
        ll = centroid(f.get("geometry"))
        if not ll:
            continue
        out.append({"bbl": bbl, "address": p.get("address", ""), "ll": ll,
                    "geometry": f.get("geometry"),
                    "addresses": [p.get("address", "")] + list(alts.get(bbl) or [])})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbl")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--segment", default="transient")
    ap.add_argument("--geojson")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("Set GOOGLE_API_KEY (the key the Places sweeps already use).")

    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    targets = load_features(args)
    if args.limit:
        targets = targets[: args.limit]
    log(f"  asking Nearby Search about {len(targets)} buildings")

    rows, resolved, consecutive = [], 0, 0
    for i, t in enumerate(targets, 1):
        cached = cache.get(t["bbl"]) if not args.bbl else None
        if cached is not None and "places" in cached:
            verdict = read(places := cached["places"], t["ll"],
                           t.get("geometry"), t.get("addresses") or ())
        else:
            places, err = nearby(t["ll"][0], t["ll"][1])
            if err and "TERMINAL" in err:
                log(f"\n  STOPPED at {i}/{len(targets)} — {err}")
                log("  Rows already written are complete; the rest were not asked.")
                break
            if err:
                consecutive += 1
                log(f"  [{i}] {t['address'][:34]:36} api_error {err[:50]}")
                if consecutive >= 5:
                    log("\n  STOPPED — 5 consecutive failures")
                    break
                continue
            consecutive = 0
            verdict = read(places, t["ll"],
                           t.get("geometry"), t.get("addresses") or ())
            if args.bbl:
                for pl in places[:10]:
                    loc = pl.get("location") or {}
                    d = metres(t["ll"], (loc.get("latitude", 0), loc.get("longitude", 0)))
                    log(f"      {round(d):>4}m {pl.get('primaryType',''):<26} "
                        f"{(pl.get('displayName') or {}).get('text','')[:40]}")
            # The raw answer, not the reading of it. A verdict cached under a
            # rule that later changes is a stale verdict nothing re-reads —
            # which is exactly how the committed C of O parse arrived carrying
            # a classifier three commits old. Caching the places means the next
            # guard costs nothing to apply.
            cache[t["bbl"]] = {"places": places}
            if i % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache, indent=2))
            time.sleep(0.12)

        row = {"bbl": t["bbl"], "address": t["address"], **verdict}
        rows.append(row)
        if verdict.get("nearby_use"):
            resolved += 1
            log(f"  [{i}/{len(targets)}] {t['address'][:32]:34} -> "
                f"{verdict['nearby_use']:16} {verdict['nearby_use_basis'][:52]}")

    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    merged = {}
    if OUTPUT_FILE.exists():
        merged = {r["bbl"]: r for r in json.loads(OUTPUT_FILE.read_text())}
    merged.update({r["bbl"]: r for r in rows})
    OUTPUT_FILE.write_text(json.dumps(list(merged.values()), indent=2))

    pct = (100 * resolved / len(rows)) if rows else 0
    log(f"\n  {resolved} of {len(rows)} resolved to a building-level use ({pct:.0f}%)")
    log(f"  wrote {OUTPUT_FILE.name} ({len(merged)} rows)")


if __name__ == "__main__":
    main()
