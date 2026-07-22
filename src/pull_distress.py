"""Pull financial distress and violation signals from NYC Open Data.

Four datasets:
  1. HPD Violations — open violations by severity (A/B/C)
  2. DOB ECB Violations — OATH summonses with penalties/balance due
  3. Tax Lien Sale Lists — properties with outstanding tax liens
  4. ACRIS Lis Pendens — litigation filings against properties

All filtered to our coverage boroughs (Manhattan, Brooklyn, Queens).
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
from config import (
    DATA_RAW, SOCRATA_BASE_URL,
    HPD_VIOLATIONS_DATASET_ID, DOB_ECB_VIOLATIONS_DATASET_ID,
    TAX_LIENS_DATASET_ID, ACRIS_LEGALS_DATASET_ID, ACRIS_MASTER_DATASET_ID,
)

BATCH_SIZE = 5000
TODAY = date.today().strftime("%Y%m%d")


def _fetch_all(dataset_id, params_base, label="records"):
    """Generic Socrata paginated fetch."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    all_rows = []
    offset = 0

    while True:
        params = {**params_base, "$limit": BATCH_SIZE, "$offset": offset}
        url = f"{SOCRATA_BASE_URL}/{dataset_id}.json?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
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
        print(f"  fetched {len(all_rows)} {label}...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    return all_rows


def pull_hpd_violations() -> Path:
    """Pull open HPD violations for Manhattan/Brooklyn/Queens."""
    outfile = DATA_RAW / f"hpd_violations_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    print("Pulling HPD violations (open only)...")

    rows = _fetch_all(
        HPD_VIOLATIONS_DATASET_ID,
        {
            "$select": "boroid,block,lot,class,violationstatus,inspectiondate,currentstatusdate,rentimpairing",
            "$where": "violationstatus='Open' AND boroid IN ('1','3','4')",
            "$order": "inspectiondate DESC",
        },
        label="HPD violations",
    )

    # Build BBL and simplify
    for r in rows:
        boro = str(r.get("boroid", ""))
        block = str(r.get("block", "")).zfill(5)
        lot = str(r.get("lot", "")).zfill(4)
        r["bbl"] = f"{boro}{block}{lot}"

    outfile.write_text(json.dumps(rows, indent=2))
    print(f"Saved {len(rows)} HPD violations -> {outfile}")
    return outfile


def pull_ecb_violations() -> Path:
    """Pull active DOB ECB violations with balance due."""
    outfile = DATA_RAW / f"ecb_violations_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    print("Pulling DOB ECB violations (active)...")

    rows = _fetch_all(
        DOB_ECB_VIOLATIONS_DATASET_ID,
        {
            "$select": "boro,block,lot,bin,ecb_violation_status,severity,violation_type,issue_date,penality_imposed,balance_due",
            "$where": "ecb_violation_status='ACTIVE' AND boro IN ('1','3','4')",
            "$order": "issue_date DESC",
        },
        label="ECB violations",
    )

    for r in rows:
        boro = str(r.get("boro", ""))
        block = str(r.get("block", "")).zfill(5)
        lot = str(r.get("lot", "")).zfill(4)
        r["bbl"] = f"{boro}{block}{lot}"

    outfile.write_text(json.dumps(rows, indent=2))
    print(f"Saved {len(rows)} ECB violations -> {outfile}")
    return outfile


def pull_tax_liens() -> Path:
    """Pull tax lien sale list entries."""
    outfile = DATA_RAW / f"tax_liens_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    print("Pulling tax lien sale list...")

    rows = _fetch_all(
        TAX_LIENS_DATASET_ID,
        {
            "$select": "borough,block,lot,cycle,tax_class_code,building_class,water_debt_only",
            "$where": "borough IN ('1','3','4')",
            "$order": "borough,block,lot",
        },
        label="tax liens",
    )

    for r in rows:
        boro = str(r.get("borough", ""))
        block = str(r.get("block", "")).zfill(5)
        lot = str(r.get("lot", "")).zfill(4)
        r["bbl"] = f"{boro}{block}{lot}"

    outfile.write_text(json.dumps(rows, indent=2))
    print(f"Saved {len(rows)} tax lien entries -> {outfile}")
    return outfile


def pull_lis_pendens() -> Path:
    """Pull ACRIS distress filings (last 5 years).

    Pulls LTPA (lis pendens / lien attachments) and JUDG (judgments)
    from ACRIS Master, then joins to Legals for BBL mapping.
    """
    outfile = DATA_RAW / f"lis_pendens_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    cutoff = f"{date.today().year - 5}-01-01T00:00:00"

    # Step 1: Get recent LTPA + JUDG document IDs from Master
    print("Pulling ACRIS distress filings (LTPA + JUDG)...")
    master_rows = _fetch_all(
        ACRIS_MASTER_DATASET_ID,
        {
            "$select": "document_id,doc_type,document_date,recorded_datetime,document_amt",
            "$where": f"doc_type IN ('LTPA','JUDG') AND recorded_datetime>'{cutoff}' AND recorded_borough IN ('1','3','4')",
            "$order": "recorded_datetime DESC",
        },
        label="distress filings (master)",
    )

    if not master_rows:
        outfile.write_text("[]")
        print("No distress filings found")
        return outfile

    master_by_id = {r["document_id"]: r for r in master_rows}
    doc_ids = list(master_by_id.keys())
    print(f"  Found {len(doc_ids)} distress documents, fetching BBLs...")

    # Step 2: Fetch Legals for these document_ids in batches
    legals = []
    for i in range(0, len(doc_ids), 200):
        batch_ids = doc_ids[i:i + 200]
        id_list = ",".join(f"'{d}'" for d in batch_ids)
        batch_legals = _fetch_all(
            ACRIS_LEGALS_DATASET_ID,
            {
                "$select": "document_id,borough,block,lot",
                "$where": f"document_id IN ({id_list})",
            },
            label=f"legals batch {i // 200 + 1}",
        )
        legals.extend(batch_legals)
        time.sleep(0.5)

    # Join: attach BBL to master records
    results = []
    for leg in legals:
        doc_id = leg.get("document_id", "")
        master = master_by_id.get(doc_id)
        if not master:
            continue
        boro = str(leg.get("borough", ""))
        block = str(leg.get("block", "")).zfill(5)
        lot = str(leg.get("lot", "")).zfill(4)
        results.append({
            "bbl": f"{boro}{block}{lot}",
            "doc_type": master.get("doc_type", ""),
            "document_date": master.get("document_date", ""),
            "recorded_datetime": master.get("recorded_datetime", ""),
            "document_amt": master.get("document_amt", ""),
        })

    outfile.write_text(json.dumps(results, indent=2))
    print(f"Saved {len(results)} distress filing records -> {outfile}")
    return outfile


def pull_all():
    """Pull all distress signal datasets."""
    pull_hpd_violations()
    pull_ecb_violations()
    pull_tax_liens()
    pull_lis_pendens()


if __name__ == "__main__":
    pull_all()
