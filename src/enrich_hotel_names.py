"""Enrich hotel name data via Google Places API.

For buildings with DCWP hotel licenses where the business_name is just an LLC,
queries Google Places to find the actual hotel name (e.g., "DoubleTree by Hilton"
instead of "350 West Forty Manager LLC"). Results are cached to avoid repeated
API calls.
"""

import json
import os
import ssl
import time
import urllib.request
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW

CTX = ssl.create_default_context(cafile=certifi.where())
TODAY = date.today().strftime("%Y%m%d")
API_KEY = os.environ.get("GOOGLE_API_KEY", "")
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
CACHE_FILE = DATA_RAW / "google_hotel_names_cache.json"
OUTPUT_FILE = DATA_RAW / f"google_hotel_names_{TODAY}.json"

LLC_SUFFIXES = ("LLC", "L.L.C", "INC", "CORP", "CORPORATION", "LTD", "LP", "L.P.")


def _looks_like_llc(name: str) -> bool:
    upper = name.strip().upper()
    return any(upper.endswith(s) or f" {s} " in f" {upper} " or f" {s}." in f" {upper}." for s in LLC_SUFFIXES)


def _places_search(query: str, included_type: str = ""):
    """Raw Places API text search. Returns first place or None."""
    payload = {"textQuery": query, "maxResultCount": 1}
    if included_type:
        payload["includedType"] = included_type
    body = json.dumps(payload).encode()

    req = urllib.request.Request(
        SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "places.id,places.displayName,places.types",
        },
    )
    resp = urllib.request.urlopen(req, context=CTX, timeout=15)
    data = json.loads(resp.read())
    places = data.get("places", [])
    return places[0] if places else None


def search_hotel_by_address(address: str, biz_name: str = ""):
    """Search Google Places for a hotel at a given address.

    Strategy:
    1. Type-filtered search (includedType=hotel) — most precise
    2. Fall back to unfiltered search with biz name + address, check types
    """
    addr_query = f"{address}, New York, NY"

    try:
        # Pass 1: type-filtered by address
        place = _places_search(addr_query, included_type="hotel")
        if place:
            return _extract(place)

        # Pass 2: biz name + address, no type filter, check types in response
        if biz_name:
            place = _places_search(f"{biz_name}, {addr_query}")
            if place and _is_lodging(place):
                return _extract(place)

        # Pass 3: "hotel near" + address, no type filter
        place = _places_search(f"hotel near {addr_query}")
        if place and _is_lodging(place):
            return _extract(place)
    except Exception as e:
        print(f"    Error searching {address}: {e}")
    return None


def _is_lodging(place: dict) -> bool:
    types = place.get("types", [])
    return "lodging" in types or "hotel" in types


def _extract(place: dict) -> dict:
    return {
        "google_name": place.get("displayName", {}).get("text", ""),
        "place_id": place.get("id", ""),
        "types": place.get("types", []),
    }


def load_hotel_licenses() -> list[dict]:
    files = sorted(DATA_RAW.glob("hotel_licenses_*.json"), reverse=True)
    if not files:
        return []
    return json.loads(files[0].read_text())


def enrich():
    licenses = load_hotel_licenses()
    active = [l for l in licenses if l.get("license_status") in ("Active", "Ready for Renewal")]
    print(f"Active/renewal hotel licenses: {len(active)}")

    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())

    results = []
    new_lookups = 0
    cached_hits = 0
    errors = 0

    for i, lic in enumerate(active):
        bbl = (lic.get("bbl") or "").strip()
        biz_name = (lic.get("business_name") or "").strip()
        addr = f"{lic.get('address_building', '')} {lic.get('address_street_name', '')}".strip()

        if not bbl or not addr:
            continue

        if not _looks_like_llc(biz_name):
            continue

        cache_key = f"{bbl}|{addr}"

        if cache_key in cache:
            result = cache[cache_key]
            if result:
                cached_hits += 1
                results.append({"bbl": bbl, "address": addr, **result})
            continue

        result = search_hotel_by_address(addr, biz_name)
        if result:
            cache[cache_key] = result
            results.append({"bbl": bbl, "address": addr, **result})
            new_lookups += 1
        else:
            cache[cache_key] = None
            errors += 1

        time.sleep(0.15)

        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(active)}: {new_lookups} new, {cached_hits} cached, {errors} miss")
            CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))

    CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))
    OUTPUT_FILE.write_text(json.dumps(results, indent=2, default=str))

    print(f"\nResults: {len(results)} hotel names resolved")
    print(f"  New API lookups: {new_lookups}")
    print(f"  From cache: {cached_hits}")
    print(f"  No result: {errors}")
    print(f"Saved -> {OUTPUT_FILE}")
    return results


if __name__ == "__main__":
    if not API_KEY:
        print("Set GOOGLE_API_KEY environment variable")
        print("  export GOOGLE_API_KEY=your_key_here")
        exit(1)

    print("Testing API...")
    test = search_hotel_by_address("346 W 40th St", "350 West Forty Manager LLC")
    if test:
        print(f"  OK: {test['google_name']}")
    else:
        print("  API test failed — could not find hotel at 346 W 40th St")
        print("  (API works but address may not match a hotel; continuing anyway)")

    test2 = search_hotel_by_address("70 Park Avenue")
    if test2:
        print(f"  OK: {test2['google_name']}")
    else:
        print("  Warning: could not find hotel at 70 Park Ave")

    enrich()
