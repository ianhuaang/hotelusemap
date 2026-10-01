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
    # This used to assert the opposite — that the manifest admitted Places
    # data was refreshed by hand — because it was, and saying so was the best
    # available. The three Places sweeps and the City Record pull are in
    # refresh-data.yml now, so the honest assertion is the stronger one: there
    # is nothing left that only a person remembering keeps current.
    manual = [k for k, v in sources.items() if not v["refreshed_by_ci"]]
    assert manual == [], \
        f"sources nothing on a schedule refreshes: {manual}"
    assert sources["google_current_use"]["pulled_on"], \
        "the current-use sweep reports no pull date"


def test_the_tier_split_is_sane(by_bbl):
    """partial is the bulk; legal_transient a minority; excluded a handful."""
    from collections import Counter
    counts = Counter(p["tier"] for p in by_bbl.values())
    assert counts[TIER_PARTIAL] > counts[TIER_LEGAL_TRANSIENT], counts
    assert counts[TIER_LEGAL_TRANSIENT] > 100, counts
    assert 1000 < len(by_bbl) < 50000, f"{len(by_bbl)} buildings — out of range"


# --- free owner contacts -----------------------------------------------------

def test_every_contact_says_where_it_came_from(by_bbl):
    """A mailing address with no source or date is not usable evidence.

    HPD registration and ACRIS are pulled on different days, so a contact
    inherits neither the build date nor the other source's date.
    """
    seen = 0
    for bbl, p in by_bbl.items():
        for c in p.get("owner_contacts") or []:
            seen += 1
            assert c.get("kind") in ("owner", "managing_agent"), f"{bbl}: {c.get('kind')}"
            assert c.get("source"), f"{bbl}: contact with no source"
            assert c.get("pulled_on"), f"{bbl}: contact with no pull date"
            assert c.get("name") or c.get("org"), f"{bbl}: contact with no name at all"
    assert seen > 1000, f"only {seen} contacts — the HPD join has probably broken"


def test_has_free_contact_means_there_is_an_address(by_bbl):
    """The flag the panel branches on, checked against what it claims.

    A name with nowhere to send anything is not a contact. If this drifted, the
    panel would offer "access contact info" and then show a name and no address,
    or claim a paid lookup was needed for a building that has one.
    """
    for bbl, p in by_bbl.items():
        addressed = any(c.get("address") for c in (p.get("owner_contacts") or []))
        assert bool(p.get("has_free_contact")) == addressed, bbl


def test_the_free_sources_cover_most_of_the_map(by_bbl):
    """Coverage is the number that decides whether to pay for the rest.

    84% of buildings carry both an owner and a managing agent, and 11% carry
    neither. A silent regression in the HPD join would show up here as the
    paid-lookup share climbing, and nowhere else.
    """
    covered = sum(1 for p in by_bbl.values() if p.get("has_free_contact"))
    share = covered / len(by_bbl)
    assert share > 0.80, f"only {share:.0%} of buildings have a free contact"


# --- buildings DOB has no footprint for --------------------------------------

def test_a_building_without_a_footprint_is_still_published(by_bbl):
    """They were dropped outright for having no shape to draw.

    35 buildings, 11 of them legal_transient, including Hotel Beacon and its
    320 registered Class B rooms — a building ground_truth.csv names as a
    canonical positive. The map and the table both lost them silently.
    """
    nofp = [p for p in by_bbl.values() if p.get("no_footprint")]
    assert len(nofp) > 20, f"only {len(nofp)} footprint-less buildings — the injection has broken"
    assert any(p.get("tier") == TIER_LEGAL_TRANSIENT for p in nofp)


def test_every_building_says_how_precisely_it_is_located(by_bbl):
    for bbl, p in by_bbl.items():
        prec = p.get("location_precision")
        assert prec in ("footprint", "approximate", "unplaceable"), f"{bbl}: {prec}"
        # The two must agree: a building with a real footprint is not approximate,
        # and one without a footprint must not claim to have one.
        assert (prec == "footprint") != bool(p.get("no_footprint")), bbl


def test_a_pin_is_only_dropped_where_the_geocode_is_trustworthy(build):
    """Geometry matches the claim, feature by feature.

    GeoSearch resolving an address to a neighbouring lot would put a confident
    pin on the wrong building, which is worse than no pin. Anything it could
    not confirm carries a null geometry — valid GeoJSON, invisible to the map,
    still present for the table.
    """
    for f in build["features"]:
        p, geom = f["properties"], f.get("geometry")
        prec = p.get("location_precision")
        if prec == "approximate":
            assert geom and geom.get("type") == "Point", f"{p['bbl']} is approximate with no point"
        elif prec == "unplaceable":
            assert geom is None, f"{p['bbl']} is unplaceable yet carries geometry"
        else:
            assert geom and geom.get("type") in ("Polygon", "MultiPolygon"), p["bbl"]


def test_the_availability_score_matches_its_own_signals(by_bbl):
    """Recomputed from the published fields, every score agrees — and tops out at 100.

    The divisor was 45 while the five terms summed to 40, so 100 was
    unreachable by construction: the best possible was 89 and the highest in
    the set was 62. It is shown on the Independent Hotels tab and exported to
    HubSpot, so a building carrying every distress signal we track went out
    reading as barely available.

    Checked against the artifact rather than by re-running the formula, since
    re-running the formula would pass with the bug in place.
    """
    from datetime import date

    year = date.today().year
    weights = [15, 8, 8, 5, 4]
    ceiling = sum(weights)
    seen_any = 0

    for bbl, p in by_bbl.items():
        fired = [
            bool(p.get("prior_operator")),
            bool(p.get("has_tax_lien")),
            bool(p.get("has_lis_pendens")),
            (p.get("last_sale_date") or "") >= f"{year - 2}-01-01",
            (p.get("ecb_total_balance") or 0) > 10000,
        ]
        raw = sum(w for w, f in zip(weights, fired) if f)
        assert p["score_avail"] == round(raw / ceiling * 100), (
            f"{bbl}: published {p['score_avail']}, signals give "
            f"{round(raw / ceiling * 100)}")
        if raw:
            seen_any += 1

    assert seen_any > 100, "no building fires an availability signal; the check proves nothing"
    assert max(p["score_avail"] for p in by_bbl.values()) <= 100
