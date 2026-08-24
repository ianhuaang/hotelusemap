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


# Zoning compatibility for hotel use (Use Group 5)
# Post-2021 amendment: ALL new hotels require CPC special permit.
# But we care about whether hotel use is fundamentally permitted in the district.
# Districts where hotels (UG5) are a permitted use (with special permit):
#   C1 (except C1-1 thru C1-4), C2 (except C2-1 thru C2-4), C4, C5, C6, C8, M1
# Not permitted: all R districts, M2, M3, C3, C7, PARK, BPC
_HOTEL_NOT_PERMITTED_LOW_C = {"C1-1", "C1-2", "C1-3", "C1-4", "C2-1", "C2-2", "C2-3", "C2-4"}

def _zoning_hotel_compatibility(zonedist: str) -> tuple[str, str]:
    """Return (compatibility, detail) for hotel use in this zoning district.

    compatibility: 'permitted', 'not_permitted', or 'unknown'
    """
    if not zonedist:
        return "unknown", ""

    z = zonedist.strip().upper()
    # Handle paired zones like M1-5/R10 — use the first
    if "/" in z:
        z = z.split("/")[0]

    # Base district: C6-4A -> C6, R7-2 -> R7, M1-5 -> M1
    base = z.split("-")[0] if "-" in z else z

    if base in ("C4", "C5", "C6"):
        return "permitted", "Hotel use permitted (CPC special permit required for new/enlarged hotels since 2021)"
    if base == "C8":
        return "permitted", "Hotel use permitted in C8 (CPC special permit required)"
    if base == "C1":
        if z in _HOTEL_NOT_PERMITTED_LOW_C:
            return "not_permitted", f"Hotel use not permitted in {z} (low-density commercial overlay)"
        return "permitted", "Hotel use permitted in C1-5+ (CPC special permit required)"
    if base == "C2":
        if z in _HOTEL_NOT_PERMITTED_LOW_C:
            return "not_permitted", f"Hotel use not permitted in {z} (low-density commercial overlay)"
        return "permitted", "Hotel use permitted in C2-5+ (CPC special permit required)"
    if base == "M1":
        return "permitted", "Hotel use permitted in M1 (CPC special permit required)"
    if base.startswith("R"):
        return "not_permitted", "Hotel use not permitted in residential zoning districts"
    if base in ("M2", "M3"):
        return "not_permitted", f"Hotel use not permitted in {base} (heavy manufacturing)"
    if base in ("C3", "C7"):
        return "not_permitted", f"Hotel use not permitted in {base}"
    if base in ("PARK", "BPC"):
        return "not_permitted", f"Hotel use not permitted in {base}"

    return "unknown", f"Zoning district {z} — hotel compatibility unclassified"


def _resolve_data_file(prefix: str, path: Path = None) -> Path:
    """Find today's data file or fall back to the most recent one."""
    if path is not None:
        if path.exists():
            return path
    today_path = DATA_RAW / f"{prefix}_{TODAY}.json"
    if today_path.exists():
        return today_path
    files = sorted(DATA_RAW.glob(f"{prefix}_*.json"), reverse=True)
    if files:
        return files[0]
    return today_path  # will fail with FileNotFoundError, which is correct


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
    path = _resolve_data_file("sales", path)
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
    path = _resolve_data_file("permits", path)
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


# --- Transient keyword scanning in permit descriptions ---

_TRANSIENT_PATTERNS = [
    # Strong signals — these directly indicate transient/hotel use
    (re.compile(r'\bhotel\b', re.I), "hotel", "strong"),
    (re.compile(r'\btransient\b', re.I), "transient", "strong"),
    (re.compile(r'\bSRO\b'), "SRO", "strong"),
    (re.compile(r'\bsingle\s+room\s+occupancy\b', re.I), "single_room_occupancy", "strong"),
    (re.compile(r'\bR[- ]?1\b'), "R-1", "strong"),
    (re.compile(r'\bhostel\b', re.I), "hostel", "strong"),
    # Moderate signals — suggestive but need context
    (re.compile(r'\btourist\b', re.I), "tourist", "moderate"),
    (re.compile(r'\bguest\s+room', re.I), "guest_rooms", "moderate"),
    (re.compile(r'\bshort[- ]term\b', re.I), "short_term", "moderate"),
    (re.compile(r'\brooming\b', re.I), "rooming", "moderate"),
    (re.compile(r'\bdormitor', re.I), "dormitory", "moderate"),
    (re.compile(r'\blodging\b', re.I), "lodging", "moderate"),
    (re.compile(r'\bmotel\b', re.I), "motel", "strong"),
    (re.compile(r'\binn\b', re.I), "inn", "moderate"),
]


