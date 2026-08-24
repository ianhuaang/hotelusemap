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
