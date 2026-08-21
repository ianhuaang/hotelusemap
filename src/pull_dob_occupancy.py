"""Pull DOB occupancy classifications from legacy filings.

Queries the legacy DOB jobs dataset (ic3t-wcy2) for existing_occupancy
and proposed_occupancy values R-1 (transient) and J-1 (transient
residential / SRO).

Two modes:
  - Full borough pull: all R-1/J-1 filings across MN/BK/QN (discovery)
  - Pipeline-only: just pipeline BBLs (legacy behavior)

The full pull serves as a pipeline entry source — buildings with R-1/J-1
occupancy that don't meet PLUTO filter criteria still have confirmed
transient capacity and should enter the pipeline.
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
from config import DATA_RAW, SOCRATA_BASE_URL, DOB_FILINGS_DATASET_ID

TODAY = date.today().strftime("%Y%m%d")
BATCH_SIZE = 5000

TRANSIENT_OCCUPANCY_CODES = {"R-1", "J-1"}
TARGET_BOROUGHS = ("MANHATTAN", "BROOKLYN", "QUEENS")


def _fetch_paginated(where: str, ctx) -> list[dict]:
    select = ("bbl, existing_occupancy, proposed_occupancy, "
              "existing_dwelling_units, proposed_dwelling_units, "
              "pre__filing_date, job_description, borough")
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
            "$order": "bbl,pre__filing_date DESC",
        })
        url = f"{SOCRATA_BASE_URL}/{DOB_FILINGS_DATASET_ID}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                    batch = json.loads(resp.read())
                break
            except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
                if attempt == 3:
                    raise
                wait = 10 * (attempt + 1)
                print(f"  retry {attempt + 1}: {e} (waiting {wait}s)")
                time.sleep(wait)

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} filings...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    return all_rows


def _aggregate_by_bbl(rows: list[dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}

    for row in rows:
        bbl = str(row.get("bbl", "")).strip()
        if not bbl:
            continue

        ex_occ = (row.get("existing_occupancy") or "").strip()
        pr_occ = (row.get("proposed_occupancy") or "").strip()
        matched_occ = None
        if ex_occ in TRANSIENT_OCCUPANCY_CODES:
            matched_occ = ex_occ
        elif pr_occ in TRANSIENT_OCCUPANCY_CODES:
            matched_occ = pr_occ
        if not matched_occ:
            continue

        filing_date = (row.get("pre__filing_date") or "")[:10]
        du = row.get("existing_dwelling_units") or row.get("proposed_dwelling_units") or ""
        desc = (row.get("job_description") or "")[:200]

        if bbl not in results:
            results[bbl] = {
                "bbl": bbl,
                "has_r1": False,
                "has_j1": False,
                "r1_filing_count": 0,
                "j1_filing_count": 0,
                "earliest_date": filing_date,
                "latest_date": filing_date,
                "max_dwelling_units": 0,
                "sample_description": "",
            }

        entry = results[bbl]
        if matched_occ == "R-1":
            entry["has_r1"] = True
            entry["r1_filing_count"] += 1
        elif matched_occ == "J-1":
            entry["has_j1"] = True
            entry["j1_filing_count"] += 1

        if filing_date and (not entry["earliest_date"] or filing_date < entry["earliest_date"]):
            entry["earliest_date"] = filing_date
        if filing_date and filing_date > entry["latest_date"]:
            entry["latest_date"] = filing_date

        try:
            du_int = int(du)
            if du_int > entry["max_dwelling_units"]:
                entry["max_dwelling_units"] = du_int
        except (ValueError, TypeError):
            pass

        if not entry["sample_description"] and desc:
            entry["sample_description"] = desc

    return results


def pull_dob_occupancy() -> Path:
    """Full borough-wide pull of all R-1/J-1 occupancy filings."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    outfile = DATA_RAW / f"dob_occupancy_{TODAY}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    borough_list = ",".join(f"'{b}'" for b in TARGET_BOROUGHS)
    where = (f"(existing_occupancy in('R-1','J-1') "
             f"OR proposed_occupancy in('R-1','J-1')) "
             f"AND borough in({borough_list})")

    print(f"Pulling all R-1/J-1 filings from {', '.join(TARGET_BOROUGHS)}...")
    rows = _fetch_paginated(where, ctx)
    print(f"Total filings: {len(rows)}")

    results = _aggregate_by_bbl(rows)

    outfile.write_text(json.dumps(list(results.values()), indent=2))
    r1_count = sum(1 for r in results.values() if r["has_r1"])
    j1_count = sum(1 for r in results.values() if r["has_j1"])
    both = sum(1 for r in results.values() if r["has_r1"] and r["has_j1"])
    print(f"Saved {len(results)} unique BBLs with transient occupancy -> {outfile}")
    print(f"  R-1 (transient): {r1_count}")
    print(f"  J-1 (transient residential): {j1_count}")
    print(f"  Both R-1 and J-1: {both}")
    return outfile


if __name__ == "__main__":
    pull_dob_occupancy()
