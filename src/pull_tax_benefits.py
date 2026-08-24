"""Pull 421-a and J-51 tax exemption data from NYC Open Data.

Dataset: muvi-b6kx (Property Exemption Detail)
Buildings with active 421-a or J-51 benefits have rent stabilization
obligations and restrictions on changes of use.

BBL is constructed from boro + block + lot (no pre-joined field).
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

DATASET_ID = "muvi-b6kx"
BATCH_SIZE = 5000
TODAY = date.today().strftime("%Y%m%d")

# 421-a: exmp_codes 1010, 1015, 1019 (nys_exmp_code 418xx)
# J-51: exmp_codes 1920, 1925, 1985, 1986, 51xx series (nys_exmp_code 480xx/479xx)
CODES_421A = {"1010", "1015", "1019"}
CODES_J51 = {"1920", "1925", "1985", "1986"}
# 51xx codes are also J-51 (5101, 5106, 5110-5130)
TARGET_BOROUGHS = ("1", "3", "4")  # MN, BK, QN


def _make_bbl(boro, block, lot):
    try:
        return f"{int(boro)}{int(block):05d}{int(lot):04d}"
    except (ValueError, TypeError):
        return ""


def pull_tax_benefits() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    outfile = DATA_RAW / f"tax_benefits_{TODAY}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    current_year = date.today().year

    borough_list = ",".join(f"'{b}'" for b in TARGET_BOROUGHS)
    all_codes = ",".join(f"'{c}'" for c in (CODES_421A | CODES_J51))

    by_bbl = {}
    offset = 0
    select = "boro,block,lot,exmp_code,benftstart,no_years"
    where = (f"boro in({borough_list}) "
             f"AND exmp_code in({all_codes})")
    total_fetched = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
        })
        url = f"{SOCRATA_BASE_URL}/{DATASET_ID}.json?{params}"
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

        for row in batch:
            bbl = _make_bbl(row.get("boro"), row.get("block"), row.get("lot"))
            if not bbl:
                continue
            exmp_code = (row.get("exmp_code") or "").strip()
            benefit_start = (row.get("benftstart") or "").replace("+", "").strip()
            num_years = int(row.get("no_years") or 0)

            try:
                start_year = int(benefit_start[:4]) if benefit_start and benefit_start != "0" else None
                expires = start_year + num_years if start_year and num_years > 0 else None
            except (ValueError, TypeError):
                start_year = None
                expires = None

            is_active = expires is None or expires >= current_year
            benefit_type = "421-a" if exmp_code in CODES_421A else "J-51"

            if bbl not in by_bbl or (is_active and not by_bbl[bbl]["is_active"]) or \
               (is_active and expires and (by_bbl[bbl].get("benefit_expires") is None or expires > by_bbl[bbl]["benefit_expires"])):
                by_bbl[bbl] = {
                    "bbl": bbl,
                    "benefit_type": benefit_type,
                    "exmp_code": exmp_code,
                    "benefit_start": str(start_year) if start_year else "",
                    "benefit_years": num_years,
                    "benefit_expires": expires,
                    "is_active": is_active,
                }

        total_fetched += len(batch)
        if total_fetched % 50000 == 0 or len(batch) < BATCH_SIZE:
            print(f"  fetched {total_fetched} records, {len(by_bbl)} unique BBLs...")
        offset += BATCH_SIZE
        time.sleep(0.3)

    print(f"Total records: {total_fetched}, Unique BBLs: {len(by_bbl)}")

    active = sum(1 for v in by_bbl.values() if v["is_active"])
    j51 = sum(1 for v in by_bbl.values() if v["benefit_type"] == "J-51")
    a421 = sum(1 for v in by_bbl.values() if v["benefit_type"] == "421-a")

    outfile.write_text(json.dumps(list(by_bbl.values()), indent=2))
    print(f"Saved {len(by_bbl)} BBLs with tax benefits -> {outfile}")
    print(f"  421-a: {a421}")
    print(f"  J-51: {j51}")
    print(f"  Currently active: {active}")
    return outfile


if __name__ == "__main__":
    pull_tax_benefits()
