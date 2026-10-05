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


# --- a closure outranks a listing --------------------------------------------
#
# Google Places keeps serving a hotel listing long after the hotel shuts, and
# three of the four clauses that decide "somebody is operating here" trace to
# that one sweep. The Court closed in 2020, its listing still names it, and it
# shipped in "Transient, hotel" -- out of the sourcing segment, and visible on
# the map only because the Reversion overlay reaches past the segment filter.
# These three guard the rule that fixed it, and the exception that keeps Row
# NYC where it is.

def test_a_recorded_closure_is_not_an_operating_hotel(by_bbl):
    """The general rule, so the next stale listing fails here too.

    Named buildings are checked below; this one has no names in it on
    purpose. A curated closure year and no live DCWP licence is the whole
    test, and a building that arrives tomorrow meeting it will be caught
    without anybody remembering to add a row.
    """
    from src.build_geojson import LIVE_LICENCE_STATUSES, POST_2021_REVERSIONS

    failures = []
    for bbl, entry in POST_2021_REVERSIONS.items():
        p = by_bbl.get(bbl)
        if not p or not entry.get("closure_year"):
            continue
        if (p.get("hotel_license_status") or "") in LIVE_LICENCE_STATUSES:
            continue
        if p.get("segment") == "active_hotel":
            failures.append(
                f"{bbl} {p.get('address')}: closed {entry['closure_year']}, no live "
                f"licence, still active_hotel on {p.get('operator_name') or 'no operator name'}"
            )
    assert not failures, (
        "closures reported as operating hotels:\n  " + "\n  ".join(failures))


def test_the_closure_cases_tier_where_the_research_says(by_bbl):
    """The three buildings the rule was written for, and what each must be.

    Row NYC is the one that must NOT move. It closed in Aug 2025 and holds an
    Active DCWP licence, so the city still recognises somebody as entitled to
    run it and its own entry says it may reopen. Dropping licensure from this
    test was measured once and moved 34 buildings into available capacity,
    among them the Ritz-Carlton Central Park -- the exception is the lesson
    from that, and it is worth a test of its own because it looks like an
    oversight.
    """
    expected = {
        "1008947505": ("transient", "130 East 39th -- The Court / St. Giles"),
        "1001060017": ("transient", "320 Pearl -- Hampton Inn Seaport"),
        "1010167501": ("active_hotel", "700 8th Ave -- Row NYC, Active licence"),
    }
    missing = [f"{bbl} ({what})" for bbl, (_, what) in expected.items()
               if bbl not in by_bbl]
    assert not missing, f"closure cases dropped from the build: {missing}"

    wrong = [
        f"{bbl} ({what}): expected {segment}, got {by_bbl[bbl].get('segment')}"
        for bbl, (segment, what) in expected.items()
        if by_bbl[bbl].get("segment") != segment
    ]
    assert not wrong, "closure cases tiered wrong:\n  " + "\n  ".join(wrong)


def test_a_vetoed_listing_says_the_sources_disagree(by_bbl):
    """Re-tiering is half of it; the score has to hear about it too.

    current_use_conflict is -35, the largest single term, and the panel shows
    it as a reason code. A building moved out of active_hotel while still
    presenting an unchallenged "Hotel" label would be quieter than before the
    fix, not louder.
    """
    from src.build_geojson import LIVE_LICENCE_STATUSES, POST_2021_REVERSIONS

    failures = []
    for bbl, entry in POST_2021_REVERSIONS.items():
        p = by_bbl.get(bbl)
        if not p or not entry.get("closure_year"):
            continue
        if (p.get("hotel_license_status") or "") in LIVE_LICENCE_STATUSES:
            continue
        if p.get("current_use") == "hotel" and not p.get("current_use_conflict"):
            failures.append(f"{bbl} {p.get('address')}: reads Hotel against a recorded closure")
    assert not failures, (
        "closures the score never heard about:\n  " + "\n  ".join(failures))


