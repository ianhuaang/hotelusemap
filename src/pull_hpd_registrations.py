"""Pull HPD registration managing agents for pipeline buildings.

Two datasets joined:
  - HPD Registrations (tesw-yqqr): building → registrationid
  - HPD Registration Contacts (feu5-w2e2): registrationid → agent name/corp

Output: {bbl: {managing_agent, managing_agent_corp, owner_corp, head_officer}}
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
from config import DATA_RAW, DATA_PROCESSED, SOCRATA_BASE_URL

HPD_REGISTRATIONS_DATASET_ID = "tesw-yqqr"
HPD_CONTACTS_DATASET_ID = "feu5-w2e2"
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


def pull_hpd_registrations():
    outfile = DATA_RAW / f"hpd_registrations_{TODAY}.json"
    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    # Load pipeline BBLs
    proc_dir = DATA_PROCESSED
    pipeline_files = sorted(proc_dir.glob("pipeline_*.json"), reverse=True)
    pipeline = json.loads(pipeline_files[0].read_text())
    target_bbls = set(
        r["bbl"] for r in pipeline
        if r["tier"] in ("legal_transient", "partial")
    )
    print(f"Looking up HPD registrations for {len(target_bbls)} BBLs")

    # Step 1: Pull registrations for target boroughs
    # Build BBL from boroid+block+lot
    print("Step 1: Fetching HPD Registrations...")
    bbl_list = sorted(target_bbls)

    by_boro = defaultdict(list)
    for bbl in bbl_list:
        boro = bbl[0]
        block = str(int(bbl[1:6]))
        lot = str(int(bbl[6:10]))
        by_boro[boro].append((block, lot))

    all_registrations = []
    for boro, block_lots in by_boro.items():
        for i in range(0, len(block_lots), 100):
            chunk = block_lots[i:i + 100]
            conditions = " OR ".join(
                f"(block='{bl}' AND lot='{lt}')" for bl, lt in chunk
            )
            where = f"boroid='{boro}' AND ({conditions})"

            regs = _fetch_all(
                HPD_REGISTRATIONS_DATASET_ID,
                {
                    "$select": "registrationid,boroid,block,lot,lastregistrationdate",
                    "$where": where,
                },
                label=f"registrations boro {boro} chunk {i // 100 + 1}",
            )
            for r in regs:
                r["bbl"] = f"{r['boroid']}{int(r['block']):05d}{int(r['lot']):04d}"
            all_registrations.extend(regs)

        count = sum(1 for r in all_registrations if r['bbl'][0] == boro)
        print(f"  Borough {boro}: {count} registrations")

    print(f"Total registrations: {len(all_registrations)}")

    # Deduplicate: keep most recent registration per BBL
    best_reg_by_bbl = {}
    for reg in all_registrations:
        bbl = reg["bbl"]
        reg_date = reg.get("lastregistrationdate", "") or ""
        existing = best_reg_by_bbl.get(bbl)
        if existing is None or reg_date > (existing.get("lastregistrationdate", "") or ""):
            best_reg_by_bbl[bbl] = reg

    reg_ids = set(r["registrationid"] for r in best_reg_by_bbl.values())
    print(f"Unique BBLs with registrations: {len(best_reg_by_bbl)}")
    print(f"Registration IDs to look up contacts: {len(reg_ids)}")

    # Step 2: Pull contacts for these registration IDs
    print("Step 2: Fetching HPD Registration Contacts...")
    reg_id_list = sorted(reg_ids)
    all_contacts = []

    for i in range(0, len(reg_id_list), 200):
        chunk = reg_id_list[i:i + 200]
        id_list = ",".join(f"'{rid}'" for rid in chunk)
        where = (
            f"registrationid IN ({id_list}) "
            f"AND type IN ('Agent','CorporateOwner','HeadOfficer')"
        )
        contacts = _fetch_all(
            HPD_CONTACTS_DATASET_ID,
            {
                "$select": "registrationid,type,corporationname,firstname,lastname",
                "$where": where,
            },
            label=f"contacts batch {i // 200 + 1}",
        )
        all_contacts.extend(contacts)
        time.sleep(0.3)

    print(f"Total contacts: {len(all_contacts)}")

    # Step 3: Assemble per-BBL
    contacts_by_regid = defaultdict(list)
    for c in all_contacts:
        contacts_by_regid[c["registrationid"]].append(c)

    results = []
    for bbl, reg in best_reg_by_bbl.items():
        rid = reg["registrationid"]
        contacts = contacts_by_regid.get(rid, [])

        entry = {"bbl": bbl}

        for contact in contacts:
            ctype = contact.get("type", "")
            corp = (contact.get("corporationname") or "").strip()
            first = (contact.get("firstname") or "").strip()
            last = (contact.get("lastname") or "").strip()
            person = f"{first} {last}".strip()

            if ctype == "Agent":
                entry["managing_agent"] = person
                entry["managing_agent_corp"] = corp
            elif ctype == "CorporateOwner":
                entry["owner_corp"] = corp
            elif ctype == "HeadOfficer":
                entry["head_officer"] = person

        if any(k in entry for k in ("managing_agent", "managing_agent_corp", "owner_corp")):
            results.append(entry)

    outfile.write_text(json.dumps(results, indent=2))
    print(f"\nSaved {len(results)} BBLs with HPD registration data -> {outfile}")
    with_agent = sum(1 for r in results if r.get("managing_agent_corp"))
    with_owner = sum(1 for r in results if r.get("owner_corp"))
    print(f"  With managing agent: {with_agent}")
    print(f"  With corporate owner: {with_owner}")
    return outfile


if __name__ == "__main__":
    pull_hpd_registrations()
