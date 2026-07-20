"""Enrich pipeline output with sales history, DOB permits, and owner portfolio data.

Reads pipeline output + sales + permits, writes enriched pipeline JSON.
Includes owner name normalization to consolidate fragmented portfolios.
"""

import json
import re
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED

TODAY = date.today().strftime("%Y%m%d")

# DOB NOW uses full-text job types
INTERESTING_JOB_TYPES = {"Alteration", "New Building", "Demolition"}
LEGACY_JOB_TYPES = {"A1", "A2", "A3", "NB", "DM"}
JOB_TYPE_LABELS = {
    "A1": "Major alteration", "A2": "Minor alteration (structural)",
    "A3": "Minor alteration (non-structural)", "NB": "New building", "DM": "Demolition",
    "Alteration": "Alteration", "New Building": "New building", "Demolition": "Demolition",
}


def _normalize_bbl(raw: str) -> str:
    try:
        return str(int(float(raw)))
    except (ValueError, TypeError):
        return ""


# --- Owner name normalization ---

# Suffixes to strip for matching (kept in display name)
_STRIP_SUFFIXES = re.compile(
    r'\b(LLC|L\.L\.C|INC|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|LP|L\.P|'
    r'ASSOCIATES|ASSOC|PARTNER|PARTNERS|PARTNERSHIP|TRUST|HOLDINGS|GROUP|'
    r'ENTERPRISES|PROPERTIES|PROPERTY|MGMT|MANAGEMENT|REALTY|REAL ESTATE|'
    r'DEVELOPMENT|DEVELOPERS|INVESTMENTS|INVESTORS|CAPITAL|EQUITY|FUND)\b\.?',
    re.IGNORECASE,
)

# Address abbreviations to normalize
_ADDR_ABBREVS = {
    r'\bST\b': 'STREET', r'\bAVE?\b': 'AVENUE', r'\bBLVD\b': 'BOULEVARD',
    r'\bRD\b': 'ROAD', r'\bDR\b': 'DRIVE', r'\bPL\b': 'PLACE',
    r'\bCT\b': 'COURT', r'\bLN\b': 'LANE', r'\bPKWY\b': 'PARKWAY',
    r'\bN\b': 'NORTH', r'\bS\b': 'SOUTH', r'\bE\b': 'EAST', r'\bW\b': 'WEST',
}


