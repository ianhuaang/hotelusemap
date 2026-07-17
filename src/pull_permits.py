"""Pull DOB NOW job application filings for Manhattan, Brooklyn, and Queens.

Uses DOB NOW: Build dataset (w9ak-ipjd) which has current filings.
The legacy DOB dataset (ic3t-wcy2) stopped updating around 2020.
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, SOCRATA_BASE_URL

DOB_NOW_DATASET_ID = "w9ak-ipjd"

BATCH_SIZE = 5000
COLUMNS = [
    "bbl", "borough", "block", "lot", "bin",
    "job_type", "filing_status", "job_description",
    "filing_date", "current_status_date", "first_permit_date", "signoff_date",
    "initial_cost", "building_type",
    "owner_first_name", "owner_last_name", "owner_s_business_name",
]


def pull_permits() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"permits_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    select = ",".join(COLUMNS)
    cutoff = (date.today() - timedelta(days=365 * 3)).strftime("%Y-%m-%dT00:00:00")
    # Borough is title case in DOB NOW
    where = f"borough IN ('Manhattan', 'Brooklyn', 'Queens') AND current_status_date >= '{cutoff}'"
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
            "$order": "current_status_date DESC",
        })
        url = f"{SOCRATA_BASE_URL}/{DOB_NOW_DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            batch = json.loads(resp.read())

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} permits...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    outfile.write_text(json.dumps(all_rows, indent=2))
    print(f"Saved {len(all_rows)} permit rows -> {outfile}")
    return outfile


if __name__ == "__main__":
    pull_permits()
