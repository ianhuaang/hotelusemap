"""Pull alternate addresses for each BBL from the DOB legacy jobs dataset.

Queries ic3t-wcy2 for all distinct house__ + street_name combinations per BBL,
merges in the PLUTO address from the pipeline output, normalises, and deduplicates.
"""

import json
import math
import re
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
from config import DATA_PROCESSED, SOCRATA_BASE_URL, DOB_FILINGS_DATASET_ID

TODAY = date.today().strftime("%Y%m%d")
BATCH_SIZE = 50  # BBLs per Socrata request

# Street suffix normalisation map
SUFFIX_MAP = {
    "ST": "STREET",
    "AVE": "AVENUE",
    "BLVD": "BOULEVARD",
    "DR": "DRIVE",
    "RD": "ROAD",
    "CT": "COURT",
    "PL": "PLACE",
    "LN": "LANE",
    "PKWY": "PARKWAY",
    "PLZ": "PLAZA",
    "SQ": "SQUARE",
    "TER": "TERRACE",
    "CIR": "CIRCLE",
    "HWY": "HIGHWAY",
    "EXPWY": "EXPRESSWAY",
    "EXPY": "EXPRESSWAY",
    "TPK": "TURNPIKE",
    "TPKE": "TURNPIKE",
}

# Build a regex that matches any abbreviated suffix as a whole word at end of string
_SUFFIX_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(SUFFIX_MAP, key=len, reverse=True)) + r")\.?$"
)


def _normalise_address(addr: str) -> str:
    """Uppercase, strip, and expand common street-suffix abbreviations."""
    addr = addr.upper().strip()
    # Collapse multiple spaces
    addr = re.sub(r"\s+", " ", addr)
    # Expand suffix abbreviations
    addr = _SUFFIX_PATTERN.sub(lambda m: SUFFIX_MAP[m.group(1)], addr)
    return addr


def _load_pipeline_bbls() -> dict:
    """Load the latest pipeline output and return {bbl: pluto_address}."""
    # Find the most recent pipeline file
    files = sorted(DATA_PROCESSED.glob("pipeline_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError("No pipeline_*.json found in data/processed/")

    path = files[0]
    print(f"Reading pipeline BBLs from {path.name}")

    with open(path) as f:
        rows = json.load(f)

    bbl_to_pluto_addr = {}
    for row in rows:
        bbl = str(row["bbl"])
        addr = row.get("address", "")
        bbl_to_pluto_addr[bbl] = addr
    print(f"  {len(bbl_to_pluto_addr)} BBLs loaded")
    return bbl_to_pluto_addr


def _fetch_batch(bbls: list[str], ctx) -> list[dict]:
    """Query Socrata for distinct house__ + street_name for a batch of BBLs."""
    bbl_list = ",".join(f"'{b}'" for b in bbls)
    params = urllib.parse.urlencode({
        "$select": "bbl, house__, street_name",
        "$where": f"bbl IN ({bbl_list})",
        "$limit": 50000,
    })
    url = f"{SOCRATA_BASE_URL}/{DOB_FILINGS_DATASET_ID}.json?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                return json.loads(resp.read())
        except (ConnectionResetError, TimeoutError, urllib.error.URLError) as e:
            if attempt == 0:
                print(f"    retry after error: {e}")
                time.sleep(5)
            else:
                print(f"    skipping batch after 2 failures: {e}")
                return []


def pull_alt_addresses() -> Path:
    """Pull alternate addresses for all pipeline BBLs and save to JSON."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    outfile = DATA_PROCESSED / f"alt_addresses_{TODAY}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    bbl_to_pluto_addr = _load_pipeline_bbls()
    bbls = list(bbl_to_pluto_addr.keys())
    total_batches = math.ceil(len(bbls) / BATCH_SIZE)

    # Collect DOB addresses per BBL
    bbl_addresses: dict[str, set[str]] = {bbl: set() for bbl in bbls}

    for i in range(0, len(bbls), BATCH_SIZE):
        batch_num = i // BATCH_SIZE + 1
        batch = bbls[i : i + BATCH_SIZE]
        print(f"Pulling alt addresses: batch {batch_num}/{total_batches}...")

        rows = _fetch_batch(batch, ctx)
        for row in rows:
            bbl = str(row.get("bbl", ""))
            house = (row.get("house__") or "").strip()
            street = (row.get("street_name") or "").strip()
            if house and street and bbl in bbl_addresses:
                raw = f"{house} {street}"
                bbl_addresses[bbl].add(_normalise_address(raw))

        time.sleep(0.5)

    # Merge in PLUTO addresses
    for bbl, pluto_addr in bbl_to_pluto_addr.items():
        if pluto_addr:
            bbl_addresses[bbl].add(_normalise_address(pluto_addr))

    # Convert sets to sorted lists
    result = {bbl: sorted(addrs) for bbl, addrs in bbl_addresses.items() if addrs}

    outfile.write_text(json.dumps(result, indent=2))
    print(f"Saved {len(result)} BBLs with alt addresses -> {outfile}")
    return outfile


if __name__ == "__main__":
    pull_alt_addresses()