def scan_permit_descriptions(path: Path = None) -> dict[str, dict]:
    """Scan raw permit job_description fields for transient-related keywords.

    Returns {bbl: {keywords: [...], strong_count: int, moderate_count: int,
                   sample_descriptions: [...]}}
    """
    path = _resolve_data_file("permits", path)
    if not path.exists():
        return {}

    raw = json.loads(path.read_text())
    by_bbl: dict[str, dict] = {}

    for row in raw:
        bbl = _normalize_bbl(row.get("bbl", ""))
        if not bbl:
            continue
        desc = (row.get("job_description") or "").strip()
        if not desc:
            continue

        matched_keywords = set()
        strong = 0
        moderate = 0
        for pattern, keyword, strength in _TRANSIENT_PATTERNS:
            if pattern.search(desc):
                matched_keywords.add(keyword)
                if strength == "strong":
                    strong += 1
                else:
                    moderate += 1

        if not matched_keywords:
            continue

        entry = by_bbl.get(bbl)
        if entry is None:
            entry = {"keywords": set(), "strong_count": 0, "moderate_count": 0,
                     "sample_descriptions": []}
            by_bbl[bbl] = entry

        entry["keywords"].update(matched_keywords)
        entry["strong_count"] += strong
        entry["moderate_count"] += moderate
        if len(entry["sample_descriptions"]) < 3:
            entry["sample_descriptions"].append(desc[:200])

    for bbl, entry in by_bbl.items():
        entry["keywords"] = sorted(entry["keywords"])

    return by_bbl


def load_coo(path: Path = None) -> dict[str, list[dict]]:
    path = _resolve_data_file("coo", path)
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


def load_hpd_violations(path: Path = None) -> dict[str, dict]:
    """Load open HPD violations, summarized per BBL."""
    path = _resolve_data_file("hpd_violations", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())

    by_bbl: dict[str, dict] = {}
    for row in raw:
        bbl = row.get("bbl", "")
        if not bbl:
            continue
        entry = by_bbl.setdefault(bbl, {"total": 0, "class_a": 0, "class_b": 0, "class_c": 0, "rent_impairing": 0})
        entry["total"] += 1
        cls = (row.get("class") or "").upper()
        if cls == "A":
            entry["class_a"] += 1
        elif cls == "B":
            entry["class_b"] += 1
        elif cls == "C":
            entry["class_c"] += 1
        if (row.get("rentimpairing") or "").upper() == "Y":
            entry["rent_impairing"] += 1

    return by_bbl


def load_ecb_violations(path: Path = None) -> dict[str, dict]:
    """Load active DOB ECB violations, summarized per BBL."""
    path = _resolve_data_file("ecb_violations", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())

    by_bbl: dict[str, dict] = {}
    for row in raw:
        bbl = row.get("bbl", "")
        if not bbl:
            continue
        entry = by_bbl.setdefault(bbl, {"count": 0, "total_penalty": 0, "total_balance": 0, "hazardous": 0})
        entry["count"] += 1
        entry["total_penalty"] += int(float(row.get("penality_imposed") or 0))
        entry["total_balance"] += int(float(row.get("balance_due") or 0))
        sev = (row.get("severity") or "").upper()
        if "HAZARDOUS" in sev or "CLASS - 1" in sev:
            entry["hazardous"] += 1

    return by_bbl


def load_tax_liens(path: Path = None) -> dict[str, dict]:
    """Load tax lien records, summarized per BBL."""
    path = _resolve_data_file("tax_liens", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())

    by_bbl: dict[str, dict] = {}
    for row in raw:
        bbl = row.get("bbl", "")
        if not bbl:
            continue
        entry = by_bbl.setdefault(bbl, {"count": 0, "has_lien_sale": False, "water_only": True})
        entry["count"] += 1
        if (row.get("cycle") or "").lower() == "lien sale":
            entry["has_lien_sale"] = True
        if (row.get("water_debt_only") or "").upper() != "YES":
            entry["water_only"] = False

    return by_bbl


