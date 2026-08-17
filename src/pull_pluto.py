"""Pull PLUTO data for target areas from NYC Open Data. Immutable, date-stamped output.

Pulls all of Manhattan + specific community districts in Brooklyn and Queens.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, SOCRATA_BASE_URL, PLUTO_DATASET_ID, TARGET_CDS

BATCH_SIZE = 5000
COLUMNS = [
    "bbl", "borough", "block", "lot", "address", "bldgclass", "landuse",
    "unitsres", "unitstotal", "numfloors", "numbldgs", "bldgarea", "comarea",
    "resarea", "lotarea", "ownername", "ownertype", "zonedist1", "cd",
    "yearbuilt", "yearalter1", "yearalter2",
]


def pull_pluto() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"pluto_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    select = ",".join(COLUMNS)
    # All of Manhattan + target CDs in Brooklyn/Queens
    cd_list = ",".join(f"'{cd}'" for cd in sorted(TARGET_CDS))
    where = f"borough='MN' OR cd IN ({cd_list})"
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
            "$order": "bbl",
        })
        url = f"{SOCRATA_BASE_URL}/{PLUTO_DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        batch = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                    batch = json.loads(resp.read())
                break
            except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
                if attempt == 3:
                    raise
                wait = 10 * (attempt + 1)
                print(f"  retry {attempt + 1} at offset {offset}: {e} (waiting {wait}s)")
                time.sleep(wait)

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} rows...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    outfile.write_text(json.dumps(all_rows, indent=2))
    print(f"Saved {len(all_rows)} PLUTO rows -> {outfile}")
    return outfile


if __name__ == "__main__":
    pull_pluto()
