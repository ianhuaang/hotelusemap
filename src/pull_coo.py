"""Fetch Certificate of Occupancy data from DOB for pipeline buildings.

Targets legal_transient and class_b tier buildings where C of O floor-by-floor
data would clarify exactly which units are transient vs residential.

Two sources:
  - Legacy BIS (bs8b-p36w): COs issued ~2012–March 2021
  - DOB NOW (pkdm-hqz6): COs issued March 2021+

Phase 1: Pull structured C of O issuance records from both Socrata datasets.
Phase 2: For priority buildings, download actual C of O PDFs from BIS for parsing.
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import certifi

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DATA_RAW, DATA_PROCESSED, SOCRATA_BASE_URL,
    COO_LEGACY_DATASET_ID, COO_NOW_DATASET_ID,
)

BATCH_SIZE = 5000

# Legacy BIS columns
LEGACY_COLUMNS = [
    "job_number", "bin_number", "borough", "block", "lot", "bbl",
    "c_o_issue_date", "job_type", "issue_type",
    "pr_dwelling_unit",
]

# DOB NOW columns
NOW_COLUMNS = [
    "job_filing_name", "bin", "borough", "block", "lot", "bbl",
    "c_of_o_issuance_date", "job_type", "c_of_o_filing_type",
    "number_of_dwelling_units",
]

BIS_COO_URL = "http://a810-bisweb.nyc.gov/bisweb/COsByLocationServlet"


def _fetch_paginated(dataset_id: str, columns: list[str], where: str, ctx) -> list[dict]:
    """Fetch all rows from a Socrata dataset with pagination."""
    select = ",".join(columns)
    all_rows = []
    offset = 0

    while True:
        params = urllib.parse.urlencode({
            "$select": select,
            "$where": where,
            "$limit": BATCH_SIZE,
            "$offset": offset,
        })
        url = f"{SOCRATA_BASE_URL}/{dataset_id}.json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                    batch = json.loads(resp.read())
                break
            except (ConnectionResetError, TimeoutError) as e:
                if attempt == 2:
                    raise
                print(f"  retry {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))

        if not batch:
            break

        all_rows.extend(batch)
        print(f"  fetched {len(all_rows)} records from {dataset_id}...")
        offset += BATCH_SIZE
        time.sleep(0.5)

    return all_rows


def _clean_date(raw: str) -> str:
    """Extract YYYY-MM-DD or MM/DD/YY from messy date strings."""
    if not raw:
        return ""
    # Socrata ISO format: 2021-04-08T00:00:00.000
    if "T" in raw:
        return raw[:10]
    # DOB NOW format: 04/08/21 1 (trailing junk)
    return raw.strip().split()[0] if raw.strip() else ""


def _normalize_legacy(row: dict) -> dict:
    """Normalize a legacy BIS C of O row to a common schema."""
    return {
        "bin": str(row.get("bin_number") or row.get("bin") or "").strip(),
        "bbl": str(row.get("bbl") or "").strip(),
        "borough": (row.get("borough") or "").strip(),
        "block": (row.get("block") or "").strip(),
        "lot": (row.get("lot") or "").strip(),
        "issue_date": _clean_date(row.get("c_o_issue_date") or ""),
        "job_type": (row.get("job_type") or "").strip(),
        "co_type": (row.get("issue_type") or "").strip(),
        "dwelling_units": (row.get("pr_dwelling_unit") or "").strip(),
        "source": "legacy",
    }


def _normalize_now(row: dict) -> dict:
    """Normalize a DOB NOW C of O row to a common schema."""
    return {
        "bin": str(row.get("bin") or "").strip(),
        "bbl": str(row.get("bbl") or "").strip(),
        "borough": (row.get("borough") or "").strip(),
        "block": (row.get("block") or "").strip(),
        "lot": (row.get("lot") or "").strip(),
        "issue_date": _clean_date(row.get("c_of_o_issuance_date") or ""),
        "job_type": (row.get("job_type") or "").strip(),
        "co_type": (row.get("c_of_o_filing_type") or "").strip(),
        "dwelling_units": (row.get("number_of_dwelling_units") or "").strip(),
        "source": "now",
    }


def _load_priority_bbls(pipeline_path: Path = None) -> set[str]:
    """Get BBLs for legal_transient and class_b buildings."""
    if pipeline_path is None:
        today = date.today().strftime("%Y%m%d")
        pipeline_path = DATA_PROCESSED / f"pipeline_{today}.json"

    pipeline = json.loads(pipeline_path.read_text())
    return {r["bbl"] for r in pipeline if r["tier"] in ("legal_transient", "class_b")}


def pull_coo_records() -> Path:
    """Pull C of O issuance records for MN/BK/QN from both Socrata datasets."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    today = date.today().strftime("%Y%m%d")
    outfile = DATA_RAW / f"coo_{today}.json"

    if outfile.exists():
        print(f"Already exists: {outfile}")
        return outfile

    DATA_RAW.mkdir(parents=True, exist_ok=True)

    # Legacy BIS: borough uses title case (Manhattan, Brooklyn, Queens)
    legacy_where = "borough IN ('Manhattan', 'Brooklyn', 'Queens')"
    print("  Pulling legacy BIS C of O records...")
    legacy_rows = _fetch_paginated(COO_LEGACY_DATASET_ID, LEGACY_COLUMNS, legacy_where, ctx)
    print(f"  Legacy: {len(legacy_rows)} records")

    # DOB NOW: borough names may differ, try both formats
    now_where = "borough IN ('MANHATTAN', 'BROOKLYN', 'QUEENS', 'Manhattan', 'Brooklyn', 'Queens')"
    print("  Pulling DOB NOW C of O records...")
    now_rows = _fetch_paginated(COO_NOW_DATASET_ID, NOW_COLUMNS, now_where, ctx)
    print(f"  DOB NOW: {len(now_rows)} records")

    # Normalize to common schema
    all_rows = []
    for row in legacy_rows:
        all_rows.append(_normalize_legacy(row))
    for row in now_rows:
        all_rows.append(_normalize_now(row))

    outfile.write_text(json.dumps(all_rows, indent=2))
    print(f"Saved {len(all_rows)} total C of O records -> {outfile}")
    return outfile


