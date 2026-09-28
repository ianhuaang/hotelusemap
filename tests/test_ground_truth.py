"""Ground-truth and artifact checks against the GeoJSON the build publishes.

Replaces a test_pipeline.py that had not run in a long time: it imported
TIER_CLASS_B and TIER_PRIOR_OPERATOR, which config.py no longer defines, so the
module failed at collection. Nothing noticed, because refresh-data.yml never ran
the tests. Tests that cannot import are worse than no tests -- they look like
coverage on the repo page.

It also called run_pipeline() five times. This reads the built artifact instead,
which is both faster and a better subject: it is what actually ships.
"""

import csv
import glob
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TIER_EXCLUDED, TIER_LEGAL_TRANSIENT, TIER_PARTIAL, is_hotel_class

ROOT = Path(__file__).parent.parent
GROUND_TRUTH = ROOT / "ground_truth.csv"


def _latest_build():
    files = sorted(glob.glob(str(ROOT / "data" / "processed" / "buildings_*.geojson")))
    if not files:
        pytest.skip("no build to check — run python src/build_geojson.py first")
    return json.loads(Path(files[-1]).read_text())


@pytest.fixture(scope="module")
def build():
    return _latest_build()


@pytest.fixture(scope="module")
def by_bbl(build):
    return {f["properties"]["bbl"]: f["properties"] for f in build["features"]}


@pytest.fixture(scope="module")
def labels():
    with open(GROUND_TRUTH) as f:
        return list(csv.DictReader(f))


# --- ground truth ------------------------------------------------------------

def test_eligibility_positives_are_legal_transient(by_bbl, labels, build):
    """Known transient buildings must surface, or say why they did not.

    A positive can legitimately be absent for one reason: the DOB footprint
    file has no geometry for its BBL, and build_geojson drops a record it
    cannot draw. That is a real gap -- 175 buildings, 27 of them legal_transient
    -- but it is a known one. Anything absent for any OTHER reason is a
    regression and fails here.
    """
    dropped_for_geometry = {"1011660035"}   # Hotel Beacon, 320 Class B rooms
    failures = []
    for row in labels:
        if row["label_type"] != "eligibility_positive":
            continue
        got = by_bbl.get(row["bbl"])
        if got is None:
            if row["bbl"] not in dropped_for_geometry:
                failures.append(f"{row['name']} ({row['bbl']}): absent, and not for want of a footprint")
            continue
        if got["tier"] != TIER_LEGAL_TRANSIENT:
            failures.append(f"{row['name']} ({row['bbl']}): tier={got['tier']}, expected {TIER_LEGAL_TRANSIENT}")
    assert not failures, "Eligibility positives regressed:\n  " + "\n  ".join(failures)


def test_negatives_never_reach_legal_transient(by_bbl, labels):
    """A building labelled negative must not be presented as transient capacity.

    Absent is an acceptable answer; `partial` is too, since it claims only that
    something is worth a look. legal_transient is not.
    """
    failures = [
        f"{row['name']} ({row['bbl']}): tier={by_bbl[row['bbl']]['tier']}"
        for row in labels
        if row["label_type"] == "negative"
        and row["bbl"] in by_bbl
        and by_bbl[row["bbl"]]["tier"] == TIER_LEGAL_TRANSIENT
    ]
    assert not failures, "Negatives surfaced as transient:\n  " + "\n  ".join(failures)


def test_prior_operators_keep_their_operator_record(by_bbl, labels):
    """Every labelled prior operator that survives must carry its record."""
    failures = [
        f"{row['name']} ({row['bbl']})"
        for row in labels
        if row["label_type"] == "prior_operator"
        and row["bbl"] in by_bbl
        and not by_bbl[row["bbl"]].get("prior_operator")
    ]
    assert not failures, "Prior operators lost their record:\n  " + "\n  ".join(failures)


# --- the defects the September review found ----------------------------------

def test_no_competing_score_is_published(by_bbl):
    """score_legal drifted from the app twice. It is the app's to compute."""
    for bbl, p in by_bbl.items():
        assert "score_legal" not in p, f"{bbl} carries a second legal score"
        assert "score_quality" not in p, f"{bbl} carries score_quality"


def test_no_excluded_class_is_scored_as_a_hotel(by_bbl):
    for bbl, p in by_bbl.items():
        if p.get("tier") == TIER_EXCLUDED:
            assert not is_hotel_class(p.get("bldgclass")), \
                f"{bbl} ({p.get('bldgclass')}) is excluded yet reads as a hotel class"


def test_reason_codes_agree_with_their_fields(by_bbl):
    """The panel presents these as the evidence on the building."""
    checks = {
        "hpd_class_b": lambda p: (p.get("hpd_class_b") or 0) > 0,
        "bldg_class_hotel": lambda p: is_hotel_class(p.get("bldgclass")),
        "dob_transient_occupancy": lambda p: bool(p.get("dob_has_r1") or p.get("dob_has_j1")),
        "current_use_conflict": lambda p: bool(p.get("current_use_conflict")),
        "dcwp_hotel_license": lambda p: p.get("has_hotel_license")
            and p.get("hotel_license_status") in ("Active", "Ready for Renewal"),
    }
    failures = []
    for bbl, p in by_bbl.items():
        codes = set(p.get("reason_codes") or [])
        for code, test in checks.items():
            if test(p) and code not in codes:
                failures.append(f"{bbl}: has {code} and no code for it")
            elif code in codes and not test(p) and code != "dob_transient_occupancy":
                failures.append(f"{bbl}: claims {code} and the field says otherwise")
    assert not failures, f"{len(failures)} code/field mismatches:\n  " + "\n  ".join(failures[:20])


def test_no_departure_is_reported_as_a_current_operator(by_bbl):
    """operator_source ground_truth records that a flex company LEFT."""
    failures = [
        f"{bbl}: {p.get('flex_op_name')}"
        for bbl, p in by_bbl.items()
        if p.get("flex_op_status") == "current" and p.get("operator_source") == "ground_truth"
    ]
    assert not failures, "Prior operators promoted to current:\n  " + "\n  ".join(failures)


def test_a_reversion_is_red_only_when_a_record_backs_it(by_bbl):
    for bbl, p in by_bbl.items():
        rev = p.get("reversion")
        if not rev:
            continue
        if rev.get("verified"):
            assert rev.get("verified_against"), f"{bbl} claims verified and cites nothing"
            assert rev.get("sale_price"), f"{bbl} claims verified with no checkable figure"
        assert p.get("reversion_unverified") == (not rev.get("verified")), bbl


def test_the_build_says_what_it_read(build):
    """Every source, its pull date, and whether a weekly run refreshes it."""
    sources = build.get("sources")
    assert sources, "the collection publishes no provenance"
    assert sources["pluto"]["pulled_on"], "PLUTO has no pull date"
    manual = [k for k, v in sources.items() if not v["refreshed_by_ci"]]
    assert "google_current_use" in manual, \
        "Places data is refreshed by hand; the manifest must say so"


def test_the_tier_split_is_sane(by_bbl):
    """partial is the bulk; legal_transient a minority; excluded a handful."""
    from collections import Counter
    counts = Counter(p["tier"] for p in by_bbl.values())
    assert counts[TIER_PARTIAL] > counts[TIER_LEGAL_TRANSIENT], counts
    assert counts[TIER_LEGAL_TRANSIENT] > 100, counts
    assert 1000 < len(by_bbl) < 50000, f"{len(by_bbl)} buildings — out of range"