def _normalize_owner(name: str) -> str:
    """Normalize owner name for dedup matching. Returns canonical form."""
    if not name:
        return ""
    s = name.upper().strip()
    # Remove punctuation except spaces
    s = re.sub(r'[,.\-\'\"#&/()]+', ' ', s)
    # Strip entity suffixes
    s = _STRIP_SUFFIXES.sub('', s)
    # Normalize address abbreviations
    for pattern, replacement in _ADDR_ABBREVS.items():
        s = re.sub(pattern, replacement, s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    # Remove standalone numbers at start (e.g., "123 MAIN STREET" as owner name)
    # but keep them if that's all there is
    return s


_SKIP_OWNERS = {
    "UNAVAILABLE OWNER", "UNAVAILABLE", "UNKNOWN", "UNKNOWN OWNER",
    "N/A", "NA", "NONE", "NOT AVAILABLE", "NO OWNER", "OWNER UNKNOWN",
}


def _build_owner_groups(records: list[dict]) -> dict[str, str]:
    """Build a mapping from raw owner name -> canonical group name.

    First normalizes names, then does a second pass with fuzzy matching
    to catch near-duplicates (>90% similarity).
    """
    # Step 1: Normalize all names
    raw_to_norm: dict[str, str] = {}
    norm_to_raws: dict[str, list[str]] = defaultdict(list)

    for record in records:
        raw = (record.get("ownername") or "").strip().upper()
        if not raw or raw in _SKIP_OWNERS or _normalize_owner(raw) == "UNAVAILABLE":
            continue
        norm = _normalize_owner(raw)
        if not norm:
            continue
        raw_to_norm[raw] = norm
        if raw not in norm_to_raws[norm]:
            norm_to_raws[norm].append(raw)

    # Step 2: Fuzzy merge normalized names that are very similar
    # Bucket by first 4 chars to avoid O(n^2) on thousands of names
    norm_names = sorted(norm_to_raws.keys(), key=lambda n: -len(norm_to_raws[n]))
    canonical_map: dict[str, str] = {}  # norm -> canonical norm

    # Build buckets by prefix for faster matching
    prefix_buckets: dict[str, list[str]] = defaultdict(list)

    for norm in norm_names:
        if norm in canonical_map:
            continue
        prefix = norm[:4] if len(norm) >= 4 else norm
        # Only compare within the same prefix bucket
        best_match = None
        best_ratio = 0.0
        for canon in prefix_buckets.get(prefix, []):
            if abs(len(norm) - len(canon)) > max(len(norm), len(canon)) * 0.2:
                continue
            ratio = SequenceMatcher(None, norm, canon).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = canon
        if best_ratio >= 0.90 and best_match:
            canonical_map[norm] = best_match
        else:
            canonical_map[norm] = norm
            prefix_buckets[prefix].append(norm)

    # Step 3: Build raw -> canonical display name mapping
    # The display name is the most common raw name in the group
    canon_to_best_raw: dict[str, str] = {}
    canon_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for record in records:
        raw = (record.get("ownername") or "").strip().upper()
        norm = raw_to_norm.get(raw)
        if not norm:
            continue
        canon = canonical_map.get(norm, norm)
        canon_counts[canon][raw] += 1

    for canon, raw_counts in canon_counts.items():
        canon_to_best_raw[canon] = max(raw_counts, key=raw_counts.get)

    # Final mapping: raw owner -> canonical display name
    result: dict[str, str] = {}
    for raw, norm in raw_to_norm.items():
        canon = canonical_map.get(norm, norm)
        result[raw] = canon_to_best_raw.get(canon, raw)

    return result


def load_sales(path: Path = None) -> dict[str, list[dict]]:
    if path is None:
        path = DATA_RAW / f"sales_{TODAY}.json"
    raw = json.loads(path.read_text())

    by_bbl: dict[str, list[dict]] = {}
    for row in raw:
        bbl = _normalize_bbl(row.get("bbl", ""))
        if not bbl:
            continue
        price = int(float(row.get("sale_price", 0) or 0))
        sale_date = (row.get("sale_date") or "")[:10]
        if not sale_date:
            continue
        by_bbl.setdefault(bbl, []).append({
            "date": sale_date,
            "price": price,
        })

    for bbl in by_bbl:
        by_bbl[bbl].sort(key=lambda s: s["date"], reverse=True)

    return by_bbl


def load_permits(path: Path = None) -> dict[str, list[dict]]:
    if path is None:
        path = DATA_RAW / f"permits_{TODAY}.json"
    raw = json.loads(path.read_text())

    by_bbl: dict[str, list[dict]] = {}
    for row in raw:
        bbl = _normalize_bbl(row.get("bbl", ""))
        if not bbl:
            continue
        job_type = (row.get("job_type") or "").strip()
        if job_type not in INTERESTING_JOB_TYPES and job_type.upper() not in LEGACY_JOB_TYPES:
            continue

        action_date = (row.get("current_status_date") or row.get("latest_action_date") or "")[:10]
        filing_date = (row.get("filing_date") or row.get("pre__filing_date") or "")[:10]
        cost = int(float(row.get("initial_cost", 0) or 0))

        by_bbl.setdefault(bbl, []).append({
            "job_type": job_type,
            "job_type_label": JOB_TYPE_LABELS.get(job_type, job_type),
            "description": (row.get("job_description") or "").strip()[:120],
            "status": row.get("filing_status") or row.get("job_status_descrp", ""),
            "filing_date": filing_date,
            "action_date": action_date,
            "cost": cost,
        })

    for bbl in by_bbl:
        by_bbl[bbl].sort(key=lambda p: p.get("action_date", ""), reverse=True)

    return by_bbl


def load_coo(path: Path = None) -> dict[str, list[dict]]:
    if path is None:
        path = DATA_RAW / f"coo_{TODAY}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())

    by_bbl: dict[str, list[dict]] = {}
    for row in raw:
        bbl = (row.get("bbl") or "").strip()
        if not bbl:
            continue
        issue_date = (row.get("issue_date") or "").strip()
        by_bbl.setdefault(bbl, []).append({
            "issue_date": issue_date,
            "job_type": row.get("job_type", ""),
            "co_type": row.get("co_type", ""),
            "dwelling_units": row.get("dwelling_units", ""),
            "source": row.get("source", ""),
        })

    for bbl in by_bbl:
        by_bbl[bbl].sort(key=lambda c: c.get("issue_date", ""), reverse=True)

    return by_bbl


def enrich_pipeline(
    pipeline_path: Path = None,
    sales_path: Path = None,
    permits_path: Path = None,
    coo_path: Path = None,
) -> Path:
    if pipeline_path is None:
        pipeline_path = DATA_PROCESSED / f"pipeline_{TODAY}.json"

    pipeline = json.loads(pipeline_path.read_text())
    sales_by_bbl = load_sales(sales_path)
    permits_by_bbl = load_permits(permits_path)
    coo_by_bbl = load_coo(coo_path)

    # Owner dedup: normalize names and build groups
    owner_canonical = _build_owner_groups(pipeline)

    # Count unique raw vs canonical owners for reporting
    raw_owners = set(owner_canonical.keys())
    canonical_owners = set(owner_canonical.values())
    print(f"Owner dedup: {len(raw_owners)} raw names -> {len(canonical_owners)} canonical groups")

    # Build portfolio index using canonical names
    owner_bbls: dict[str, list[str]] = defaultdict(list)
    for record in pipeline:
        raw = (record.get("ownername") or "").strip().upper()
        if not raw or raw in _SKIP_OWNERS:
            continue
        canon = owner_canonical.get(raw, raw)
        if canon and canon not in _SKIP_OWNERS:
            owner_bbls[canon].append(record["bbl"])

    # Enrich each record
    enriched_count = 0
    for record in pipeline:
        bbl = record["bbl"]

        # Sales history
        sales = sales_by_bbl.get(bbl, [])
        if sales:
            last_sale = sales[0]
            record["last_sale_date"] = last_sale["date"]
            record["last_sale_price"] = last_sale["price"]
            record["sale_count"] = len(sales)
        else:
            record["last_sale_date"] = None
            record["last_sale_price"] = None
            record["sale_count"] = 0

        # DOB permits
        permits = permits_by_bbl.get(bbl, [])
        record["permits"] = permits[:5]
        record["permit_count"] = len(permits)

        # Owner portfolio (using deduped names)
        raw = (record.get("ownername") or "").strip().upper()
        canon = owner_canonical.get(raw, raw)
        record["owner_canonical"] = canon
        portfolio = owner_bbls.get(canon, [])
        record["owner_portfolio_size"] = len(portfolio)
        record["owner_portfolio_bbls"] = portfolio if len(portfolio) > 1 else []

        # C of O history
        coos = coo_by_bbl.get(bbl, [])
        record["coo_records"] = coos[:5]
        record["coo_count"] = len(coos)
        # Summarize: most recent CO type, and whether there are temporary COs (strong transient signal)
        if coos:
            record["coo_latest_date"] = coos[0].get("issue_date", "")
            record["coo_latest_type"] = coos[0].get("co_type", "")
            record["coo_has_temporary"] = any(c.get("co_type") == "Temporary" for c in coos)
            # Dwelling units from most recent CO
            units_str = coos[0].get("dwelling_units", "").strip()
            record["coo_dwelling_units"] = int(units_str) if units_str.isdigit() else None
        else:
            record["coo_latest_date"] = None
            record["coo_latest_type"] = None
            record["coo_has_temporary"] = False
            record["coo_dwelling_units"] = None

        if sales or permits or coos:
            enriched_count += 1

    outpath = DATA_PROCESSED / f"pipeline_{TODAY}.json"
    outpath.write_text(json.dumps(pipeline, indent=2, default=str))
    print(f"Enriched {enriched_count}/{len(pipeline)} buildings with sales/permit/C of O data")
    print(f"  Buildings with sales: {sum(1 for r in pipeline if r.get('last_sale_date'))}")
    print(f"  Buildings with permits: {sum(1 for r in pipeline if r.get('permit_count', 0) > 0)}")
    print(f"  Buildings with C of O: {sum(1 for r in pipeline if r.get('coo_count', 0) > 0)}")
    print(f"    with Temporary COs: {sum(1 for r in pipeline if r.get('coo_has_temporary'))}")
    multi = sum(1 for v in owner_bbls.values() if len(v) > 1)
    print(f"  Multi-building owners: {multi} (owning {sum(len(v) for v in owner_bbls.values() if len(v) > 1)} buildings)")
    return outpath


if __name__ == "__main__":
    enrich_pipeline()
