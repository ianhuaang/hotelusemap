"""Load rent stabilization unit counts from nycdb/taxbill data.

Source: rentstab_v2 CSV from JustFix/nycdb
  https://s3.amazonaws.com/justfix-data/rentstab_counts_from_doffer_2023.csv

Contains per-building stabilized unit counts from DOF tax bills (2018-2023),
keyed by BBL. We take the most recent non-zero count as the current figure.
"""

import csv
import json
import ssl
import urllib.request
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW

CSV_URL = "https://s3.amazonaws.com/justfix-data/rentstab_counts_from_doffer_2023.csv"
TODAY = date.today().strftime("%Y%m%d")
TARGET_BOROUGHS = {"1", "3", "4"}  # MN, BK, QN
YEARS = list(range(2023, 2017, -1))


def _safe_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def pull_rent_stabilization() -> Path:
    outfile = DATA_RAW / f"rent_stabilization_{TODAY}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    csv_path = DATA_RAW / "rentstab_v2.csv"
    if not csv_path.exists():
        print("Downloading rent stabilization CSV...")
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(CSV_URL, headers={"User-Agent": "nyc-transient-capacity/0.1"})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            csv_path.write_bytes(resp.read())
        print(f"  Downloaded {csv_path.stat().st_size / 1e6:.1f} MB")
    else:
        print(f"Using cached CSV: {csv_path}")

    with open(csv_path) as f:
        rows = list(csv.DictReader(f))

    results = []
    for r in rows:
        bbl = (r.get("ucbbl") or "").strip()
        if not bbl or bbl[0] not in TARGET_BOROUGHS:
            continue

        latest_count = 0
        latest_year = None
        for y in YEARS:
            v = _safe_int(r.get(f"uc{y}"))
            if v > 0:
                latest_count = v
                latest_year = y
                break

        if latest_count > 0:
            results.append({
                "bbl": bbl,
                "stabilized_units": latest_count,
                "data_year": latest_year,
            })

    outfile.write_text(json.dumps(results, indent=2))

    total_units = sum(r["stabilized_units"] for r in results)
    by_boro = {}
    for r in results:
        b = r["bbl"][0]
        by_boro.setdefault(b, {"count": 0, "units": 0})
        by_boro[b]["count"] += 1
        by_boro[b]["units"] += r["stabilized_units"]

    print(f"Saved {len(results)} buildings with {total_units:,} stabilized units -> {outfile}")
    boro_names = {"1": "Manhattan", "3": "Brooklyn", "4": "Queens"}
    for b in sorted(by_boro):
        print(f"  {boro_names.get(b, b)}: {by_boro[b]['count']:,} buildings, {by_boro[b]['units']:,} units")

    return outfile


if __name__ == "__main__":
    pull_rent_stabilization()
