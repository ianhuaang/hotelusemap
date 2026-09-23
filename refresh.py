"""Master refresh script — re-pulls all data sources, re-runs pipeline, deploys.

Usage:
  python refresh.py          # full refresh (all pulls + pipeline + geojson)
  python refresh.py --quick  # skip pulls, just re-run pipeline + geojson
"""

import subprocess
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC = PROJECT_ROOT / "src"

PULL_SCRIPTS = [
    ("PLUTO", "pull_pluto.py"),
    ("HPD buildings", "pull_hpd.py"),
    ("DOB permits", "pull_permits.py"),
    ("DOF sales", "pull_sales.py"),
    ("Building footprints", "pull_footprints.py"),
    ("Distress signals", "pull_distress.py"),
    ("C of O", "pull_coo.py"),
    ("DCWP hotel licenses", "pull_hotel_licenses.py"),
    ("DOB occupancy", "pull_dob_occupancy.py"),
    ("HPD registrations", "pull_hpd_registrations.py"),
    ("ACRIS owners", "pull_acris_owners.py"),
    ("Alt addresses", "pull_alt_addresses.py"),
    ("LPC landmarks", "pull_landmarks.py"),
    ("Tax benefits", "pull_tax_benefits.py"),
    ("Rent stabilization", "pull_rent_stabilization.py"),
]

PIPELINE_SCRIPTS = [
    ("Pipeline", "pipeline.py"),
    ("Enrich", "enrich.py"),
    ("Build GeoJSON", "build_geojson.py"),
]


def run_script(label, script_name):
    path = SRC / script_name
    if not path.exists():
        print(f"  SKIP {label} — {script_name} not found")
        return False
    print(f"\n{'='*60}")
    print(f"  {label} ({script_name})")
    print(f"{'='*60}")
    start = time.time()
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(PROJECT_ROOT),
    )
    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"  FAILED ({elapsed:.0f}s)")
        return False
    print(f"  Done ({elapsed:.0f}s)")
    return True


# A pull that loses rows reconciles. That is what made the Certificate of
# Occupancy bug dangerous: it paged Socrata on a non-total order, came back
# with 34,126 duplicates out of 121,577 and dropped as many it never saw, and
# the total still matched DOB's own count exactly. 101 buildings scored as
# though they had no certificate for months.
#
# Duplicates are the tell — but only where a row is naturally unique. HPD
# violations are selected without a violation id, so a building with 96 Class B
# violations inspected the same day yields 96 byte-identical rows. That is the
# data, not a fault, and a check that calls it corruption is worse than no
# check: it cries wolf on four datasets every run.
#
# So this reports, and only fails on files whose rows carry enough identity to
# make repetition meaningful. Making it authoritative everywhere means adding
# :id to each pull's $select, which changes the stored schema; worth doing, not
# worth guessing at in the meantime.
DUPLICATE_THRESHOLD = 0.02

# Files whose rows are unique by construction, so duplicates mean lost pages.
KEYED_PULLS = ("coo_", "acris_owners_", "pluto_", "footprints_", "landmarks_")


def check_pull_integrity() -> list[str]:
    """Compare total rows against distinct rows in everything pulled today."""
    import json
    from collections import Counter

    raw = PROJECT_ROOT / "data" / "raw"
    today = date.today().strftime("%Y%m%d")
    problems = []

    print("\n" + "=" * 60)
    print("  PULL INTEGRITY")
    print("=" * 60)

    for path in sorted(raw.glob(f"*_{today}.json")):
        try:
            rows = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{path.name}: unreadable ({e})")
            continue
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        if not isinstance(rows, list) or not rows:
            continue

        counts = Counter(json.dumps(r, sort_keys=True) for r in rows)
        dupes = sum(v - 1 for v in counts.values() if v > 1)
        share = dupes / len(rows)
        keyed = path.name.startswith(KEYED_PULLS)
        fails = keyed and share > DUPLICATE_THRESHOLD
        note = ("  <-- PAGING LOST ROWS" if fails
                else "" if keyed else "  (no unique key — repeats expected)")
        print(f"  {path.name:42} {len(rows):>8} rows, {dupes:>7} repeated ({share:.1%}){note}")
        if fails:
            problems.append(
                f"{path.name}: {dupes:,} duplicates of {len(rows):,} rows ({share:.1%}). "
                "Paging on a non-total order — the file repeats rows and is missing as many."
            )

    if problems:
        print("\n  INTEGRITY FAILURES:")
        for p_ in problems:
            print(f"    {p_}")
    else:
        print("\n  No pull lost rows.")
    return problems


def main():
    quick = "--quick" in sys.argv
    today = date.today().strftime("%Y-%m-%d")

    print(f"NYC Transient Capacity — Refresh ({today})")
    print(f"Mode: {'quick (pipeline only)' if quick else 'full (pulls + pipeline)'}")

    if not quick:
        print("\n" + "="*60)
        print("  DATA PULLS")
        print("="*60)
        failed = []
        for label, script in PULL_SCRIPTS:
            ok = run_script(label, script)
            if not ok:
                failed.append(label)
        if failed:
            print(f"\nWARNING: {len(failed)} pulls failed: {', '.join(failed)}")
            print("Continuing with pipeline using available data...")

        # Before the pipeline reads any of it.
        corrupt = check_pull_integrity()
        if corrupt:
            print("\nERROR: a pull came back incomplete. Building on it would put "
                  "silently wrong numbers in front of the deal team.")
            print("Fix the paging, re-run that pull, then continue.")
            sys.exit(1)

    print("\n" + "="*60)
    print("  PIPELINE")
    print("="*60)
    for label, script in PIPELINE_SCRIPTS:
        ok = run_script(label, script)
        if not ok:
            print(f"\nERROR: {label} failed. Stopping.")
            sys.exit(1)

    # Copy GeoJSON to map/public for local dev
    from config import DATA_PROCESSED
    today_fmt = date.today().strftime("%Y%m%d")
    geojson = DATA_PROCESSED / f"buildings_{today_fmt}.geojson"
    map_public = PROJECT_ROOT / "map" / "public" / "buildings.geojson"
    if geojson.exists():
        import shutil
        shutil.copy2(geojson, map_public)
        print(f"\nCopied GeoJSON to {map_public}")

    print(f"\nRefresh complete ({today})")


if __name__ == "__main__":
    main()
