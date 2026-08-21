"""Pull owner contact names from ACRIS for pipeline buildings.

Strategy:
  1. Query ACRIS Legals for all document_ids linked to our BBLs
  2. Filter to DEED and MTGE doc types from ACRIS Master (most recent)
  3. Join to ACRIS Parties to get grantor/grantee names
  4. The grantee (party_type=2) on the most recent deed = current owner
  5. The grantor (party_type=1) on the most recent mortgage = borrower (often individual behind LLC)
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED, SOCRATA_BASE_URL, ACRIS_LEGALS_DATASET_ID, ACRIS_MASTER_DATASET_ID

ACRIS_PARTIES_DATASET_ID = "636b-3b5g"
BATCH_SIZE = 5000
TODAY = date.today().strftime("%Y%m%d")


def _fetch_all(dataset_id, params_base, label="records"):
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
        if len(all_rows) % 20000 == 0 or len(batch) < BATCH_SIZE:
            print(f"  fetched {len(all_rows)} {label}...")
        offset += BATCH_SIZE
        time.sleep(0.3)
    return all_rows


def pull_acris_owners():
    outfile = DATA_RAW / f"acris_owners_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    # Load pipeline BBLs (legal_transient + class_b only)
    proc_dir = DATA_PROCESSED
    pipeline_files = sorted(proc_dir.glob("pipeline_*.json"), reverse=True)
    pipeline = json.loads(pipeline_files[0].read_text())
    target_bbls = set(
        r["bbl"] for r in pipeline
        if r["tier"] in ("legal_transient", "partial")
    )
    print(f"Looking up ACRIS owners for {len(target_bbls)} BBLs")

    # Step 1: Get document_ids from Legals for our BBLs
    # Query by borough + block + lot (ACRIS doesn't have concatenated BBL)
    # We'll query per borough to keep WHERE clauses manageable
    print("Step 1: Fetching ACRIS Legals...")
    all_legals = []
    bbl_list = sorted(target_bbls)

    # Group by borough
    by_boro = defaultdict(list)
    for bbl in bbl_list:
        boro = bbl[0]
        block = bbl[1:6]
        lot = bbl[6:10]
        by_boro[boro].append((block, lot))

    for boro, block_lots in by_boro.items():
        # Build WHERE clause in chunks (Socrata has URL length limits)
        for i in range(0, len(block_lots), 100):
            chunk = block_lots[i:i + 100]
            # Build OR conditions
            conditions = " OR ".join(
                f"(block='{bl}' AND lot='{lt}')" for bl, lt in chunk
            )
            where = f"borough='{boro}' AND ({conditions})"

            legals = _fetch_all(
                ACRIS_LEGALS_DATASET_ID,
                {
                    "$select": "document_id,borough,block,lot",
                    "$where": where,
                },
                label=f"legals boro {boro} chunk {i // 100 + 1}",
            )
            # Add BBL to each
            for r in legals:
                r["bbl"] = f"{r['borough']}{r['block'].zfill(5)}{r['lot'].zfill(4)}"
            all_legals.extend(legals)

        print(f"  Borough {boro}: {sum(1 for l in all_legals if l['bbl'][0] == boro)} legal records")

    print(f"Total legals: {len(all_legals)}")

    # Group document_ids by BBL
    doc_ids_by_bbl = defaultdict(set)
    for leg in all_legals:
        doc_ids_by_bbl[leg["bbl"]].add(leg["document_id"])

    all_doc_ids = set()
    for ids in doc_ids_by_bbl.values():
        all_doc_ids.update(ids)
    print(f"Unique document_ids: {len(all_doc_ids)}")

    # Step 2: Get Master records for these doc_ids (DEED + MTGE only)
    print("Step 2: Fetching ACRIS Master (DEED + MTGE)...")
    master_by_id = {}
    doc_id_list = sorted(all_doc_ids)

    for i in range(0, len(doc_id_list), 200):
        chunk = doc_id_list[i:i + 200]
        id_list = ",".join(f"'{d}'" for d in chunk)
        masters = _fetch_all(
            ACRIS_MASTER_DATASET_ID,
            {
                "$select": "document_id,doc_type,document_date,recorded_datetime,document_amt",
                "$where": f"document_id IN ({id_list}) AND doc_type IN ('DEED','MTGE')",
            },
            label=f"master batch {i // 200 + 1}",
        )
        for m in masters:
            master_by_id[m["document_id"]] = m
        time.sleep(0.3)

    print(f"DEED/MTGE documents: {len(master_by_id)}")

    # Step 3: Get Parties for these documents
    print("Step 3: Fetching ACRIS Parties...")
    relevant_doc_ids = sorted(master_by_id.keys())
    all_parties = []

    for i in range(0, len(relevant_doc_ids), 200):
        chunk = relevant_doc_ids[i:i + 200]
        id_list = ",".join(f"'{d}'" for d in chunk)
        parties = _fetch_all(
            ACRIS_PARTIES_DATASET_ID,
            {
                "$select": "document_id,party_type,name,address_1,city,state,zip",
                "$where": f"document_id IN ({id_list})",
            },
            label=f"parties batch {i // 200 + 1}",
        )
        all_parties.extend(parties)
        time.sleep(0.3)

    print(f"Party records: {len(all_parties)}")

    # Step 4: Assemble per-BBL owner info
    # For each BBL, find:
    #   - Most recent DEED grantee (party_type=2) = current owner
    #   - Most recent MTGE grantor (party_type=1) = borrower (often individual)
    parties_by_doc = defaultdict(list)
    for p in all_parties:
        parties_by_doc[p["document_id"]].append(p)

    results = {}
    for bbl in target_bbls:
        doc_ids = doc_ids_by_bbl.get(bbl, set())
        deeds = []
        mortgages = []
        for did in doc_ids:
            m = master_by_id.get(did)
            if not m:
                continue
            rec_date = (m.get("recorded_datetime") or "")[:10]
            if m["doc_type"] == "DEED":
                deeds.append((rec_date, did, m))
            elif m["doc_type"] == "MTGE":
                mortgages.append((rec_date, did, m))

        entry = {"bbl": bbl}

        # Most recent deed — grantee = buyer = current owner
        if deeds:
            deeds.sort(reverse=True)
            latest_deed_date, latest_deed_id, latest_deed = deeds[0]
            grantees = [
                p for p in parties_by_doc.get(latest_deed_id, [])
                if str(p.get("party_type")) == "2"
            ]
            entry["deed_date"] = latest_deed_date
            entry["deed_amt"] = latest_deed.get("document_amt", "")
            entry["deed_grantees"] = [
                {
                    "name": (g.get("name") or "").strip(),
                    "address": ", ".join(filter(None, [
                        (g.get("address_1") or "").strip(),
                        (g.get("city") or "").strip(),
                        (g.get("state") or "").strip(),
                        (g.get("zip") or "").strip(),
                    ])),
                }
                for g in grantees
                if (g.get("name") or "").strip()
            ]

        # Most recent mortgage — grantor = borrower
        if mortgages:
            mortgages.sort(reverse=True)
            latest_mtge_date, latest_mtge_id, latest_mtge = mortgages[0]
            grantors = [
                p for p in parties_by_doc.get(latest_mtge_id, [])
                if str(p.get("party_type")) == "1"
            ]
            lenders = [
                p for p in parties_by_doc.get(latest_mtge_id, [])
                if str(p.get("party_type")) == "2"
            ]
            entry["mtge_date"] = latest_mtge_date
            entry["mtge_amt"] = latest_mtge.get("document_amt", "")
            entry["mtge_borrowers"] = [
                {
                    "name": (g.get("name") or "").strip(),
                    "address": ", ".join(filter(None, [
                        (g.get("address_1") or "").strip(),
                        (g.get("city") or "").strip(),
                        (g.get("state") or "").strip(),
                        (g.get("zip") or "").strip(),
                    ])),
                }
                for g in grantors
                if (g.get("name") or "").strip()
            ]
            entry["mtge_lender"] = lenders[0].get("name", "").strip() if lenders else ""

        if "deed_grantees" in entry or "mtge_borrowers" in entry:
            results[bbl] = entry

    outfile.write_text(json.dumps(list(results.values()), indent=2, default=str))
    print(f"\nSaved {len(results)} BBLs with ACRIS owner data -> {outfile}")
    print(f"  With deed info: {sum(1 for r in results.values() if 'deed_grantees' in r)}")
    print(f"  With mortgage info: {sum(1 for r in results.values() if 'mtge_borrowers' in r)}")
    return outfile


if __name__ == "__main__":
    pull_acris_owners()
