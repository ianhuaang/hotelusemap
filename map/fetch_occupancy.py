#!/usr/bin/env python3
"""
Fetch occupancy classifications (R-1/R-2/etc.) from DOB Job Application Filings
for split-use transient buildings with 50+ Class B rooms.
"""
import json
import csv
import ssl
import time
import urllib.request
import urllib.parse

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

SODA_BASE = "https://data.cityofnewyork.us/resource/ic3t-wcy2.json"

with open("split_use_bins.json") as f:
    buildings = json.load(f)

results = []

for i, bldg in enumerate(buildings):
    bin_num = bldg["bin"]
    addr = bldg["address"]
    print(f"[{i+1}/{len(buildings)}] {addr} (BIN {bin_num})...", end=" ", flush=True)

    params = urllib.parse.urlencode({
        "bin__": bin_num,
        "$where": "existing_occupancy IS NOT NULL",
        "$order": "pre__filing_date DESC",
        "$limit": "10",
    })
    url = f"{SODA_BASE}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15, context=ssl_ctx)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR: {e}")
        results.append({**bldg, "existing_occ": "ERROR", "proposed_occ": "", "filing_date": "", "job_desc": str(e)})
        time.sleep(0.5)
        continue

    if data:
        occ_codes = set()
        prop_codes = set()
        latest = data[0]
        for rec in data:
            if rec.get("existing_occupancy"):
                occ_codes.add(rec["existing_occupancy"])
            if rec.get("proposed_occupancy"):
                prop_codes.add(rec["proposed_occupancy"])

        results.append({
            **bldg,
            "existing_occ": ", ".join(sorted(occ_codes)),
            "proposed_occ": ", ".join(sorted(prop_codes)),
            "filing_date": latest.get("pre__filing_date", ""),
            "job_desc": latest.get("job_description", "")[:200],
            "building_class": latest.get("building_class", ""),
            "owner_business": latest.get("owner_s_business_name", ""),
            "num_filings": len(data),
        })
        print(f"found {len(data)} filings -> existing: {', '.join(sorted(occ_codes))}")
    else:
        results.append({**bldg, "existing_occ": "NO DATA", "proposed_occ": "", "filing_date": "", "job_desc": ""})
        print("no occupancy data")

    time.sleep(0.3)

out_path = "split_use_occupancy.csv"
fields = ["address", "bin", "bbl", "class_b", "class_a", "existing_occ", "proposed_occ",
          "building_class", "owner_business", "filing_date", "num_filings", "job_desc"]
with open(out_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(results)

print(f"\nSaved {len(results)} rows to {out_path}")

has_r1 = [r for r in results if "R-1" in r.get("existing_occ", "")]
has_r2 = [r for r in results if "R-2" in r.get("existing_occ", "")]
no_data = [r for r in results if r.get("existing_occ") in ("NO DATA", "ERROR")]
print(f"\nSummary:")
print(f"  R-1 (transient): {len(has_r1)}")
print(f"  R-2 (residential): {len(has_r2)}")
print(f"  No data: {len(no_data)}")
print(f"  Other: {len(results) - len(has_r1) - len(has_r2) - len(no_data)}")
