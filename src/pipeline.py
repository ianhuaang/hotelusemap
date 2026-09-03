"""Filter funnel: PLUTO -> building class partition -> HPD join -> tier assignment.

Outputs a list of per-BBL records with tier, confidence, reason_codes, blockers.
"""

import csv
import json
from datetime import date
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DATA_RAW, DATA_PROCESSED, MIN_RESIDENTIAL_UNITS,
    TIER_LEGAL_TRANSIENT, TIER_PARTIAL,
    TIER_UNKNOWN, TIER_EXCLUDED,
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
    TARGET_CDS,
)

TODAY = date.today().strftime("%Y%m%d")

# Building classes that indicate direct hotel/transient use
HOTEL_CLASSES = {"H1", "H2", "H3", "H4", "H5", "H6", "H7", "H9",
                 "HB", "HH", "HS", "RH"}

# Excluded hotel-adjacent classes — legal constraints make them non-targets.
# NOTE: the tier exclusion below only fires when class_b == 0, so it never
# catches the SROs/dorms that actually reach the target list. Those are flagged
# via RESTRICTED_CONVERSION_CLASSES instead and hidden behind a UI toggle, so
# they stay reachable rather than being dropped from the dataset entirely.
EXCLUDED_HOTEL_CLASSES = {"HR", "H8"}  # HR=SRO (rent-regulated), H8=dormitory

# Classes whose conversion to conventional transient use is legally or
# operationally restricted. Kept in the data, hidden from the default view.
RESTRICTED_CONVERSION_CLASSES = {
    "HR": "SRO — rent-regulated rooming stock, conversion restricted",
    "RS": "SRO — rent-regulated rooming stock, conversion restricted",
    "H8": "Dormitory — institutional use, not a conventional hotel target",
    "HH": "Hostel — shared-room operating model, not a conventional hotel target",
}

# Mixed residential/commercial classes — the interesting middle
MIXED_CLASSES = {"RM", "RR", "RC", "RD", "RK", "RI", "RW", "RS", "RX"}

# 1-4 family: drop these (not the target building profile)
SMALL_RESIDENTIAL = {"A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9",
                     "B1", "B2", "B3", "B9"}


def _normalize_bbl(raw_bbl: str) -> str:
    """PLUTO BBLs come as floats like '1000010010.00000000'. Normalize to 10-digit str."""
    return str(int(float(raw_bbl)))


def _hpd_bbl(row: dict) -> str:
    """Construct 10-digit BBL from HPD boroid/block/lot."""
    return f"{row['boroid']}{int(row['block']):05d}{int(row['lot']):04d}"


def load_pluto(path: Path = None) -> list[dict]:
    if path is None:
        path = DATA_RAW / f"pluto_{TODAY}.json"
    if not path.exists():
        files = sorted(DATA_RAW.glob("pluto_*.json"), reverse=True)
        if not files:
            raise FileNotFoundError("No PLUTO data found")
        path = files[0]
        print(f"Using cached PLUTO: {path.name}")
    return json.loads(path.read_text())


def load_hpd(path: Path = None) -> list[dict]:
    if path is None:
        path = DATA_RAW / f"hpd_{TODAY}.json"
    if not path.exists():
        files = sorted(DATA_RAW.glob("hpd_[0-9]*.json"), reverse=True)
        if not files:
            raise FileNotFoundError("No HPD data found")
        path = files[0]
        print(f"Using cached HPD: {path.name}")
    return json.loads(path.read_text())


def load_prior_operators(path: Path = None) -> dict[str, dict]:
    """Load prior-operator ground truth keyed by BBL."""
    if path is None:
        path = Path(__file__).parent.parent / "ground_truth.csv"
    result = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["label_type"] == "prior_operator" and row.get("bbl"):
                result[row["bbl"]] = {
                    "name": row["name"],
                    "address": row["address"],
                    "notes": row.get("notes", ""),
                }
    return result


def _load_dob_occupancy_bbls() -> set[str]:
    """Load BBLs with R-1/J-1 occupancy from the full DOB pull."""
    files = sorted(DATA_RAW.glob("dob_occupancy_*.json"), reverse=True)
    if not files:
        return set()
    raw = json.loads(files[0].read_text())
    return {r["bbl"] for r in raw if r.get("has_r1") or r.get("has_j1")}


def _load_dob_conversion_bbls() -> dict[str, str]:
    """Load BBLs with confirmed conversion FROM transient occupancy."""
    files = sorted(DATA_RAW.glob("dob_occupancy_*.json"), reverse=True)
    if not files:
        return {}
    raw = json.loads(files[0].read_text())
    return {r["bbl"]: r.get("conversion_detail", "") for r in raw if r.get("has_conversion_from_transient")}


