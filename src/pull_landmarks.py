"""Pull LPC landmark and historic district data from NYC Open Data.

Dataset: gpmc-yuvp (LPC Individual Landmark and Historic District Buildings)
Keyed by BBL — join directly to pipeline.
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
from config import DATA_RAW, SOCRATA_BASE_URL

DATASET_ID = "gpmc-yuvp"
BATCH_SIZE = 5000
TODAY = date.today().strftime("%Y%m%d")

COLUMNS = "bbl,bin,borough,block,lot,des_addres,hist_dist,lm_orig,build_type,use_orig,style_prim"


def pull_landmarks() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    outfile = DATA_RAW / f"landmarks_{TODAY}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    borough_filter = "borough in('MN','BK','QN')"
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": COLUMNS,
            "$where": borough_filter,
            "$limit": BATCH_SIZE,
            "$offset": offset,
        })
        url = f"{SOCRATA_BASE_URL}/{DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                    batch = json.loads(resp.read())
                break
            except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
                if attempt == 2:
                    raise
                print(f"  retry {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} landmark records...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    # Aggregate by BBL
    by_bbl = {}
    for row in all_rows:
        bbl = (row.get("bbl") or "").strip()
        if not bbl:
            continue
        if bbl not in by_bbl:
            by_bbl[bbl] = {
                "bbl": bbl,
                "is_individual_landmark": False,
                "landmark_name": "",
                "is_historic_district": False,
                "historic_district": "",
            }
        entry = by_bbl[bbl]
        lm = (row.get("lm_orig") or "").strip()
        hd = (row.get("hist_dist") or "").strip()
        if lm and lm != "0":
            entry["is_individual_landmark"] = True
            entry["landmark_name"] = lm
        if hd and hd != "0":
            entry["is_historic_district"] = True
            entry["historic_district"] = hd

    outfile.write_text(json.dumps(list(by_bbl.values()), indent=2))

    individual = sum(1 for v in by_bbl.values() if v["is_individual_landmark"])
    district = sum(1 for v in by_bbl.values() if v["is_historic_district"])
    print(f"Saved {len(by_bbl)} landmark BBLs -> {outfile}")
    print(f"  Individual landmarks: {individual}")
    print(f"  Historic district buildings: {district}")
    return outfile


if __name__ == "__main__":
    pull_landmarks()
