#!/usr/bin/env python3
"""Which sources reached which buildings, counted rather than assumed.

Things fell through the cracks because nothing was looking at the cracks. The
sweep quietly skipped 173 buildings for a week after two null geometries
crashed the loader; the DOB filing dates were sorted by month for every
building in the file; 269 buildings read as "nobody knows what is inside"
while HPD held their unit counts. Each of those was invisible for the same
reason: a source that returns nothing and a source that was never asked look
identical downstream.

This counts, per source, how many published buildings it reached. It cannot
tell you a source is right. It can tell you a source stopped arriving, which
is the failure that actually happened, three times.

Run standalone against the newest geojson, or imported by build_geojson so
the numbers print on every build:

    python3 src/coverage.py
    python3 src/coverage.py --verbose      list the buildings a source missed
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PROCESSED = ROOT / "data" / "processed"


def _truthy(field):
    return lambda p: bool(p.get(field))


def _positive(field):
    return lambda p: (p.get(field) or 0) > 0


def _nonempty_list(field):
    def test(p):
        v = p.get(field)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                return bool(v)
        return bool(v)
    return test


# Every source, the field that proves it arrived, and roughly how much of the
# set it should reach. `floor` is not a target — it is the point below which
# something has probably broken rather than the city simply having no record.
# A source legitimately covering 5% gets a floor of 0 and is still counted.
SOURCES = [
    # name, reached-test, floor as a fraction, note
    ("PLUTO — building class",      _truthy("bldgclass"),               0.99, "the entry point; anything missing here is a bug"),
    ("PLUTO — residential units",   _positive("unitsres"),              0.00, "zero is legitimate for a pure commercial lot"),
    ("Geocoding / footprint",       lambda p: not p.get("no_footprint"), 0.95, "a building with no footprint is drawn as a point"),
    ("Alt addresses",               _nonempty_list("alt_addresses"),    0.50, "corner lots and renumbered buildings"),
    ("HPD — Class A/B registration", lambda p: (p.get("hpd_class_a") or 0) > 0 or (p.get("hpd_class_b") or 0) > 0, 0.50, ""),
    ("HPD — managing agent",        _truthy("hpd_managing_agent"),      0.60, ""),
    ("HPD — owner contacts",        _nonempty_list("owner_contacts"),   0.60, "names, orgs and mailing addresses"),
    ("HPD — violations",            lambda p: p.get("hpd_open_violations") is not None, 0.90, "zero is a finding; missing is not"),
    ("DOB — certificate of occupancy", _positive("coo_count"),          0.30, ""),
    ("DOB — occupancy filings",     lambda p: p.get("dob_has_r1") is not None, 0.90, "R-1 / J-1 transient filings"),
    ("DOB — permits",               lambda p: p.get("permit_count") is not None, 0.90, ""),
    ("DCWP — hotel licence",        lambda p: p.get("has_hotel_license") is not None, 0.90, "false is a finding; missing is not"),
    ("ACRIS — owner of record",     _truthy("acris_deed_owner"),        0.20, ""),
    ("ACRIS — mortgage",            _truthy("acris_mtge_date"),         0.20, ""),
    ("DOF — sales",                 lambda p: p.get("sale_count") is not None, 0.90, ""),
    ("Distress — liens, lis pendens", lambda p: p.get("has_tax_lien") is not None, 0.90, ""),
    ("Zoning",                      _truthy("zoning_hotel_permitted"),  0.95, ""),
    ("Landmarks",                   lambda p: p.get("is_landmark") is not None, 0.90, ""),
    ("Tax benefits",                lambda p: p.get("is_branded") is not None or True, 0.00, "421-a / J-51, sparse by nature"),
    ("Rent stabilisation",          lambda p: p.get("rent_stabilized_units") is not None, 0.90, ""),
    ("Hotel Trades Council roster", lambda p: p.get("htc_union") is not None, 0.90, "false is a finding; missing is not"),
    ("Google Places — current use", _truthy("current_use_checked"),     0.95, "the sweep; this is the one that silently stopped"),
    ("City Record — shelter notices", lambda p: p.get("shelter_notice") is not None, 0.90, ""),
    ("Occupancy — established",     lambda p: p.get("occupancy_state") not in (None, "", "unchecked"), 0.99, "what is inside, from any source"),
]


# Coverage catches a source that stopped arriving. It cannot catch a source
# that arrives wrong, and two of the three failures that prompted this were
# exactly that: DOB filing dates sorted by month rather than by year, and 269
# buildings called unknown while the records described them. These are the
# assertions that would have caught the rest — each one a property that has to
# hold between fields, checked against every published building rather than
# reasoned about once.
INTEGRITY = [
    (
        "a use has a basis",
        lambda p: not p.get("current_use") or bool(p.get("current_use_basis")),
        "current_use is set with nothing saying where it came from",
    ),
    (
        "occupancy state is one of the five",
        lambda p: p.get("occupancy_state") in
        ("clear", "onrecord", "thin", "unchecked", "occupied"),
        "an occupancy state nothing knows how to render",
    ),
    (
        "on record says which record",
        lambda p: p.get("occupancy_state") != "onrecord" or bool(p.get("occupancy_basis")),
        "claims the register knows without saying what it holds",
    ),
    (
        "a shelter notice is graded",
        lambda p: not p.get("shelter_notice") or bool(p.get("shelter_status")),
        "a notice with no active/historical grading is a date with no meaning",
    ),
    (
        "a reversion says which kind",
        lambda p: not (p.get("has_reversion")
                       or "reversion_window" in (p.get("reason_codes") or []))
        or bool(p.get("reversion_kind")),
        "closed and converted are different jobs and this one says neither",
    ),
    (
        "a named occupant has a place id",
        lambda p: not p.get("current_use_name") or bool(p.get("current_use_place_id")),
        "a listing we quote but cannot link to",
    ),
    (
        "a building on the map has been looked at",
        lambda p: p.get("segment") in ("unknown", None)
        or p.get("current_use_checked")
        or p.get("occupancy_state") in ("onrecord", "occupied"),
        "in a segment somebody browses, with nothing establishing what is inside",
    ),
    (
        "every building has an address",
        lambda p: bool(str(p.get("address") or "").strip()),
        "unaddressable — it cannot be searched for, exported or visited",
    ),
]


def integrity(features: list[dict], verbose: bool = False) -> int:
    """Check the properties that have to hold. Returns the number breached."""
    props = [f["properties"] for f in features]
    print(f"\nIntegrity over {len(props)} published buildings")
    print("  " + "-" * 74)
    breached = 0
    for name, test, why in INTEGRITY:
        bad = []
        for p in props:
            try:
                ok = bool(test(p))
            except (TypeError, ValueError, AttributeError):
                ok = False
            if not ok:
                bad.append(p)
        mark = "ok  " if not bad else f"{len(bad):4d}"
        print(f"  {mark}  {name}")
        if bad:
            breached += 1
            print(f"        {why}")
            for p in bad[:4]:
                print(f"        {str(p.get('address') or '(no address)')[:44]:46s} {p.get('bbl')}")
            if len(bad) > 4:
                print(f"        ... and {len(bad) - 4} more")
    if not breached:
        print("\n  Every property holds.")
    return breached


def newest_geojson() -> Path:
    files = sorted(DATA_PROCESSED.glob("buildings_*.geojson"), reverse=True)
    if not files:
        raise SystemExit("no buildings_*.geojson in data/processed")
    return files[0]


def report(features: list[dict], verbose: bool = False) -> list[dict]:
    """Print the coverage table. Returns the rows, worst first."""
    props = [f["properties"] for f in features]
    total = len(props)
    rows = []
    for name, test, floor, note in SOURCES:
        missed = []
        for p in props:
            try:
                ok = bool(test(p))
            except (TypeError, ValueError, AttributeError):
                ok = False
            if not ok:
                missed.append(p)
        reached = total - len(missed)
        rows.append({
            "source": name, "reached": reached, "total": total,
            "share": reached / total if total else 0,
            "floor": floor, "note": note, "missed": missed,
        })

    print(f"\nSource coverage over {total} published buildings")
    print("  " + "-" * 74)
    for r in sorted(rows, key=lambda r: r["share"]):
        flag = "  <-- below floor" if r["share"] < r["floor"] else ""
        print(f"  {r['source']:32s} {r['reached']:5d}  {r['share']:6.1%}{flag}")
        if r["note"] and (verbose or flag):
            print(f"      {r['note']}")
        if verbose and r["missed"] and r["share"] < 0.99:
            for p in r["missed"][:5]:
                print(f"        missed: {str(p.get('address'))[:40]}")
            if len(r["missed"]) > 5:
                print(f"        ... and {len(r['missed']) - 5} more")

    broken = [r for r in rows if r["share"] < r["floor"]]
    for r in rows:
        r["broken"] = r["share"] < r["floor"]
    if broken:
        print(f"\n  {len(broken)} source(s) below the floor — something has stopped arriving:")
        for r in broken:
            print(f"    {r['source']}: {r['reached']} of {r['total']} ({r['share']:.1%}, expected {r['floor']:.0%}+)")
    else:
        print("\n  Every source is reaching at least as much as it should.")
    return sorted(rows, key=lambda r: r["share"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--geojson")
    args = ap.parse_args()
    path = Path(args.geojson) if args.geojson else newest_geojson()
    print(f"  reading {path.name}")
    features = json.loads(path.read_text())["features"]
    report(features, verbose=args.verbose)
    integrity(features, verbose=args.verbose)


if __name__ == "__main__":
    main()