def load_lis_pendens(path: Path = None) -> dict[str, dict]:
    """Load lis pendens filings, summarized per BBL."""
    path = _resolve_data_file("lis_pendens", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())

    by_bbl: dict[str, dict] = {}
    for row in raw:
        bbl = row.get("bbl", "")
        if not bbl:
            continue
        rec_date = (row.get("recorded_datetime") or "")[:10]
        entry = by_bbl.setdefault(bbl, {"count": 0, "latest_date": ""})
        entry["count"] += 1
        if rec_date > entry["latest_date"]:
            entry["latest_date"] = rec_date

    return by_bbl


def load_hotel_info(path: Path = None) -> dict[str, dict]:
    """Load hotel names/phone/website from OSM data."""
    if path is None:
        path = DATA_RAW / "hotel_info_osm.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {r["bbl"]: r for r in raw if r.get("hotel_name")}


def load_acris_owners(path: Path = None) -> dict[str, dict]:
    """Load ACRIS owner/borrower names per BBL."""
    path = _resolve_data_file("acris_owners", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {r["bbl"]: r for r in raw}


def load_hotel_licenses(path: Path = None) -> dict[str, dict]:
    """Load DCWP hotel license data keyed by BBL."""
    path = _resolve_data_file("hotel_licenses", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    by_bbl: dict[str, dict] = {}
    for r in raw:
        bbl = (r.get("bbl") or "").strip()
        if not bbl:
            continue
        status = r.get("license_status", "")
        existing = by_bbl.get(bbl)
        if existing is None or (status == "Active" and existing["license_status"] != "Active"):
            by_bbl[bbl] = {
                "business_name": r.get("business_name", ""),
                "license_status": status,
                "license_creation_date": (r.get("license_creation_date") or "")[:10],
                "license_expiration": (r.get("lic_expir_dd") or "")[:10],
                "contact_phone": r.get("contact_phone", ""),
            }
    return by_bbl


def load_hpd_registrations(path: Path = None) -> dict[str, dict]:
    """Load HPD registration managing agents per BBL."""
    path = _resolve_data_file("hpd_registrations", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {r["bbl"]: r for r in raw}


def load_dob_occupancy(path: Path = None) -> dict[str, dict]:
    """Load DOB transient occupancy signals (R-1, J-1) per BBL."""
    path = _resolve_data_file("dob_occupancy", path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {r["bbl"]: r for r in raw}


def load_landmarks() -> dict[str, dict]:
    """Load LPC landmark/historic district data per BBL."""
    files = sorted(DATA_RAW.glob("landmarks_*.json"), reverse=True)
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {r["bbl"]: r for r in raw}


def load_tax_benefits() -> dict[str, dict]:
    """Load 421-a/J-51 tax benefit data per BBL."""
    files = sorted(DATA_RAW.glob("tax_benefits_*.json"), reverse=True)
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {r["bbl"]: r for r in raw}


def load_rent_stabilization() -> dict[str, dict]:
    """Load rent stabilization unit counts per BBL (from DOF tax bills)."""
    files = sorted(DATA_RAW.glob("rent_stabilization_*.json"), reverse=True)
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {r["bbl"]: r for r in raw}


def enrich_pipeline(
    pipeline_path: Path = None,
    sales_path: Path = None,
    permits_path: Path = None,
    coo_path: Path = None,
    hpd_violations_path: Path = None,
    ecb_violations_path: Path = None,
    tax_liens_path: Path = None,
    lis_pendens_path: Path = None,
    acris_owners_path: Path = None,
    dob_occupancy_path: Path = None,
) -> Path:
    if pipeline_path is None:
        pipeline_path = DATA_PROCESSED / f"pipeline_{TODAY}.json"

    pipeline = json.loads(pipeline_path.read_text())
    sales_by_bbl = load_sales(sales_path)
    permits_by_bbl = load_permits(permits_path)
    coo_by_bbl = load_coo(coo_path)
    hpd_viol_by_bbl = load_hpd_violations(hpd_violations_path)
    ecb_viol_by_bbl = load_ecb_violations(ecb_violations_path)
    liens_by_bbl = load_tax_liens(tax_liens_path)
    lp_by_bbl = load_lis_pendens(lis_pendens_path)
    acris_owners = load_acris_owners(acris_owners_path)
    hotel_info = load_hotel_info()
    dob_occ_by_bbl = load_dob_occupancy(dob_occupancy_path)
    permit_keywords_by_bbl = scan_permit_descriptions(permits_path)
    hotel_licenses = load_hotel_licenses()
    hpd_regs = load_hpd_registrations()
    landmarks = load_landmarks()
    tax_benefits = load_tax_benefits()
    rent_stab = load_rent_stabilization()

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

        # Flag: temporary C of O only (no final) — may be operating on expired authorization
        if record["coo_has_temporary"] and record.get("coo_latest_type") == "Temporary":
            has_final = any(c.get("co_type") != "Temporary" for c in coos)
            if not has_final:
                record["coo_temp_only"] = True
                if "coo_temp_only" not in [b.split(" —")[0] for b in record.get("blockers", [])]:
                    record.setdefault("blockers", []).append(
                        "Temporary C of O only — no final C of O on file, may be operating on expired authorization"
                    )

        # Distress signals
        hpd_v = hpd_viol_by_bbl.get(bbl)
        if hpd_v:
            record["hpd_open_violations"] = hpd_v["total"]
            record["hpd_class_c_violations"] = hpd_v["class_c"]
            record["hpd_rent_impairing"] = hpd_v["rent_impairing"]
        else:
            record["hpd_open_violations"] = 0
            record["hpd_class_c_violations"] = 0
            record["hpd_rent_impairing"] = 0

        ecb_v = ecb_viol_by_bbl.get(bbl)
        if ecb_v:
            record["ecb_open_violations"] = ecb_v["count"]
            record["ecb_total_balance"] = ecb_v["total_balance"]
            record["ecb_hazardous"] = ecb_v["hazardous"]
        else:
            record["ecb_open_violations"] = 0
            record["ecb_total_balance"] = 0
            record["ecb_hazardous"] = 0

        lien = liens_by_bbl.get(bbl)
        record["has_tax_lien"] = lien is not None
        record["tax_lien_count"] = lien["count"] if lien else 0
        record["tax_lien_non_water"] = (not lien["water_only"]) if lien else False

        lp = lp_by_bbl.get(bbl)
        record["has_lis_pendens"] = lp is not None
        record["lis_pendens_count"] = lp["count"] if lp else 0
        record["lis_pendens_latest"] = lp["latest_date"] if lp else None

        # ACRIS owner identification
        acris = acris_owners.get(bbl)
        if acris:
            grantees = acris.get("deed_grantees", [])
            borrowers = acris.get("mtge_borrowers", [])
            record["acris_deed_owner"] = grantees[0]["name"] if grantees else ""
            record["acris_deed_date"] = acris.get("deed_date", "")
            record["acris_deed_address"] = grantees[0].get("address", "") if grantees else ""
            record["acris_borrower"] = borrowers[0]["name"] if borrowers else ""
            record["acris_mtge_date"] = acris.get("mtge_date", "")
            record["acris_mtge_amt"] = acris.get("mtge_amt", "")
            record["acris_lender"] = acris.get("mtge_lender", "")
        else:
            record["acris_deed_owner"] = ""
            record["acris_deed_date"] = ""
            record["acris_deed_address"] = ""
            record["acris_borrower"] = ""
            record["acris_mtge_date"] = ""
            record["acris_mtge_amt"] = ""
            record["acris_lender"] = ""

        # Hotel info (OSM)
        hi = hotel_info.get(bbl)
        if hi:
            record["hotel_name"] = hi.get("hotel_name", "")
            record["hotel_phone"] = hi.get("phone", "")
            record["hotel_website"] = hi.get("website", "")
        else:
            record["hotel_name"] = ""
            record["hotel_phone"] = ""
            record["hotel_website"] = ""

        # DOB occupancy classification (R-1/J-1 transient signal)
        dob_occ = dob_occ_by_bbl.get(bbl)
        if dob_occ:
            record["dob_has_r1"] = dob_occ.get("has_r1", False)
            record["dob_has_j1"] = dob_occ.get("has_j1", False)
            record["dob_r1_filing_count"] = dob_occ.get("r1_filing_count", 0)
            record["dob_transient_units"] = dob_occ.get("max_dwelling_units", 0)
            # Tier upgrade: R-1 in DOB with no HPD Class B → partial medium
            # (evidence of transient use, but needs current C of O verification)
            tier = record.get("tier", "")
            class_b = record.get("hpd_class_b", 0) or 0
            if dob_occ["has_r1"] and class_b == 0 and tier == "unknown":
                record["tier"] = "partial"
                record["confidence"] = "medium"
                if "dob_r1_occupancy" not in record.get("reason_codes", []):
                    record.setdefault("reason_codes", []).append("dob_r1_occupancy")
            elif dob_occ["has_r1"] and class_b == 0 and tier == "partial" and record.get("confidence") == "low":
                record["confidence"] = "medium"
                if "dob_r1_occupancy" not in record.get("reason_codes", []):
                    record.setdefault("reason_codes", []).append("dob_r1_occupancy")
        else:
            record["dob_has_r1"] = False
            record["dob_has_j1"] = False
            record["dob_r1_filing_count"] = 0
            record["dob_transient_units"] = 0

        # Permit description keyword scanning for transient signals
        pk = permit_keywords_by_bbl.get(bbl)
        if pk:
            record["permit_transient_keywords"] = pk["keywords"]
            record["permit_transient_strong"] = pk["strong_count"]
            record["permit_transient_moderate"] = pk["moderate_count"]
            record["permit_transient_descriptions"] = pk["sample_descriptions"]
            tier = record.get("tier", "")
            if pk["strong_count"] >= 1 and tier in ("unknown", "partial"):
                if tier == "unknown":
                    record["tier"] = "partial"
                    record["confidence"] = "medium"
                elif tier == "partial" and record.get("confidence") == "low":
                    record["confidence"] = "medium"
                record.setdefault("reason_codes", []).append("permit_desc_transient")
        else:
            record["permit_transient_keywords"] = []
            record["permit_transient_strong"] = 0
            record["permit_transient_moderate"] = 0
            record["permit_transient_descriptions"] = []

        # DCWP hotel license
        hl = hotel_licenses.get(bbl)
        if hl:
            record["hotel_license_name"] = hl["business_name"]
            record["hotel_license_status"] = hl["license_status"]
            record["hotel_license_expiration"] = hl["license_expiration"]
            record["has_hotel_license"] = True
            tier = record.get("tier", "")
            status = hl["license_status"]
            if status in ("Active", "Ready for Renewal"):
                if tier in ("unknown", "partial"):
                    record["tier"] = "legal_transient"
                    record["confidence"] = "high"
                    record.setdefault("reason_codes", []).append("dcwp_hotel_license")
                # Active license means it's operating as a hotel now — not a reversion candidate
                if record.get("reversion_window"):
                    record["reversion_window"] = None
                    record["has_reversion"] = False
                    record["segment"] = "pure_hotel"
                    rc = record.get("reason_codes", [])
                    if "reversion_window" in rc:
                        rc.remove("reversion_window")
            elif status in ("Surrendered", "Failed to Renew"):
                if tier in ("unknown",):
                    record["tier"] = "partial"
                    record["confidence"] = "medium"
                record.setdefault("reason_codes", []).append("dcwp_license_lapsed")
        else:
            record["hotel_license_name"] = ""
            record["hotel_license_status"] = ""
            record["hotel_license_expiration"] = ""
            record["has_hotel_license"] = False

        # HPD registration (managing agent)
        hpd_reg = hpd_regs.get(bbl)
        if hpd_reg:
            record["hpd_managing_agent"] = hpd_reg.get("managing_agent", "")
            record["hpd_managing_agent_corp"] = hpd_reg.get("managing_agent_corp", "")
            record["hpd_owner_corp"] = hpd_reg.get("owner_corp", "")
            record["hpd_head_officer"] = hpd_reg.get("head_officer", "")
        else:
            record["hpd_managing_agent"] = ""
            record["hpd_managing_agent_corp"] = ""
            record["hpd_owner_corp"] = ""
            record["hpd_head_officer"] = ""

        # LPC landmarks / historic districts
        lm = landmarks.get(bbl)
        if lm:
            record["is_landmark"] = lm.get("is_individual_landmark", False)
            record["landmark_name"] = lm.get("landmark_name", "")
            record["is_historic_district"] = lm.get("is_historic_district", False)
            record["historic_district"] = lm.get("historic_district", "")
        else:
            record["is_landmark"] = False
            record["landmark_name"] = ""
            record["is_historic_district"] = False
            record["historic_district"] = ""

        # Tax benefits (421-a / J-51) — rent stabilization proxy
        tb = tax_benefits.get(bbl)
        if tb:
            record["has_tax_benefit"] = True
            record["tax_benefit_type"] = tb.get("benefit_type", "")
            record["tax_benefit_expires"] = tb.get("benefit_expires")
            record["tax_benefit_active"] = tb.get("is_active", False)
            if tb.get("is_active"):
                record.setdefault("blockers", []).append(
                    f"Active {tb['benefit_type']} tax benefit — rent stabilization obligations restrict use changes"
                )
        else:
            record["has_tax_benefit"] = False
            record["tax_benefit_type"] = ""
            record["tax_benefit_expires"] = None
            record["tax_benefit_active"] = False

        # Rent stabilization (from DOF tax bills)
        rs = rent_stab.get(bbl)
        if rs:
            record["rent_stabilized_units"] = rs["stabilized_units"]
            record["rent_stab_data_year"] = rs["data_year"]
            if rs["stabilized_units"] > 0:
                record.setdefault("blockers", []).append(
                    f"{rs['stabilized_units']} rent-stabilized units (as of {rs['data_year']} tax bill) — conversion to transient use restricted"
                )
        else:
            record["rent_stabilized_units"] = 0
            record["rent_stab_data_year"] = None

        # Zoning compatibility for hotel use
        zoning_compat, zoning_detail = _zoning_hotel_compatibility(record.get("zonedist1", ""))
        record["zoning_hotel_permitted"] = zoning_compat
        record["zoning_hotel_detail"] = zoning_detail

        # Consolidated operator name (best available source)
        operator = ""
        operator_source = ""
        if record.get("hotel_license_name"):
            operator = record["hotel_license_name"]
            operator_source = "dcwp_license"
        elif record.get("hpd_managing_agent_corp"):
            operator = record["hpd_managing_agent_corp"]
            operator_source = "hpd_managing_agent"
        elif record.get("hotel_name"):
            operator = record["hotel_name"]
            operator_source = "google_places"
        elif record.get("prior_operator"):
            operator = record["prior_operator"]["name"]
            operator_source = "ground_truth"
        record["operator_name"] = operator
        record["operator_source"] = operator_source

        # Mortgage maturity estimate
        mtge_date = record.get("acris_mtge_date", "")
        if mtge_date and len(mtge_date) >= 4:
            try:
                mtge_year = int(mtge_date[:4])
                current_year = date.today().year
                mortgage_age = current_year - mtge_year
                record["mortgage_age_years"] = mortgage_age
                record["mortgage_amount"] = record.get("acris_mtge_amt", "")
                record["mortgage_approaching_maturity"] = mortgage_age >= 4
            except (ValueError, TypeError):
                record["mortgage_age_years"] = None
                record["mortgage_amount"] = ""
                record["mortgage_approaching_maturity"] = False
        else:
            record["mortgage_age_years"] = None
            record["mortgage_amount"] = ""
            record["mortgage_approaching_maturity"] = False

        # --- Composite signal scoring ---
        # Stack weak signals that individually don't trigger upgrades
        # but together indicate transient capacity
        composite_signals = []
        tier = record.get("tier", "")

        if record.get("dob_has_j1") and not record.get("dob_has_r1"):
            composite_signals.append("dob_j1_occupancy")
        if record.get("coo_has_temporary"):
            composite_signals.append("temporary_coo")
        if record.get("permit_transient_moderate", 0) > 0 and record.get("permit_transient_strong", 0) == 0:
            composite_signals.append("permit_desc_moderate")
        if record.get("hotel_license_status") in ("Surrendered", "Failed to Renew"):
            composite_signals.append("dcwp_license_lapsed_signal")
        if record.get("prior_operator"):
            composite_signals.append("prior_operator_signal")

        if len(composite_signals) >= 3 and tier == "partial":
            record["tier"] = "legal_transient"
            record["confidence"] = "medium"
            record.setdefault("reason_codes", []).extend(composite_signals)
            record.setdefault("reason_codes", []).append("composite_signal")
        elif len(composite_signals) >= 2 and tier == "unknown":
            record["tier"] = "partial"
            record["confidence"] = "medium"
            record.setdefault("reason_codes", []).extend(composite_signals)
            record.setdefault("reason_codes", []).append("composite_signal")

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
    print(f"  HPD open violations: {sum(1 for r in pipeline if r.get('hpd_open_violations', 0) > 0)} buildings")
    print(f"    with Class C: {sum(1 for r in pipeline if r.get('hpd_class_c_violations', 0) > 0)} buildings")
    print(f"  ECB open violations: {sum(1 for r in pipeline if r.get('ecb_open_violations', 0) > 0)} buildings")
    print(f"  Tax liens: {sum(1 for r in pipeline if r.get('has_tax_lien'))} buildings")
    print(f"  Lis pendens: {sum(1 for r in pipeline if r.get('has_lis_pendens'))} buildings")
    print(f"  ACRIS owner data: {sum(1 for r in pipeline if r.get('acris_deed_owner'))} buildings")
    kw_count = sum(1 for r in pipeline if r.get("permit_transient_strong", 0) > 0)
    kw_upgraded = sum(1 for r in pipeline if "permit_desc_transient" in r.get("reason_codes", []))
    print(f"  Permit keyword matches: {kw_count} buildings with strong transient keywords")
    print(f"    Tier upgrades from keywords: {kw_upgraded} buildings")
    hl_count = sum(1 for r in pipeline if r.get("has_hotel_license"))
    hl_upgraded = sum(1 for r in pipeline if "dcwp_hotel_license" in r.get("reason_codes", []))
    print(f"  DCWP hotel licenses: {hl_count} buildings matched")
    print(f"    Tier upgrades from licenses: {hl_upgraded} buildings")
    comp_upgraded = sum(1 for r in pipeline if "composite_signal" in r.get("reason_codes", []))
    print(f"  Composite signal upgrades: {comp_upgraded} buildings")
    op_count = sum(1 for r in pipeline if r.get("operator_name"))
    print(f"  Operator identified: {op_count} buildings")
    from collections import Counter as C2
    op_sources = C2(r.get("operator_source") for r in pipeline if r.get("operator_name"))
    print(f"    Sources: {dict(op_sources)}")
    ma_count = sum(1 for r in pipeline if r.get("hpd_managing_agent_corp"))
    print(f"  HPD managing agents: {ma_count} buildings")
    mtge_approaching = sum(1 for r in pipeline if r.get("mortgage_approaching_maturity"))
    print(f"  Mortgage approaching maturity (4+ yrs): {mtge_approaching} buildings")
    lm_count = sum(1 for r in pipeline if r.get("is_landmark") or r.get("is_historic_district"))
    lm_individual = sum(1 for r in pipeline if r.get("is_landmark"))
    print(f"  LPC designated: {lm_count} buildings ({lm_individual} individual landmarks)")
    tb_count = sum(1 for r in pipeline if r.get("has_tax_benefit"))
    tb_active = sum(1 for r in pipeline if r.get("tax_benefit_active"))
    print(f"  Tax benefits (421-a/J-51): {tb_count} buildings ({tb_active} active)")
    rs_count = sum(1 for r in pipeline if r.get("rent_stabilized_units", 0) > 0)
    rs_units = sum(r.get("rent_stabilized_units", 0) for r in pipeline)
    print(f"  Rent stabilized: {rs_count} buildings ({rs_units:,} units)")
    zp = sum(1 for r in pipeline if r.get("zoning_hotel_permitted") == "permitted")
    znp = sum(1 for r in pipeline if r.get("zoning_hotel_permitted") == "not_permitted")
    zu = sum(1 for r in pipeline if r.get("zoning_hotel_permitted") == "unknown")
    print(f"  Zoning hotel use: {zp} permitted, {znp} not permitted, {zu} unknown")
    return outpath


if __name__ == "__main__":
    enrich_pipeline()