def match_coo_to_pipeline(
    coo_path: Path = None,
    pipeline_path: Path = None,
) -> dict[str, list[dict]]:
    """Match C of O records to pipeline buildings by BBL. Returns BBL -> C of O list."""
    today = date.today().strftime("%Y%m%d")
    if coo_path is None:
        coo_path = DATA_RAW / f"coo_{today}.json"
    if pipeline_path is None:
        pipeline_path = DATA_PROCESSED / f"pipeline_{today}.json"

    priority_bbls = _load_priority_bbls(pipeline_path)
    coo_records = json.loads(coo_path.read_text())

    by_bbl: dict[str, list[dict]] = {}
    matched = 0
    for row in coo_records:
        bbl = row.get("bbl", "").strip()
        if bbl in priority_bbls:
            by_bbl.setdefault(bbl, []).append({
                "issue_date": row.get("issue_date", ""),
                "job_type": row.get("job_type", ""),
                "co_type": row.get("co_type", ""),
                "dwelling_units": row.get("dwelling_units", ""),
                "source": row.get("source", ""),
            })
            matched += 1

    print(f"Matched {matched} C of O records to {len(by_bbl)}/{len(priority_bbls)} priority BBLs")
    return by_bbl


# --- Phase 2: PDF download (scaffolded, not yet active) ---

def download_coo_pdf(bin_number: str, output_dir: Path = None) -> list[Path]:
    """Download C of O PDFs for a specific BIN from DOB BIS.

    NOTE: Scaffolded for v2. The BIS website serves HTML pages
    with links to individual C of O PDFs. Parsing those links and downloading
    the PDFs requires HTML parsing (BeautifulSoup or similar).
    """
    if output_dir is None:
        output_dir = DATA_RAW / "coo_pdfs"
    output_dir.mkdir(parents=True, exist_ok=True)

    raise NotImplementedError(
        "PDF download requires HTML parsing (BeautifulSoup). "
        "Install bs4 and implement when ready for v2."
    )


def parse_coo_pdf(pdf_path: Path) -> dict:
    """Extract floor-by-floor occupancy from a C of O PDF.

    NOTE: Scaffolded for v2. C of O PDFs have a structured table format:
    - Floor | Use Group | Units/Rooms | Occupancy Load
    Options: pdfplumber (text extraction) + regex, or Claude API for complex PDFs.
    """
    raise NotImplementedError(
        "PDF parsing requires pdfplumber or Claude API. "
        "Implement when ready for v2."
    )


if __name__ == "__main__":
    print("Phase 1: Pulling C of O issuance records from Socrata...")
    pull_coo_records()
    print("\nPhase 1: Matching to pipeline...")
    match_coo_to_pipeline()
    print("\nPhase 2 (PDF download + parsing) is scaffolded but not yet implemented.")
