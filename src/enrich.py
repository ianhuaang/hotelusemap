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
from src import provenance

TODAY = date.today().strftime("%Y%m%d")

# DOB NOW uses full-text job types
INTERESTING_JOB_TYPES = {"Alteration", "New Building", "Demolition"}
LEGACY_JOB_TYPES = {"A1", "A2", "A3", "NB", "DM"}
JOB_TYPE_LABELS = {
    "A1": "Major alteration", "A2": "Minor alteration (structural)",
    "A3": "Minor alteration (non-structural)", "NB": "New building", "DM": "Demolition",
    "Alteration": "Alteration", "New Building": "New building", "Demolition": "Demolition",
}


# Safe Hotels Act (Local Law 111 of 2024) licensing took effect 2025-05-03.
# Every row in the DCWP feed post-dates it, but the cutoff is kept explicit so
# a future backfill of pre-Act DCA business licenses cannot quietly mix in.
SAFE_HOTELS_EFFECTIVE = "2025-05-03"

# DCWP issues these on two terms and the split is stark: 381 at one year
# against 343 at three, with almost nothing in between. The review suggested
# the long term as a proxy for a collective bargaining agreement, which was
# worth testing once the HTC roster gave us ground truth. It was tested, on
# the 493 licensed buildings whose union status the roster settles:
#
#     3-year term   89 union of 210   42.4%
#     1-year term   92 union of 283   32.5%
#     base rate                       36.7%     chi2 5.06, p 0.024
#
# Real but far too weak to act on. As a classifier it is wrong 58% of the
# time it fires and misses half of all union buildings, for a five point lift
# over simply assuming the base rate. The term is kept because it is a fact
# about the licence; the union inference is not, and sitting a 42% guess next
# to HTC's own roster would invite someone to read it as a finding.
LICENSE_TERM_LONG_YEARS = 3

# Current uses that disqualify a building as a transient target regardless of
# what its certificate of occupancy or HPD registration says.
#
# Imported rather than restated. This list existed in both files and drifted
# the moment `education` was added to one of them: the sweep flagged 124
# buildings and the map showed 82, the difference being exactly the 42
# education records. Nothing failed — the map was simply quieter than the
# data, which is the kind of gap that survives a review.
from src.enrich_current_use import NON_TRANSIENT_USES as NON_TRANSIENT_CURRENT_USES
from src.enrich_current_use import restate as restate_current_use

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
    """Find today's data file or fall back to the most recent one.

    Delegates to provenance.resolve so the manifest the build publishes and the
    file this actually opens are decided by one rule. Kept as a wrapper rather
    than replaced at twelve call sites: the name says what it does here.

    One behaviour change came with the move. This globbed `{prefix}_*.json`,
    which sorts "cache" above every digit — so a `<prefix>_cache.json` sitting
    beside the dated pulls would outrank all of them under reverse sort. No
    source reached through here has one today; google_current_use does, and
    its own loader already guards with [0-9]. Now they all do.
    """
    return provenance.resolve(prefix, DATA_RAW, path)


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
        # Sort on the normalised date, never the raw string — see _coo_date.
        by_bbl[bbl].sort(key=lambda c: _coo_date(c.get("issue_date", "")) or "", reverse=True)

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


# Admin Code and Building Code 28-210.3, "permanent dwelling offered, used or
# converted to other than permanent residential purpose" — the statute the
# Office of Special Enforcement cites against illegal hotels. 28-202.1 is the
# daily penalty that rides on a class 1 violation of it.
#
# Deliberately narrower than the neighbouring occupancy codes. 28-118.3.2 and
# 27-217 ("occupancy contrary to C of O") cover 45,000 and 23,000 violations
# and catch everything from a converted cellar to a mezzanine office; 28-210.3
# is specifically transient use of residential space, which is the finding the
# review asked for.
ILLEGAL_TRANSIENT_SECTIONS = ("28-210.3",)


def _is_illegal_transient(row: dict) -> bool:
    desc = (row.get("section_law_description1") or "").upper()
    return any(sec in desc for sec in ILLEGAL_TRANSIENT_SECTIONS)


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
        entry = by_bbl.setdefault(bbl, {"count": 0, "total_penalty": 0, "total_balance": 0,
                                        "hazardous": 0, "illegal_transient": 0})
        entry["count"] += 1
        entry["total_penalty"] += int(float(row.get("penality_imposed") or 0))
        entry["total_balance"] += int(float(row.get("balance_due") or 0))
        sev = (row.get("severity") or "").upper()
        if "HAZARDOUS" in sev or "CLASS - 1" in sev:
            entry["hazardous"] += 1
        if _is_illegal_transient(row):
            entry["illegal_transient"] += 1

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


