"""Pull building footprints for Manhattan from NYC Open Data.

Footprints are large (~300k rows citywide). We filter to Manhattan by joining
on base_bbl starting with '1' (Manhattan borough code).
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, SOCRATA_BASE_URL, BUILDING_FOOTPRINTS_DATASET_ID

BATCH_SIZE = 5000
# GeoJSON output — we need the_geom, base_bbl, and key attributes
COLUMNS = [
    "the_geom", "base_bbl", "mappluto_bbl", "bin", "height_roof",
    "construction_year", "feature_code", "doitt_id",
]


def pull_footprints_manhattan() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"footprints_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    select = ",".join(COLUMNS)
    # base_bbl is text; Manhattan BBLs start with '1'
    where = "base_bbl like '1%'"
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
            "$order": "base_bbl",
        })
        url = f"{SOCRATA_BASE_URL}/{BUILDING_FOOTPRINTS_DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            batch = json.loads(resp.read())

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} footprints...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    outfile.write_text(json.dumps(all_rows, indent=2))
    print(f"Saved {len(all_rows)} footprint rows -> {outfile}")
    return outfile


if __name__ == "__main__":
    pull_footprints_manhattan()
