"""Enrich H-class buildings that have no hotel name or DCWP license.

Uses Google Places API to check if a hotel exists at each address.
Results are merged into the existing Google hotel names cache.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED
from src.enrich_hotel_names import search_hotel_by_address, CACHE_FILE

TODAY = __import__("datetime").date.today().strftime("%Y%m%d")


def load_pipeline():
    files = sorted(DATA_PROCESSED.glob("pipeline_*.json"), reverse=True)
    if not files:
        print("No pipeline file found")
        return []
    return json.loads(files[0].read_text())


def main():
    pipeline = load_pipeline()
    hclass = [
        r for r in pipeline
        if r.get("bldgclass", "").startswith("H")
        and not r.get("hotel_name")
        and not r.get("has_hotel_license")
    ]
    print(f"H-class buildings without hotel name or license: {len(hclass)}")

    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    results = []
    new_lookups = 0
    cached = 0
    no_result = 0

    for i, r in enumerate(hclass):
        bbl = r["bbl"]
        addr = r.get("address", "")
        if not addr:
            continue

        cache_key = f"{bbl}|{addr}"
        if cache_key in cache:
            if cache[cache_key]:
                cached += 1
                results.append({"bbl": bbl, "address": addr, **cache[cache_key]})
            continue

        result = search_hotel_by_address(addr)
        if result:
            cache[cache_key] = result
            results.append({"bbl": bbl, "address": addr, **result})
            new_lookups += 1
            print(f"  FOUND: {addr} -> {result['google_name']}")
        else:
            cache[cache_key] = None
            no_result += 1

        time.sleep(0.15)

        if (i + 1) % 25 == 0:
            print(f"  Progress: {i+1}/{len(hclass)} ({new_lookups} found, {no_result} miss)")
            CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))

    CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))

    outpath = DATA_RAW / f"google_hclass_hotels_{TODAY}.json"
    outpath.write_text(json.dumps(results, indent=2, default=str))

    print(f"\nDone: {len(results)} hotels identified")
    print(f"  New API lookups: {new_lookups}")
    print(f"  From cache: {cached}")
    print(f"  No result: {no_result}")
    print(f"Saved -> {outpath}")


if __name__ == "__main__":
    import os
    if not os.environ.get("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY environment variable")
        exit(1)
    main()