def load_google_hotel_names() -> dict[str, str]:
    """Hotel names per BBL, the verified source taking precedence.

    Two sources. The current-use sweep asks what is at the building's
    coordinates and keeps only occupants whose house number matches, so a
    hotel name from it belongs to the building and is current: of 304
    buildings both sources name, they agree on 286 and the 18 disagreements
    are all rebrands the older file missed — Cachet Boutique is now Hudson
    Yards Hotel, the Hampton Inn on West 48th is now Motto by Hilton.

    The older text-search files are weaker: their third pass asked "hotel near
    <address>" and took the answer, which is how 60 Pine Street came to hold
    "Mint House 70 Pine by Kasa", a real hotel on the same block.

    So a verified name wins. But a verified *absence* does not: the sweep
    returns nothing for 21% of buildings, and rejecting on house number alone
    misses hotels that register on the cross street — the Wythe Hotel occupies
    75 North 11th and files as 80 Wythe Avenue. Treating silence as a
    contradiction would have deleted 262 names here, most of them right.
    Older names therefore stay wherever the sweep did not positively name a
    different hotel.
    """
    result: dict[str, str] = {}

    files = sorted(DATA_RAW.glob("google_current_use_[0-9]*.json"), reverse=True)
    if files:
        for r in json.loads(files[0].read_text()):
            if r.get("current_use") == "hotel" and r.get("google_name"):
                result[r["bbl"]] = r["google_name"]

    for pattern in ("google_hotel_names_[0-9]*.json", "google_hclass_hotels_clean_[0-9]*.json"):
        older = sorted(DATA_RAW.glob(pattern), reverse=True)
        if not older:
            continue
        for r in json.loads(older[0].read_text()):
            if r.get("google_name") and r.get("bbl") not in result:
                result[r["bbl"]] = r["google_name"]

    return result


# The zoning text amendment of 2021-12-09 made a new hotel a special-permit
# use across most commercial and manufacturing districts. A hotel lawfully
# established before that date is grandfathered, and that grandfathering is
# what the reversion overlay is worth. Without evidence the transient use
# predates the cutoff, a reversion candidate is only a candidate.
# Safe Hotels Act room thresholds.
#
# The Act counts "guest rooms", defined as rooms made available or used for
# transient occupancy, and it explicitly excludes residential units and single
# room occupancy units. That makes HPD's Class B count the right measure and
# total units the wrong one: a building with 60 apartments over 120 transient
# rooms is a 120-room hotel under the Act, not a 180-room one.
#
#   100 or more  -> core employees (housekeeping, front desk, front service)
#                   must be employed directly, not subcontracted. Below 100
#                   the subcontracting restriction does not apply.
#   more than 400 -> a "large hotel", carrying continuous security coverage
#                   on top of the direct-employment duty.
#
# Both are thresholds on the operating model rather than the cost line, which
# is why they are surfaced as flags. Note the asymmetry in the statute: the
# first is "or more", the second is "more than".
# Facade Inspection Safety Program, the successor to Local Law 11 of 1998.
# It binds buildings greater than six storeys, which is a height test rather
# than a use test, so it catches residential conversion targets as readily as
# hotels. It matters to underwriting because it is cyclical rather than
# one-off: an inspection by a qualified exterior wall inspector every five
# years, filed with DOB, and an unsafe finding obliges repair on a deadline.
#
# The flag says the programme applies. It cannot say what the last cycle
# found — that lives in DOB's facade filing records, which we do not pull.
FISP_MIN_STORIES = 6

SAFE_HOTELS_DIRECT_EMPLOYMENT_ROOMS = 100
SAFE_HOTELS_LARGE_HOTEL_ROOMS = 400

HOTEL_SPECIAL_PERMIT_CUTOFF = "2021-12-09"