def test_a_shelter_in_the_research_reaches_the_field(by_bbl):
    """The claim has to be a field, or no control can act on it.

    The Court reads "Currently migrant shelter" in its note and shipped with
    shelter_status empty, so Hide active shelters could not reach it. The note
    is prose a person writes; the grade beside it is what the filter reads.
    """
    from src.build_geojson import POST_2021_REVERSIONS

    failures = []
    for bbl, entry in POST_2021_REVERSIONS.items():
        shelter = entry.get("shelter")
        p = by_bbl.get(bbl)
        if not shelter or not p:
            continue
        if p.get("shelter_status") != shelter:
            failures.append(
                f"{bbl} {p.get('address')}: research says {shelter}, "
                f"field says {p.get('shelter_status') or 'nothing'}")
        if not p.get("shelter_status_basis"):
            failures.append(f"{bbl} {p.get('address')}: graded {shelter} and cites nothing")
    assert not failures, "shelter research that never reached a field:\n  " + "\n  ".join(failures)

    # The two the control has to catch, and the one it must leave alone.
    assert by_bbl["1008947505"].get("shelter_status") == "active", "The Court"
    assert by_bbl["1008060076"].get("shelter_status") == "active", "371 7th Ave"
    assert by_bbl["1010167501"].get("shelter_status") == "historical", "Row NYC"


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
    # coo_parsed is the one source CI cannot refresh: DOB BIS 403s everything
    # automated and only a headed browser gets through, so the certificates
    # are fetched by hand and the parsed output committed. See the invariant
    # in tests/test_invariants.py for why that is physics, not neglect.
    manual = [k for k, v in sources.items()
              if not v["refreshed_by_ci"] and k != "coo_parsed"]
    assert manual == [], \
        f"sources nothing on a schedule refreshes: {manual}"
    assert sources["google_current_use"]["pulled_on"], \
        "the current-use sweep reports no pull date"


def test_the_tier_split_is_sane(by_bbl):
    """Both tiers are real, and evidence outnumbers suggestion.

    This used to assert partial was the bulk, which was true while partial
    held 1,540 buildings whose only claim was the class code on the lot. Those
    were dropped on 1 Oct 2026 and partial fell to a few hundred, so the
    assertion now runs the other way — and that inversion is the point of the
    change rather than a regression against it.
    """
    from collections import Counter
    counts = Counter(p["tier"] for p in by_bbl.values())
    assert counts[TIER_LEGAL_TRANSIENT] > counts[TIER_PARTIAL], counts
    assert counts[TIER_PARTIAL] > 50, counts
    assert counts[TIER_LEGAL_TRANSIENT] > 100, counts
    assert 500 < len(by_bbl) < 50000, f"{len(by_bbl)} buildings — out of range"


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
    # A count, not a proportion, and the population it was sized against has
    # more than halved. What it is really checking is that these are published
    # at all rather than dropped for want of a shape to draw, so it asks for
    # some rather than for twenty.
    assert len(nofp) > 5, f"only {len(nofp)} footprint-less buildings — the injection has broken"
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


def test_no_curated_building_is_dropped(by_bbl):
    """Every researched building survives every filter. The negatives do not.

    Two drops run in the build and they disagreed about what counts as
    researched: the scope drop kept a building whose prior operator somebody
    had written down, and the class-code drop then deleted it. 20 Broad Street
    went that way — Sonder's first NYC building, cited here to Tribeca Citizen,
    classed D9 by PLUTO with no Class B rooms, so every automatic test of
    transient use says no and the one human record says yes.

    The three negatives are the other half: they are in this file precisely
    because they should not reach the map, so their absence is the assertion.
    """
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(
        (Path(__file__).resolve().parents[1] / "ground_truth.csv").open()))
    assert rows, "ground_truth.csv is empty"

    missing, leaked = [], []
    for row in rows:
        bbl = (row.get("bbl") or "").strip()
        if not bbl:
            continue
        present = bbl in by_bbl
        if row["label_type"] == "negative":
            if present:
                leaked.append(f"{row['name']} ({bbl})")
        elif not present:
            missing.append(f"{row['name']} ({bbl})")

    assert not missing, f"curated buildings dropped from the build: {missing}"
    assert not leaked, f"negative examples reached the map: {leaked}"