def run_pipeline(pluto_path: Path = None, hpd_path: Path = None) -> list[dict]:
    pluto = load_pluto(pluto_path)
    hpd = load_hpd(hpd_path)
    prior_ops = load_prior_operators()
    dob_transient_bbls = _load_dob_occupancy_bbls()
    if dob_transient_bbls:
        print(f"DOB transient occupancy: {len(dob_transient_bbls)} BBLs loaded as entry source")
    dob_conversion_bbls = _load_dob_conversion_bbls()
    if dob_conversion_bbls:
        print(f"DOB conversions from transient: {len(dob_conversion_bbls)} BBLs")

    # --- Step 1: PLUTO filter ---
    # Keep lots with buildings that have enough residential units OR are hotel-class
    survivors = []
    dob_additions = 0
    for row in pluto:
        bbl = _normalize_bbl(row["bbl"])
        bldgclass = (row.get("bldgclass") or "").strip().upper()
        unitsres = int(float(row.get("unitsres") or 0))
        numbldgs = int(float(row.get("numbldgs") or 0))
        borough = (row.get("borough") or "").strip().upper()
        cd = str(row.get("cd") or "").strip()

        # For BK/QN, only keep target community districts
        if borough in ("BK", "QN") and cd not in TARGET_CDS:
            continue

        # Drop lots with no building
        if numbldgs < 1:
            continue

        # Drop 1-4 family
        if bldgclass in SMALL_RESIDENTIAL:
            continue

        # Keep if: hotel class, mixed class, meets unit minimum, prior operator,
        # or DOB transient occupancy (R-1/J-1)
        is_hotel = bldgclass[:2] in {c[:2] for c in HOTEL_CLASSES} or bldgclass in HOTEL_CLASSES
        is_mixed = bldgclass in MIXED_CLASSES
        meets_units = unitsres >= MIN_RESIDENTIAL_UNITS
        is_prior_op = bbl in prior_ops
        is_dob_transient = bbl in dob_transient_bbls

        if is_hotel or is_mixed or meets_units or is_prior_op or is_dob_transient:
            row["_bbl"] = bbl
            row["_bldgclass"] = bldgclass
            row["_unitsres"] = unitsres
            if is_dob_transient and not (is_hotel or is_mixed or meets_units or is_prior_op):
                dob_additions += 1
            survivors.append(row)

    print(f"PLUTO filter: {len(pluto)} -> {len(survivors)} survivors")
    if dob_additions:
        print(f"  ({dob_additions} added by DOB R-1/J-1 that would have been filtered out)")

    # --- Step 2: HPD join ---
    # Build HPD lookup by BBL (some BBLs have multiple HPD records — take max classB)
    hpd_by_bbl = {}
    for row in hpd:
        bbl = _hpd_bbl(row)
        class_b = int(row.get("legalclassb") or 0)
        class_a = int(row.get("legalclassa") or 0)
        existing = hpd_by_bbl.get(bbl)
        if existing is None or class_b > existing["legalclassb"]:
            hpd_by_bbl[bbl] = {
                "legalclassa": class_a,
                "legalclassb": class_b,
                "dobbuildingclass": row.get("dobbuildingclass", ""),
                "legalstories": row.get("legalstories", ""),
                "bin": row.get("bin", ""),
            }

    # --- Step 3: Tier assignment ---
    results = []
    seen_bbls = set()

    for row in survivors:
        bbl = row["_bbl"]
        if bbl in seen_bbls:
            continue
        seen_bbls.add(bbl)

        bldgclass = row["_bldgclass"]
        unitsres = row["_unitsres"]
        hpd_info = hpd_by_bbl.get(bbl, {})
        class_b = hpd_info.get("legalclassb", 0)
        class_a = hpd_info.get("legalclassa", 0)
        is_prior_op = bbl in prior_ops

        reason_codes = []
        blockers = []

        is_excluded_class = bldgclass in EXCLUDED_HOTEL_CLASSES
        restricted_reason = RESTRICTED_CONVERSION_CLASSES.get(bldgclass)
        is_hotel_class = (bldgclass in HOTEL_CLASSES or bldgclass[:1] == "H") and not is_excluded_class
        is_mixed_class = bldgclass in MIXED_CLASSES
        is_dob_transient = bbl in dob_transient_bbls

        # --- Tier assignment ---

        if is_excluded_class and class_b == 0:
            tier = TIER_EXCLUDED
            confidence = CONFIDENCE_HIGH
            if bldgclass == "HR":
                reason_codes.append("sro_regulated")
                blockers.append("SRO — regulated under Multiple Dwelling Law, cannot convert to transient")
            else:
                reason_codes.append("dormitory")
                blockers.append("Dormitory — institutional use, not a hotel target")
        elif class_b > 0:
            # HPD confirms transient rooms exist
            tier = TIER_LEGAL_TRANSIENT
            confidence = CONFIDENCE_HIGH if class_a == 0 else CONFIDENCE_MEDIUM
            reason_codes.append("hpd_class_b")
            if is_hotel_class:
                reason_codes.append("bldg_class_hotel")
        elif is_hotel_class and class_a > 0:
            # Hotel class but HPD shows residential only
            tier = TIER_LEGAL_TRANSIENT
            reason_codes.append("bldg_class_hotel")
            has_conversion_evidence = (
                bbl in dob_conversion_bbls  # DOB filing changed occupancy from transient
                or class_a >= 5            # substantial residential presence, not just a super's apt
            )
            if has_conversion_evidence:
                confidence = CONFIDENCE_MEDIUM
                reason_codes.append("reversion_window")
            else:
                confidence = CONFIDENCE_LOW
        elif is_hotel_class:
            # Hotel class, no HPD data
            tier = TIER_LEGAL_TRANSIENT
            confidence = CONFIDENCE_MEDIUM
            reason_codes.append("bldg_class_hotel")
        elif is_mixed_class:
            tier = TIER_PARTIAL
            confidence = CONFIDENCE_LOW
            reason_codes.append(f"bldg_class_{bldgclass}")
        elif is_dob_transient:
            tier = TIER_PARTIAL
            confidence = CONFIDENCE_MEDIUM
            reason_codes.append("dob_transient_occupancy")
        else:
            tier = TIER_UNKNOWN
            confidence = CONFIDENCE_LOW
            reason_codes.append(f"bldg_class_{bldgclass}")

        # --- Overlays ---

        # Prior operator (doesn't change tier)
        prior_op_info = prior_ops.get(bbl)

        # Class B split-use overlay
        class_b_split = None
        if class_b > 0 and class_a > 0:
            class_b_split = {
                "class_a": class_a,
                "class_b": class_b,
                "pct_transient": round(class_b / (class_a + class_b) * 100),
            }

        # Reversion window overlay — only if there's evidence of actual conversion
        reversion_window = None
        conversion_detail = dob_conversion_bbls.get(bbl, "")
        if is_hotel_class and class_a > 0 and class_b == 0:
            has_conversion_evidence = bool(conversion_detail) or class_a >= 5
            if has_conversion_evidence:
                evidence = conversion_detail or f"{class_a} Class A units registered"
                reversion_window = {
                    "deadline": "2027-12-09",
                    "signal": "hotel_class_residential_only",
                    "class_a_units": class_a,
                    "evidence": evidence,
                    "note": f"Building class {bldgclass} (hotel) converted to residential ({evidence}). Can revert to hotel use without special permit before Dec 9, 2027.",
                }


        record = {
            "bbl": bbl,
            "address": row.get("address", ""),
            "bldgclass": bldgclass,
            "unitsres": unitsres,
            "unitstotal": int(float(row.get("unitstotal") or 0)),
            "numfloors": float(row.get("numfloors") or 0),
            "bldgarea": int(float(row.get("bldgarea") or 0)),
            "comarea": int(float(row.get("comarea") or 0)),
            "resarea": int(float(row.get("resarea") or 0)),
            "lotarea": int(float(row.get("lotarea") or 0)),
            "cd": row.get("cd", ""),
            "zonedist1": row.get("zonedist1", ""),
            "ownername": row.get("ownername", ""),
            "tier": tier,
            "confidence": confidence,
            "reason_codes": reason_codes,
            "blockers": blockers,
            "hpd_class_a": class_a,
            "hpd_class_b": class_b,
            "restricted_class": bool(restricted_reason),
            "restricted_class_reason": restricted_reason or "",
            "hpd_dob_class": hpd_info.get("dobbuildingclass", ""),
            "hpd_stories": hpd_info.get("legalstories", ""),
            "prior_operator": prior_op_info,
            "reversion_window": reversion_window,
            "class_b_split": class_b_split,
            "source_pulled_on": TODAY,
        }
        results.append(record)

    # Add prior operators that didn't survive the PLUTO filter
    for bbl, op_info in prior_ops.items():
        if bbl not in seen_bbls:
            hpd_info = hpd_by_bbl.get(bbl, {})
            class_b = hpd_info.get("legalclassb", 0)
            class_a = hpd_info.get("legalclassa", 0)
            results.append({
                "bbl": bbl,
                "address": op_info["address"],
                "bldgclass": "",
                "unitsres": 0,
                "unitstotal": 0,
                "numfloors": 0,
                "bldgarea": 0,
                "comarea": 0,
                "resarea": 0,
                "lotarea": 0,
                "cd": "",
                "zonedist1": "",
                "ownername": "",
                "tier": TIER_PARTIAL,
                "confidence": CONFIDENCE_LOW,
                "reason_codes": ["prior_operator_only"],
                "blockers": [],
                "hpd_class_a": class_a,
                "hpd_class_b": class_b,
                "restricted_class": bool(restricted_reason),
                "restricted_class_reason": restricted_reason or "",
                "hpd_dob_class": hpd_info.get("dobbuildingclass", ""),
                "hpd_stories": hpd_info.get("legalstories", ""),
                "prior_operator": op_info,
                "reversion_window": None,
                "class_b_split": None,
                "source_pulled_on": TODAY,
            })

    # Summary
    from collections import Counter
    tier_counts = Counter(r["tier"] for r in results)
    print(f"Pipeline output: {len(results)} buildings")
    for tier, count in tier_counts.most_common():
        print(f"  {tier}: {count}")

    return results


def save_pipeline_output(results: list[dict], path: Path = None) -> Path:
    if path is None:
        DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
        path = DATA_PROCESSED / f"pipeline_{TODAY}.json"
    path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Saved -> {path}")
    return path


if __name__ == "__main__":
    results = run_pipeline()
    save_pipeline_output(results)