def _coo_date(raw: str) -> str | None:
    """Normalise a C of O issue date to ISO.

    The two source datasets disagree on format: the BIS legacy feed writes
    2012-10-09, DOB NOW writes 11/12/25. Comparing or sorting them as plain
    strings puts every legacy date above every NOW date, because "2" sorts
    after "1" — so a 2012 certificate outranks a 2026 one.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    parts = raw.split("/")
    if len(parts) == 3:
        mm, dd, yy = (p_.strip() for p_ in parts)
        if mm.isdigit() and dd.isdigit() and yy.isdigit():
            # DOB NOW begins in 2021, so a two-digit year is this century.
            year = int(yy)
            if len(yy) == 2:
                year += 2000 if year <= 69 else 1900
            return f"{year:04d}-{int(mm):02d}-{int(dd):02d}"
    return None


# The Act excludes single room occupancy units from "guest room", and these
# building classes are where the exclusion bites. HPD Class B is a Multiple
# Dwelling Law category covering hotels, rooming houses, lodging houses and
# SRO alike, so it is broader than the Act's count and overstates it here.
#
# It is not a rounding difference. 35 buildings crossed the 100-room threshold
# on SRO or dormitory stock, among them International House at 492 rooms and
# NYU University Hall at 478 — neither of which is a hotel, so neither carries
# a direct-employment duty at all.
SAFE_HOTELS_EXCLUDED_CLASSES = {"HR", "RS", "H8", "HH"}


def _guest_rooms(record: dict) -> tuple[int, str]:
    """Guest rooms as the Safe Hotels Act counts them, and where it came from.

    Class B first because it is the closest thing to a transient room count,
    but not where the stock is SRO or dormitory — see above. The C of O
    fallback counts dwelling units, so it is used only where there are no
    Class A units to confuse it, and the floor estimate is a last resort that
    should never be read as a compliance number.
    """
    bldgclass_full = (record.get("bldgclass") or "").upper()
    if bldgclass_full[:2] in SAFE_HOTELS_EXCLUDED_CLASSES:
        return 0, "sro_or_dormitory"

    class_b = int(record.get("hpd_class_b") or 0)
    if class_b:
        return class_b, "hpd_class_b"

    bldgclass = (record.get("bldgclass") or "").upper()
    if bldgclass.startswith("H") and not int(record.get("hpd_class_a") or 0):
        coo_units = record.get("coo_dwelling_units")
        if coo_units:
            return int(coo_units), "coo_dwelling_units"
        floors = float(record.get("numfloors") or 0)
        if floors >= 3:
            return int(floors * 15), "floor_estimate"

    return 0, "none"


def _sortable_us(us_date: str) -> str:
    """MM/DD/YYYY as DOB writes it, to YYYY-MM-DD so it can be compared."""
    parts = str(us_date or "").strip().split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        mm, dd, yyyy = parts
        try:
            return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            return ""
    return str(us_date or "")[:10] if str(us_date or "").count("-") == 2 else ""


def _conversion_date(detail: str) -> str:
    """The date out of a DOB conversion note: "J-1 -> B (01/15/2021)"."""
    m = re.search(r"\((\d{1,2})/(\d{1,2})/(\d{4})\)", str(detail or ""))
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def _pre_cutoff_transient_evidence(record: dict) -> tuple[bool, str]:
    """Was this building in hotel use at the December 9, 2021 cutoff?

    That is the question the overlay's own copy asks — "hotels that closed or
    converted to residential post-2021 ... because their hotel use predates
    the 2021 text amendment" — and it used to be answered by asking whether
    the building still carries a hotel class, which is a different question
    with a different answer.
    """
    bldgclass = (record.get("bldgclass") or "").upper()

    # Strongest, and dated: DOB recorded the change out of transient use. If
    # that happened at or after the cutoff, the hotel was running right up to
    # it. If it happened before, the use had already gone when the amendment
    # landed and there is no pre-existing use to revert to — which the old
    # test could not say at all.
    converted = _conversion_date(record.get("dob_conversion_detail"))
    if converted:
        if converted >= HOTEL_SPECIAL_PERMIT_CUTOFF:
            return True, f"DOB records transient use here until {converted}"
        return False, (
            f"DOB records the transient use ending {converted}, before the "
            f"{HOTEL_SPECIAL_PERMIT_CUTOFF} cutoff"
        )

    # A hotel licence that was live at the cutoff says the same thing.
    created = str(record.get("hotel_license_created") or "")[:10]
    expires = str(record.get("hotel_license_expiration") or "")[:10]
    if created and created <= HOTEL_SPECIAL_PERMIT_CUTOFF and (
        not expires or expires >= HOTEL_SPECIAL_PERMIT_CUTOFF
    ):
        return True, f"DCWP hotel licence issued {created} and live at the cutoff"

    prior = record.get("prior_operator") or {}
    end = str(prior.get("end_year") or "")[:4]
    start = str(prior.get("start_year") or prior.get("since") or "")[:4]
    # A hotel that had already gone before the amendment has no use to revert
    # to. 330 East 56th Street was the Sutton Hotel and became condominiums in
    # 2005 — sixteen years early — and it sat in the list unverified, as
    # though somebody might yet find the evidence.
    if end.isdigit() and int(end) < 2021:
        return False, (
            f"{prior.get('name', 'the hotel')} left in {end}, before the "
            f"{HOTEL_SPECIAL_PERMIT_CUTOFF} cutoff"
        )
    if start.isdigit() and int(start) < 2021:
        return True, f"prior operator {prior.get('name', '')} from {start}"

    # The C of O route. No longer gated on the building class: a C of O
    # predating the cutoff on a building HPD now records as residential is the
    # ordinary shape of a conversion, and requiring an H class to look at all
    # is what hid 554 Third Avenue and 33 others.
    coos = record.get("coo_records") or []
    if bldgclass.startswith("H") or record.get("dob_has_r1") or record.get("dob_has_j1"):
        for coo in coos:
            iso = _coo_date(coo.get("issue_date", ""))
            if iso and iso < HOTEL_SPECIAL_PERMIT_CUTOFF:
                return True, f"transient occupancy on file with a C of O issued {iso}"

    # DOB's own filing dates. These were in the pull all along and dropped on
    # the way in, so every one of these buildings read "undated in our data" —
    # a statement about our plumbing that sounded like a statement about DOB.
    last = _sortable_us(record.get("dob_transient_last_filing"))
    if last:
        if last >= HOTEL_SPECIAL_PERMIT_CUTOFF:
            return True, f"DOB transient occupancy filed {last}, at or after the cutoff"
        # Filed before the cutoff and nothing records the use ending. That is
        # not proof it ran to December 2021, and saying which year it reaches
        # is worth more than saying nothing at all.
        return False, (
            f"DOB transient occupancy last filed {last}; nothing records it ending, "
            f"but nothing carries it to the cutoff either"
        )

    if record.get("dob_has_r1") or record.get("dob_has_j1"):
        return False, "DOB R-1/J-1 occupancy on file, with no filing date recorded"

    if prior.get("name"):
        # Somebody researched this building and wrote down which hotel it was,
        # without a year. Worth showing and worth checking; not worth
        # asserting.
        return False, (
            f"known former hotel ({prior['name']}), but nothing dates the use "
            f"to the cutoff"
        )

    # Nothing anywhere says this was ever a hotel. It is in the list because
    # its building class is a hotel class and HPD registers residential units
    # in it, which is a shape, not a history — and saying "no evidence found"
    # reads as though somewhere was searched and came back empty. Eleven of
    # the 42 candidates are here on nothing else.
    if not (record.get("dob_conversion_detail") or record.get("prior_operator")
            or record.get("dob_has_r1") or record.get("dob_has_j1")
            or record.get("hotel_license_status") or (record.get("coo_count") or 0) > 0):
        return False, (
            f"nothing records this as a hotel at all — no C of O, no DOB "
            f"occupancy filing, no licence, no operator. It is here because "
            f"the building class is {record.get('bldgclass', '?')} and HPD "
            f"registers {record.get('hpd_class_a') or 0} Class A units"
        )

    return False, "no transient use evidenced before the 2021-12-09 cutoff"


def _license_term_years(created: str, expires: str) -> float | None:
    """Length of a DCWP licence term, rounded to whole years."""
    try:
        start = date.fromisoformat(created[:10])
        end = date.fromisoformat(expires[:10])
    except (ValueError, TypeError):
        return None
    return round((end - start).days / 365.25)


# Places answered, but not about the building. A shop at street level says
# nothing about the 120 rooms above it, and a blank says nothing at all.
WEAK_PLACES_USES = {"", "ground_floor_tenant", "other"}


def _use_holds_building(cu: dict) -> bool:
    """Does this use occupy the building, or does it rent a suite in it?

    Two tests, and a use has to pass both.

    Most of the occupants carry it. One tenant of eight does not make the
    address a church, and reading it that way cost 238 buildings a penalty
    built for buildings that are genuinely in other hands.

    And it rests on what the place is rather than on what it is called.
    "Fountain Pen Hospital" on Warren Street is a pen shop; a name match alone
    carried it to a medical classification and a full penalty, and 49 of the
    278 penalties stood on nothing sturdier than that.
    """
    if not str(cu.get("basis") or "").startswith("type="):
        return False
    occupants = cu.get("occupants") or []
    if not occupants:
        return False
    use = cu.get("current_use")
    same = sum(1 for o in occupants if o.get("use") == use)
    return same / len(occupants) >= 0.5


def load_current_use() -> dict[str, dict]:
    """Load current-use findings keyed by BBL, Places first then the web.

    Produced by src/enrich_current_use.py. Records what is at the address
    today, which city records do not answer — 569 Lexington Avenue reads as
    730 Class B units in HPD and is a student dormitory on the ground.

    Places is a business directory, so it only sees uses that registered as a
    business. The ones that never do are invisible to it: 35-02 37 Avenue runs
    as a shelter for single adults under a $65.8m city contract to June 2030,
    and all Places had was the Citi Bike dock at the kerb. src/enrich_web_use.py
    reads the open web for exactly those, and its finding is laid over the top
    wherever Places came back with a shop or with nothing. It never displaces a
    real Places answer — a building Places calls a hotel is a hotel, and a news
    page about what it used to be is not better evidence than that.
    """
    files = sorted(DATA_RAW.glob("google_current_use_[0-9]*.json"), reverse=True)
    if not files:
        return {}
    by_bbl = {r["bbl"]: r for r in json.loads(files[0].read_text()) if r.get("bbl")}
    # Read under today's rules, not under whichever ones were in force the day
    # the sweep ran. A file on disk is the places Google returned; what they
    # mean is decided here, every build.
    restated = sum(restate_current_use(r) for r in by_bbl.values())
    if restated:
        print(f"  Current use: {restated} stored readings restated under the current rules")

    web_files = sorted(DATA_RAW.glob("web_current_use_[0-9]*.json"), reverse=True)
    if not web_files:
        return by_bbl
    for w in json.loads(web_files[0].read_text()):
        bbl = w.get("bbl")
        if not bbl or not w.get("current_use"):
            continue
        held = by_bbl.get(bbl, {})
        if (held.get("current_use") or "") not in WEAK_PLACES_USES:
            continue
        by_bbl[bbl] = {
            **held,
            "bbl": bbl,
            "current_use": w["current_use"],
            "current_use_label": w["current_use_label"],
            "use_confidence": w.get("use_confidence", "low"),
            "basis": w.get("basis", ""),
            "google_name": "",
            # A web reading is a sentence in an article, not an address-verified
            # listing, so it always goes in front of a person before it removes
            # a building from anyone's list.
            "needs_review": True,
            "swept": True,
            "web_evidence": w.get("evidence", []),
        }
    return by_bbl


def load_city_record_shelter() -> dict[str, dict]:
    """Buildings the city has published a DHS or HRA shelter notice for.

    Produced by src/enrich_city_record.py. This flags and never decides, and
    the dates are why: the City Record carries the publication date of a
    notice, not the term of the contract inside it. 17 Battery Place holds a
    DHS notice from December 2008 and is an operating hotel today. A source
    that spoke with authority here would delete it from the list on
    eighteen-year-old evidence.
    """
    files = sorted(DATA_RAW.glob("city_record_shelter_[0-9]*.json"), reverse=True)
    if not files:
        return {}
    return {r["bbl"]: r for r in json.loads(files[0].read_text()) if r.get("bbl")}


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
    google_names = load_google_hotel_names()
    dob_occ_by_bbl = load_dob_occupancy(dob_occupancy_path)
    permit_keywords_by_bbl = scan_permit_descriptions(permits_path)
    hotel_licenses = load_hotel_licenses()
    current_use = load_current_use()
    shelter_notices = load_city_record_shelter()
    hpd_regs = load_hpd_registrations()
    # The date on the file each contact was actually read from, so a contact can
    # say how stale it is without the reader going to look.
    hpd_pulled_on = provenance.vintage(provenance.resolve("hpd_registrations", DATA_RAW)) or ""
    acris_pulled_on = provenance.vintage(provenance.resolve("acris_owners", DATA_RAW)) or ""
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
            record["ecb_illegal_transient"] = ecb_v.get("illegal_transient", 0)
            if record["ecb_illegal_transient"]:
                record.setdefault("reason_codes", []).append("illegal_transient_violation")
        else:
            record["ecb_open_violations"] = 0
            record["ecb_total_balance"] = 0
            record["ecb_hazardous"] = 0
            record["ecb_illegal_transient"] = 0

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

        # Hotel info (OSM, then Google Places fallback)
        hi = hotel_info.get(bbl)
        gname = google_names.get(bbl, "")
        if hi:
            record["hotel_name"] = hi.get("hotel_name", "") or gname
            record["hotel_phone"] = hi.get("phone", "")
            record["hotel_website"] = hi.get("website", "")
        elif gname:
            record["hotel_name"] = gname
            record["hotel_phone"] = ""
            record["hotel_website"] = ""
        else:
            record["hotel_name"] = ""
            record["hotel_phone"] = ""
            record["hotel_website"] = ""

        # Facade inspection programme
        try:
            stories = float(record.get("numfloors") or 0)
        except (TypeError, ValueError):
            stories = 0.0
        record["fisp_applicable"] = stories > FISP_MIN_STORIES
        record["fisp_stories"] = int(stories)

        # Safe Hotels Act room thresholds
        rooms, basis = _guest_rooms(record)
        record["safe_hotels_guest_rooms"] = rooms
        record["safe_hotels_room_basis"] = basis
        # A floor estimate is too soft to assert a legal threshold against, so
        # it flags nothing — it only tells the reader the count is unknown.
        reliable = basis in ("hpd_class_b", "coo_dwelling_units")
        record["safe_hotels_direct_employment"] = bool(
            reliable and rooms >= SAFE_HOTELS_DIRECT_EMPLOYMENT_ROOMS
        )
        record["safe_hotels_large_hotel"] = bool(
            reliable and rooms > SAFE_HOTELS_LARGE_HOTEL_ROOMS
        )

        # A shelter notice the city printed. Flag only — see
        # load_city_record_shelter for why this never sets the use itself.
        notice = shelter_notices.get(bbl)
        if notice:
            record["shelter_notice"] = True
            # active / uncertain / historical. 17 Battery Place last appeared
            # in a DHS notice in 2008 and is a working hotel; 1 Hoyt Street
            # was awarded to a named operator for $51.9m in 2026. Grading them
            # apart is the difference between a flag worth reading and noise.
            record["shelter_status"] = notice.get("shelter_status", "")
            record["shelter_status_basis"] = notice.get("shelter_status_basis", "")
            record["shelter_notice_date"] = notice.get("latest_notice", "")
            record["shelter_notice_count"] = notice.get("notice_count", 0)
            record["shelter_notice_evidence"] = notice.get("notices", [])[:3]

        # The City Record is not the only place a shelter announces itself.
        # 130 3rd Street's own Google occupant is called "Refugee Shelter" and
        # it carried no flag at all, so the Hide-active-shelters control could
        # not reach it and it sat in the target list with 16 Class B rooms.
        # Four more read the same way, two of them in sourcing segments.
        #
        # Only where the sweep itself concluded shelter or supportive housing
        # — a name alone is how "Gimme Shelter Productions" at 15 East 20th
        # Street would arrive, which is a media company. The suffix list is
        # for exactly that shape: a word that marks the name as a business
        # rather than a facility.
        if not record.get("shelter_status"):
            cu_row = current_use.get(bbl) or {}
            cu_use = cu_row.get("current_use", "")
            cu_name = (cu_row.get("google_name") or "")
            low = cu_name.lower()
            not_a_facility = any(w in low for w in (
                "production", "records", "music", "film", "studio", "band", "tours"))
            if cu_use == "shelter" or (
                cu_use == "supportive_housing"
                and re.search(r"\bshelters?\b", low)
                and not not_a_facility
            ):
                record["shelter_status"] = "active"
                record["shelter_status_basis"] = (
                    f"the current-use sweep found {cu_name} here" if cu_name
                    else "the current-use sweep classified this building as a shelter")

        # Current use on the ground (Google Places, address-verified)
        cu = current_use.get(bbl)
        # A row with no occupant now means "asked, nothing came back", which is
        # different from never having asked and must not be read as a finding.
        if cu and not cu.get("current_use") and cu.get("swept"):
            record["current_use"] = ""
            record["current_use_label"] = ""
            record["current_use_name"] = ""
            record["current_use_confidence"] = ""
            record["current_use_basis"] = cu.get("basis", "no match")
            record["current_use_occupants"] = []
            # Nothing came back from Places, but the city may still have
            # printed a notice for the address — that is exactly the building
            # this whole source exists for.
            record["current_use_needs_review"] = bool(notice)
            record["current_use_checked"] = True
            record["current_use_conflict"] = False
        elif cu:
            record["current_use"] = cu.get("current_use", "")
            record["current_use_label"] = cu.get("current_use_label", "")
            record["current_use_name"] = cu.get("google_name", "")
            record["current_use_confidence"] = cu.get("use_confidence", "")
            record["current_use_basis"] = cu.get("basis", "")
            # The Places id of whatever the sweep resolved. Carried so the
            # profile can link straight to the listing it is quoting, rather
            # than to an address search that may land somewhere else.
            record["current_use_place_id"] = cu.get("place_id", "")
            record["current_use_occupants"] = cu.get("occupants", [])
            record["current_use_needs_review"] = bool(cu.get("needs_review")) or bool(notice)
            record["current_use_checked"] = True
            # A shelter, church or clinic is not a sourcing target no matter
            # how many Class B units it registers — but it has to actually
            # hold the building. The penalty used to fire on any occupant at
            # the address and landed on 278 buildings, 238 of which had the
            # use in one suite among many: 33 Rector Street was marked
            # religious because Orthodox Union has an office there, beside a
            # wine shop and a parking garage, and 13 Rector Street was marked
            # government in a building whose other tenants are 7-Eleven and
            # Dunkin'. Thirty-five points came off each of them.
            #
            # Flagged rather than excluded: a human should still see them
            # before the building leaves the list.
            if cu.get("current_use") in NON_TRANSIENT_CURRENT_USES and _use_holds_building(cu):
                record["current_use_conflict"] = True
                record.setdefault("reason_codes", []).append("current_use_conflict")
            else:
                record["current_use_conflict"] = False
        else:
            record["current_use"] = ""
            record["current_use_label"] = ""
            record["current_use_name"] = ""
            record["current_use_confidence"] = ""
            record["current_use_basis"] = ""
            record["current_use_occupants"] = []
            record["current_use_needs_review"] = False
            record["current_use_checked"] = False
            record["current_use_conflict"] = False

        # DOB occupancy classification (R-1/J-1 transient signal)
        dob_occ = dob_occ_by_bbl.get(bbl)
        if dob_occ:
            record["dob_has_r1"] = dob_occ.get("has_r1", False)
            record["dob_has_j1"] = dob_occ.get("has_j1", False)
            record["dob_r1_filing_count"] = dob_occ.get("r1_filing_count", 0)
            record["dob_transient_units"] = dob_occ.get("max_dwelling_units", 0)
            # When DOB last saw a transient filing here. The reversion rule
            # needs it and the comment above it used to read "DOB occupancy
            # filings carry no date through the pipeline" — they carry two,
            # and both were dropped on the way in.
            record["dob_transient_first_filing"] = dob_occ.get("earliest_date", "")
            record["dob_transient_last_filing"] = dob_occ.get("latest_date", "")
            # Tier upgrade: R-1 in DOB = legally established transient use
            tier = record.get("tier", "")
            class_b = record.get("hpd_class_b", 0) or 0
            if dob_occ["has_r1"] and class_b == 0 and tier in ("unknown", "partial"):
                record["tier"] = "legal_transient"
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
        #
        # Licensure and operation are two different facts and the tool used to
        # treat them as one: a license alone marked a building as having an
        # active operator, which segmented it away from the available-capacity
        # list. Since the Safe Hotels Act the licensee is the owner, who may
        # run the hotel themselves, contract it out, or hold a license on a
        # building nobody is operating yet. Licensure is recorded here;
        # whether anyone is running the building is decided separately, from
        # operator evidence, in build_geojson.
        hl = hotel_licenses.get(bbl)
        if hl:
            record["hotel_license_name"] = hl["business_name"]
            record["hotel_license_status"] = hl["license_status"]
            record["hotel_license_expiration"] = hl["license_expiration"]
            record["hotel_license_created"] = hl.get("license_creation_date", "")
            record["has_hotel_license"] = True
            tier = record.get("tier", "")
            status = hl["license_status"]

            created = hl.get("license_creation_date", "")
            record["safe_hotels_licensed"] = bool(
                created >= SAFE_HOTELS_EFFECTIVE
                and status in ("Active", "Ready for Renewal")
            )
            record["hotel_license_term_years"] = _license_term_years(
                created, hl.get("license_expiration", "")
            )
            if status in ("Active", "Ready for Renewal"):
                if tier in ("unknown", "partial"):
                    record["tier"] = "legal_transient"
                    record["confidence"] = "high"
                    record.setdefault("reason_codes", []).append("dcwp_hotel_license")
                # Active license means it's operating as a hotel now — not a reversion candidate
                if record.get("reversion_window"):
                    record["reversion_window"] = None
                    record["has_reversion"] = False
                    record["segment"] = "hotel"
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
            record["hotel_license_created"] = ""
            record["has_hotel_license"] = False
            record["safe_hotels_licensed"] = False
            record["hotel_license_term_years"] = None

        # A building trading as a hotel right now has nothing to revert to.
        #
        # The licence test above catches this only where DCWP shows an active
        # one, and licences lapse while hotels keep trading: 50 Bowery reads
        # "Failed to Renew" at DCWP and is Hotel 50 Bowery, JDV by Hyatt, on
        # the ground. 123 Washington Street is The Washington Hotel NYC with
        # no licence on file at all. Both were sitting in the reversion list
        # as buildings that had converted to residential.
        if record.get("reversion_window") and record.get("current_use") == "hotel":
            record["reversion_window"] = None
            record["has_reversion"] = False
            rc = record.get("reason_codes", [])
            if "reversion_window" in rc:
                rc.remove("reversion_window")

        # Reversion window — test it against the 2021-12-09 cutoff
        #
        # Runs after the DCWP block on purpose. An active licence clears the
        # reversion window outright — the building is trading as a hotel, so
        # there is nothing to revert. Testing before that left 10 active
        # hotels carrying an "unverified reversion" warning for a window
        # that had already been withdrawn.
        #
        # pipeline.py proposes a candidate from building class and unit mix
        # alone, before any C of O history is attached. That produced 27
        # candidates; hand research on those 27 confirmed 2, called 7 outright
        # wrong and left 18 unclear. The overlay is only worth something when
        # the transient use predates the special-permit amendment, so the test
        # runs here, where the C of O records exist.
        rw = record.get("reversion_window")
        if rw:
            pre_cutoff, evidence = _pre_cutoff_transient_evidence(record)
            rw["pre_2021_use"] = pre_cutoff
            rw["pre_2021_evidence"] = evidence
            if not pre_cutoff:
                # Kept, not deleted. Losing the grandfathering is the likeliest
                # reading, but a C of O gap is not proof of one, and silently
                # dropping buildings is what the review objected to.
                rw["unverified"] = True
                record["reversion_unverified"] = True
                # The sentence this function worked out, kept where the build
                # can publish it. reversion_window is not emitted, so the only
                # thing reaching a reader was the boolean — and the profile
                # rendered every one of them as "no record shows transient use
                # here before the cutoff". For 26 of the 42 that is false:
                # 700 8 Avenue registers 1,332 Class B rooms and 353 West 57th
                # is the Hudson New York. What is true of those is narrower —
                # the records are there and nothing dates them to the cutoff —
                # and the difference decides whether a special permit is
                # needed, so it is not a nuance to round off.
                record["reversion_unverified_reason"] = evidence
                record.setdefault("reason_codes", []).append("reversion_unverified")
            else:
                rw["unverified"] = False
                record["reversion_unverified"] = False
        else:
            record["reversion_unverified"] = False

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

        # --- Who to write to, and where that came from -----------------------
        #
        # Two free sources. HPD registration reaches most of the map because
        # every multiple dwelling must register annually and name who answers
        # for it; ACRIS reaches whatever has traded, and gives the entity on
        # the deed with the address it filed. Both join on BBL, which is why
        # this is a clean join and the hotel-side address matching was not.
        #
        # Each contact carries its own source and pull date rather than
        # inheriting the file's, because they are different datasets pulled on
        # different days, and a reader about to send a letter should be able to
        # see how old the address is.
        contacts = []
        if hpd_reg:
            for slot, kind in (("owner_contact", "owner"), ("agent_contact", "managing_agent")):
                c = hpd_reg.get(slot)
                if c and (c.get("name") or c.get("corp")):
                    contacts.append({
                        "kind": kind,
                        "name": c.get("name", ""),
                        "org": c.get("corp", ""),
                        "role": c.get("role", ""),
                        "address": c.get("address", ""),
                        "source": "HPD registration",
                        "source_detail": "NYC HPD Registration Contacts (feu5-w2e2)",
                        "pulled_on": hpd_pulled_on,
                    })
        # ACRIS only as an owner when HPD gave none: HPD is annual and current,
        # a deed is whenever the building last sold.
        if not any(c["kind"] == "owner" for c in contacts):
            deed_owner = (record.get("acris_deed_owner") or "").strip()
            deed_addr = (record.get("acris_deed_address") or "").strip()
            if deed_owner:
                contacts.append({
                    "kind": "owner",
                    "name": "",
                    "org": deed_owner,
                    "role": "DeedOwner",
                    "address": deed_addr,
                    "source": "ACRIS deed",
                    "source_detail": "NYC ACRIS master/legals",
                    "pulled_on": acris_pulled_on,
                })
        record["owner_contacts"] = contacts
        # A contact with nowhere to send anything is a name, not a contact.
        # The panel needs that distinction to decide whether to offer a paid
        # lookup, so it is decided here rather than in the browser.
        record["has_free_contact"] = any(c.get("address") for c in contacts)

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
            stab = rs["stabilized_units"]
            class_a = record.get("hpd_class_a") or 0
            class_b = record.get("hpd_class_b") or 0
            record["rent_stabilized_units"] = stab
            record["rent_stab_data_year"] = rs["data_year"]
            # Stabilization attaches to the Class A (permanent) units first. It only
            # threatens the transient rooms once the stabilized count exceeds the
            # Class A side — or in rooming stock, where the Class B rooms are
            # themselves stabilization-eligible and class_a is 0. Flagging any
            # stabilized unit as a blocker over-reports on split-use buildings.
            exposure = max(0, stab - class_a)
            record["rent_stab_class_b_exposure"] = exposure
            if exposure > 0 and class_b > 0:
                if class_a == 0:
                    detail = (
                        f"{stab} rent-stabilized rooms (as of {rs['data_year']} tax bill), "
                        f"no Class A units to absorb them — conversion to transient use restricted"
                    )
                else:
                    detail = (
                        f"{exposure} of {stab} rent-stabilized units fall outside the {class_a} Class A units "
                        f"(as of {rs['data_year']} tax bill) — conversion of transient rooms restricted"
                    )
                record.setdefault("blockers", []).append(detail)
        else:
            record["rent_stabilized_units"] = 0
            record["rent_stab_data_year"] = None
            record["rent_stab_class_b_exposure"] = 0

        # Zoning compatibility for hotel use
        zoning_compat, zoning_detail = _zoning_hotel_compatibility(record.get("zonedist1", ""))
        record["zoning_hotel_permitted"] = zoning_compat
        record["zoning_hotel_detail"] = zoning_detail

        # Consolidated operator name (best available source)
        # Prefer hotel_name (from OSM/Google Places) over license LLC name
        operator = ""
        operator_source = ""
        if record.get("hotel_name"):
            operator = record["hotel_name"]
            operator_source = "google_places" if gname else "osm"
        elif record.get("hotel_license_name"):
            operator = record["hotel_license_name"]
            operator_source = "dcwp_license"
        elif record.get("hpd_managing_agent_corp"):
            operator = record["hpd_managing_agent_corp"]
            operator_source = "hpd_managing_agent"
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

    # Blockers accumulate across enrichment passes and can repeat verbatim.
    # Keep first occurrence so the detail panel doesn't render duplicate rows.
    for record in pipeline:
        if record.get("blockers"):
            record["blockers"] = list(dict.fromkeys(record["blockers"]))

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
    cu_checked = sum(1 for r in pipeline if r.get("current_use_checked"))
    cu_conflict = sum(1 for r in pipeline if r.get("current_use_conflict"))
    print(f"  Current use (Google Places): {cu_checked} buildings checked")
    print(f"    Conflicts with tier: {cu_conflict} buildings (dorm/shelter/religious/medical)")
    comp_upgraded = sum(1 for r in pipeline if "composite_signal" in r.get("reason_codes", []))
    print(f"  Composite signal upgrades: {comp_upgraded} buildings")
    gn_count = sum(1 for r in pipeline if r.get("hotel_name") and r["bbl"] in google_names)
    print(f"  Google Places hotel names: {len(google_names)} loaded, {gn_count} matched to pipeline")
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
