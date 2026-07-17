"""Pull DOF property sales for Manhattan, Brooklyn, and Queens.

Uses the NYC Citywide Annualized Calendar Sales Update dataset.
Only pulls sales from the last 10 years to keep volume reasonable.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, SOCRATA_BASE_URL, DOF_SALES_DATASET_ID

BATCH_SIZE = 5000
COLUMNS = [
    "bbl", "borough", "block", "lot", "address",
    "sale_price", "sale_date", "year_built",
    "building_class_category", "residential_units", "commercial_units",
    "gross_square_feet",
]


def pull_sales() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"sales_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    select = ",".join(COLUMNS)
    cutoff = (date.today() - timedelta(days=365 * 10)).isoformat()
    # Boroughs: 1=Manhattan, 3=Brooklyn, 4=Queens
    # Only sales > $10k to filter out $0 transfers and nominal sales
    where = f"borough IN ('1', '3', '4') AND sale_date >= '{cutoff}' AND sale_price > 10000"
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
            "$order": "sale_date DESC",
        })
        url = f"{SOCRATA_BASE_URL}/{DOF_SALES_DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                    batch = json.loads(resp.read())
                break
            except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
                if attempt == 2:
                    raise
                print(f"  retry {attempt + 1} at offset {offset}: {e}")
                time.sleep(5 * (attempt + 1))

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} sales...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    outfile.write_text(json.dumps(all_rows, indent=2))
    print(f"Saved {len(all_rows)} sales rows -> {outfile}")
    return outfile


if __name__ == "__main__":
    pull_sales()
