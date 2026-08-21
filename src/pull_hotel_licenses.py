"""Pull DCWP hotel license data from NYC Open Data.

Uses the Legally Operating Businesses dataset (w7w3-xahh) filtered to
business_category='Hotel'. Includes BBL and BIN for direct pipeline matching.
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

DCWP_DATASET_ID = "w7w3-xahh"

COLUMNS = [
    "license_nbr", "business_name", "business_category", "license_type",
    "license_status", "license_creation_date", "lic_expir_dd",
    "contact_phone", "address_building", "address_street_name",
    "address_city", "address_state", "address_zip", "address_borough",
    "community_board", "bin", "bbl",
]

TARGET_BOROUGHS = ("Manhattan", "Brooklyn", "Queens")


def pull_hotel_licenses() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"hotel_licenses_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    select = ",".join(COLUMNS)
    borough_list = ",".join(f"'{b}'" for b in TARGET_BOROUGHS)
    where = f"business_category='Hotel' AND address_borough IN ({borough_list})"

    params = urllib.parse.urlencode({
        "$select": select,
        "$where": where,
        "$limit": 5000,
        "$order": "address_borough,business_name",
    })
    url = f"{SOCRATA_BASE_URL}/{DCWP_DATASET_ID}.json?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

    rows = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                rows = json.loads(resp.read())
            break
        except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
            if attempt == 3:
                raise
            wait = 10 * (attempt + 1)
            print(f"  retry {attempt + 1}: {e} (waiting {wait}s)")
            time.sleep(wait)

    if not rows:
        print("No hotel license data returned")
        return outfile

    outfile.write_text(json.dumps(rows, indent=2))

    # Summary
    from collections import Counter
    by_status = Counter(r.get("license_status", "") for r in rows)
    by_borough = Counter(r.get("address_borough", "") for r in rows)
    print(f"Saved {len(rows)} hotel licenses -> {outfile}")
    print(f"  By borough: {dict(by_borough)}")
    print(f"  By status: {dict(by_status)}")
    with_bbl = sum(1 for r in rows if r.get("bbl"))
    print(f"  With BBL: {with_bbl}/{len(rows)}")

    return outfile


if __name__ == "__main__":
    pull_hotel_licenses()
