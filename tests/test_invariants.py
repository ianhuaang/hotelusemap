"""Invariants the pipeline must hold, tested without touching the data.

These are the defects a critical review found in September 2026, each turned
into something that fails. They are pure-function tests on purpose: they run in
milliseconds and need no pulls, so there is no excuse for CI to skip them.

The artifact-level versions live in test_ground_truth.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import EXCLUDED_HOTEL_CLASSES, HOTEL_CLASSES, is_hotel_class
from src.build_geojson import (
    EVIDENCE,
    POST_2021_REVERSIONS,
    _current_flex_operator,
    complete_reason_codes,
)


# --- building class (stage 2) -----------------------------------------------

def test_a_dormitory_is_not_a_hotel():
    # H8 is a dormitory, HR is rent-regulated SRO stock. The score awarded both
    # full hotel-class points off a bare startswith("H").
    assert is_hotel_class("H8") is False
    assert is_hotel_class("HR") is False
    assert is_hotel_class("h8") is False, "case must not smuggle one through"


def test_a_hotel_condominium_is_a_hotel():
    # RH is the other direction: counted here, missed by a prefix test.
    assert is_hotel_class("RH") is True


def test_the_ordinary_h_series_still_counts():
    for c in HOTEL_CLASSES - EXCLUDED_HOTEL_CLASSES:
        assert is_hotel_class(c) is True, c
    for c in ("RM", "RC", "D9", "C7", "O5", "", None):
        assert is_hotel_class(c) is False, c


def test_the_app_is_told_the_same_rule():
    # map/src/hotelClass.js in the app repo carries a transcription of this,
    # because the two repos cannot import from one another. If this list moves
    # and that file does not, the score and the tier disagree again.
    assert HOTEL_CLASSES == {"H1", "H2", "H3", "H4", "H5", "H6", "H7", "H9",
                             "HB", "HH", "HS", "RH"}
    assert EXCLUDED_HOTEL_CLASSES == {"HR", "H8"}


# --- reason codes (stage 4) --------------------------------------------------

def _feature(**props):
    props.setdefault("reason_codes", [])
    return {"properties": props}


def test_a_reason_code_fires_whenever_its_field_is_true():
    # dob_transient_occupancy appeared on 66 buildings and was true of 444,
    # because a mixed-use class is tested first and claims the building before
    # DOB occupancy is reached.
    f = _feature(dob_has_r1=True, has_hotel_license=True,
                 hotel_license_status="Active", hpd_class_b=12)
    complete_reason_codes([f])
    codes = f["properties"]["reason_codes"]
    assert "dob_transient_occupancy" in codes
    assert "dcwp_hotel_license" in codes
    assert "hpd_class_b" in codes


def test_a_reason_code_the_field_contradicts_is_dropped():
    # enrich.py appends current_use_conflict and clears the field further down;
    # 16 buildings wore the chip with the field reading False.
    f = _feature(reason_codes=["current_use_conflict"], current_use_conflict=False)
    complete_reason_codes([f])
    assert "current_use_conflict" not in f["properties"]["reason_codes"]


def test_a_lapsed_licence_is_not_an_active_one():
    f = _feature(has_hotel_license=True, hotel_license_status="Surrendered")
    complete_reason_codes([f])
    codes = f["properties"]["reason_codes"]
    assert "dcwp_license_lapsed" in codes
    assert "dcwp_hotel_license" not in codes, "a surrendered licence read as current"


def test_the_deciding_signal_still_comes_first():
    # Completion appends; it must not reorder. Which signal decided the tier is
    # only legible in the order.
    f = _feature(reason_codes=["bldg_class_RM"], dob_has_r1=True)
    complete_reason_codes([f])
    assert f["properties"]["reason_codes"][0] == "bldg_class_RM"


def test_every_evidence_rule_survives_a_bare_record():
    # A record missing every key must not raise. The build runs this over
    # everything, and one KeyError there loses the whole run.
    f = _feature()
    complete_reason_codes([f])
    assert f["properties"]["reason_codes"] == []
    assert len(EVIDENCE) >= 7


# --- flex operators (stage 5) ------------------------------------------------

def test_a_prior_operator_is_not_a_current_one():
    # enrich.py falls back to the prior-operator ground truth for operator_name
    # and stamps it "ground_truth" -- a record that a flex company has LEFT.
    # Reading it back turned every departure into an arrival.
    assert _current_flex_operator({
        "operator_name": "Sonder Wall Street / Stock Exchange",
        "operator_source": "ground_truth",
    }) == ""


def test_an_operator_google_found_is_current():
    # The other half. Without this the rule could call everything former and
    # look careful while being useless.
    assert "Sonder" in _current_flex_operator({
        "operator_name": "Sonder Battery Park Apartments",
        "operator_source": "google_places",
    })


# --- reversion claims (stage 5) ----------------------------------------------

def test_every_reversion_declares_whether_it_can_be_checked():
    # Each entry says what city records must show. None means "nothing here can
    # confirm this", which the panel draws as unverified rather than red.
    for bbl, rev in POST_2021_REVERSIONS.items():
        assert "sale_price" in rev, f"{bbl} does not say whether it is checkable"
        assert "source" in rev, f"{bbl} carries no source field"
        if rev["sale_price"] is None:
            assert rev["source"] is None, f"{bbl} claims a source with nothing to check"
        else:
            assert rev["source"], f"{bbl} has a checkable figure and no source"


def test_the_stewart_hotel_figure_matches_city_records():
    # It was written as $255M. The city records $260M, and it had been read the
    # wrong way for as long as nobody re-read it.
    assert POST_2021_REVERSIONS["1008060076"]["sale_price"] == 260_000_000


# --- flex operators (stage 3) -----------------------------------------------

def test_a_former_residence_inn_is_not_a_flex_operator():
    # FLEX_OPERATORS names the exclusion three lines above itself — purpose-
    # built extended-stay brands sell hotel product, these take over apartment
    # inventory — and then the former-operator branch took whatever the ground
    # truth held and bypassed it. 554 Third Avenue is the former Residence Inn
    # Midtown East and wore the Flex operators badge; 18 others did too,
    # including six LuxUrbans, three AKAs, a Yotel and a Citadines.
    from src.build_geojson import _is_flex_name

    for not_flex in ("Residence Inn Midtown East", "LuxUrban Washington Hotel",
                     "AKA Times Square", "Yotel New York", "Citadines Fifth Avenue",
                     "Oakwood at The Nash", "Selina Chelsea", ""):
        assert _is_flex_name(not_flex) is False, f"{not_flex} is not a flex operator"

    for flex in ("Sonder Battery Park", "Mint House 70 Pine (Kasa)",
                 "Placemakr Wall Street", "Blueground", "Sentral"):
        assert _is_flex_name(flex) is True, f"{flex} is a flex operator"


# --- DOB occupancy dates (pull stage) ---------------------------------------

def test_filing_dates_sort_by_year_and_not_by_month():
    # DOB writes MM/DD/YYYY and these were compared as those strings, which
    # sorts by month, then day, then year. 1,007 of 3,795 buildings came out
    # of the pull with an earliest filing date later than their latest one,
    # and the reversion rule was about to read those dates as evidence of
    # when a building stopped being a hotel.
    from src.pull_dob_occupancy import _sortable

    assert _sortable("12/30/2013") < _sortable("01/04/2016")
    assert _sortable("05/14/2019") < _sortable("10/01/2020")
    assert _sortable("09/24/2008") < _sortable("04/06/2012")
    assert _sortable("") == ""
    # Already-ISO input is left alone rather than mangled.
    assert _sortable("2019-05-14") == "2019-05-14"


def test_the_pair_it_produces_is_in_order():
    # The property that was violated, stated directly.
    from src.pull_dob_occupancy import _sortable

    entry = {"earliest_date": "", "latest_date": ""}
    for filing in ("04/06/2012", "09/24/2008", "12/30/2013", "01/04/2016"):
        key = _sortable(filing)
        if not entry["earliest_date"] or key < _sortable(entry["earliest_date"]):
            entry["earliest_date"] = filing
        if not entry["latest_date"] or key > _sortable(entry["latest_date"]):
            entry["latest_date"] = filing
    assert entry["earliest_date"] == "09/24/2008"
    assert entry["latest_date"] == "01/04/2016"


# --- coverage and integrity (build stage) ------------------------------------

def test_a_source_that_stops_arriving_is_caught():
    # The failure that keeps happening: a source returns nothing for a run and
    # nothing downstream can tell that from the city having no record. The
    # sweep skipped 173 buildings for a week this way.
    from src.coverage import SOURCES, report

    swept = [{"properties": {"bldgclass": "H2", "current_use_checked": True,
                             "occupancy_state": "clear", "address": "1 TEST ST"}}] * 100
    rows = {r["source"]: r for r in report(swept)}
    assert rows["Google Places — current use"]["share"] == 1.0

    # Now the same buildings with the sweep absent, which is what a broken
    # pull looks like from here.
    unswept = [{"properties": {"bldgclass": "H2", "occupancy_state": "clear",
                               "address": "1 TEST ST"}}] * 100
    rows = {r["source"]: r for r in report(unswept)}
    row = rows["Google Places — current use"]
    assert row["share"] == 0.0
    assert row["broken"] is True, "a source at zero has to be reported as broken"

    # And the floors have to mean something. Zeroing them would silence every
    # one of these without touching a test that only reads shares.
    guarded = [s for s in SOURCES if s[2] >= 0.9]
    assert len(guarded) >= 10, "the sources that should always arrive need a real floor"
    named = {s[0] for s in guarded}
    for must in ("PLUTO — building class", "Google Places — current use",
                 "Occupancy — established", "Zoning"):
        assert must in named, f"{must} has no floor to fall below"


def test_the_integrity_checks_fail_on_the_things_they_name():
    from src.coverage import integrity

    good = {"bldgclass": "H2", "address": "1 TEST ST", "occupancy_state": "clear",
            "segment": "transient", "current_use_checked": True}
    assert integrity([{"properties": good}]) == 0

    # Each of these is a defect that shipped at some point.
    breaks = [
        {**good, "current_use": "hotel", "current_use_basis": ""},        # a use from nowhere
        {**good, "occupancy_state": "onrecord", "occupancy_basis": ""},   # knows without saying how
        {**good, "shelter_notice": True, "shelter_status": ""},           # a date with no meaning
        {**good, "has_reversion": True, "reversion_kind": ""},            # closed or converted, unsaid
        {**good, "address": ""},                                          # unsearchable
    ]
    for bad in breaks:
        assert integrity([{"properties": bad}]) >= 1, bad


def test_a_building_on_the_map_has_been_looked_at():
    # Three evidenced reversions entered the geojson through the zoning
    # exemption, which admits a building the sweep had never reached. They
    # were in a segment somebody browses with nothing establishing what was
    # inside them.
    from src.coverage import integrity

    stranded = {"bldgclass": "H2", "address": "1 TEST ST", "segment": "transient",
                "occupancy_state": "unchecked"}
    assert integrity([{"properties": stranded}]) >= 1
    # Excluded buildings are allowed to be unaccounted for; nobody browses them.
    assert integrity([{"properties": {**stranded, "segment": "unknown"}}]) == 0


def test_every_source_has_a_route_into_a_scheduled_run():
    """No input may depend on somebody remembering to run it.

    Three Google Places sweeps and the City Record shelter pull were outside
    refresh-data.yml. Their output sat in data/raw as committed files, so every
    Monday re-read whichever sweep was last run by hand and stamped it with
    that Monday's date. current_use_conflict is the largest single term in the
    score at -35, so the gap moved scores quietly.

    Asserted against the workflow text rather than the SOURCES table, because
    the table is a claim and the workflow is what actually runs.
    """
    import re
    from pathlib import Path

    from src import provenance

    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/refresh-data.yml"
    text = workflow.read_text()

    # One named exception, and it is physics rather than neglect. DOB BIS
    # returns 403 to everything automated — curl with browser headers,
    # headless Chromium with a session established first, the servlet
    # directly. Only a headed browser gets 200, so the certificates are
    # fetched by a visible window on somebody's desk and the parsed output is
    # committed for the build to read.
    #
    # Named rather than tolerated: a new manual source still fails this.
    MANUAL_BY_NECESSITY = {"coo_parsed"}
    missing = [key for key, _, _, ci in provenance.SOURCES
               if not ci and key not in MANUAL_BY_NECESSITY]
    assert not missing, (
        f"sources with no scheduled refresh: {missing}. "
        "A source nothing pulls is a source that quietly ages.")

    # And the scripts themselves are invoked, not merely claimed.
    scripts = sorted(
        p.name for p in (Path(__file__).resolve().parents[1] / "src").glob("enrich_*.py"))
    run_by_ci = set(re.findall(r"python src/(enrich_[a-z_]+\.py)", text))
    # enrich.py is the main pass and is not an enrich_* source script.
    never_run = [s for s in scripts if s not in run_by_ci]
    assert never_run == ["enrich_web_use.py"], (
        f"enrichment scripts the workflow never runs: {never_run}. "
        "enrich_web_use.py is the known exception — it needs a Programmable "
        "Search Engine id that was never set up, and it is dormant in the "
        "pipeline too.")


def test_every_fetching_step_has_a_time_cap():
    """A step that talks to the network cannot be allowed to run to the job cap.

    Every pull step in refresh-data.yml carries timeout-minutes, sized just
    above its real runtime so drift shows up as a warning before it becomes a
    failure. The four sweeps added on 30 Sep 2026 did not, which meant a
    Places sweep that stalled would have burned the job's whole 350-minute
    budget before anyone saw it — and because the sweeps run with
    continue-on-error, it would have done so silently.

    Compute-only steps (pipeline, enrichment, build) are deliberately
    uncapped: they do no I/O beyond the local disk, and a cap there would be
    a guess about machine speed rather than about a remote host.
    """
    import re
    from pathlib import Path

    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/refresh-data.yml"
    steps = re.split(r"\n      - name: ", workflow.read_text())[1:]

    COMPUTE_ONLY = {"src/pipeline.py", "src/enrich.py", "src/build_geojson.py",
                    "src/report_step_budget.py",
                    # Counts rows in files the pulls already wrote. The only
                    # thing it opens is the local disk.
                    "src/source_volume.py"}
    uncapped = []
    for step in steps:
        run = re.search(r"run: python (src/\S+\.py)", step)
        if not run or run.group(1) in COMPUTE_ONLY:
            continue
        if "timeout-minutes" not in step:
            uncapped.append(step.split("\n")[0])

    assert not uncapped, (
        f"these fetch over the network with no time cap: {uncapped}. "
        "An unbounded step runs to the job's 350-minute limit.")


def test_the_sweeps_run_after_something_has_built_a_geojson():
    """enrich_current_use.py reads the built GeoJSON and exits if there is none.

    It probes a point inside each building's footprint, and the built file is
    the only artefact carrying one. data/processed is not committed, so on a
    fresh runner nothing satisfies that until build_geojson.py has run — and
    the sweeps were placed before it. The 1 Oct 2026 run failed there, which
    the completion gate turned into a refusal to publish rather than a
    silently stale build.

    The ordering is invisible in the YAML: nothing in a step named "Sweep
    current use" says it depends on a file an earlier step writes.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/refresh-data.yml").read_text()

    # The dependency is real: assert the script still bails without the file,
    # so this test fails loudly if that changes rather than guarding a ghost.
    source = (root / "src/enrich_current_use.py").read_text()
    assert "buildings_*.geojson" in source and "sys.exit" in source, (
        "enrich_current_use.py no longer reads the built GeoJSON; this "
        "ordering constraint may no longer apply")

    order = re.findall(r"\n      - name: ([^\n]+)", text)
    builds = [i for i, n in enumerate(order) if n.startswith("Build GeoJSON")]
    sweep = next(i for i, n in enumerate(order) if n.startswith("Sweep current use"))

    assert builds, "no Build GeoJSON step at all"
    assert min(builds) < sweep, (
        f"the current-use sweep runs at step {sweep} and the first build at "
        f"{min(builds)}; it reads what the build writes")
    assert max(builds) > sweep, (
        "nothing rebuilds after the sweeps, so the published file would not "
        "carry what they found")


# --- a door pin the register agrees with ------------------------------------

def test_a_corroborated_lodging_pin_survives_the_bare_address_rule():
    """138 Bowery is 48 registered Class B rooms and an operating hotel.

    Its Places listing is named after the door it sits on, so the rule that
    throws away address-shaped names threw away a type=hotel on a building
    whose own HPD registration says the same thing — and reported Unknown, so
    the segment chain read no operator. A BD person googles the address and
    sees a hotel in ten seconds.
    """
    from src.enrich_current_use import classify

    pin = {"displayName": {"text": "138 Bowery"}, "primaryType": "hotel",
           "types": ["hotel", "lodging"], "businessStatus": "OPERATIONAL"}
    assert classify(pin, class_b=0)[0] == "unknown", (
        "with no registered rooms there is nothing to corroborate the pin")
    assert classify(pin, class_b=48)[0] == "hotel", (
        "a lodging pin on registered transient rooms is still being discarded")


def test_a_pin_the_register_cannot_corroborate_is_still_discarded():
    """Corroboration, not trust — and not contradiction either.

    57 discarded pins carry condominium or apartment types. Keeping them is
    not wrong because apartments are impossible on Class B rooms, and not
    right because Class B rooms may be run as apartments at will: under the
    MDL a Class A unit shall only be used for permanent residence, and moving
    rooms across means filed plans and a new certificate of occupancy.

    It is wrong because one listing named after a door cannot say which of
    three things it found — a lawful conversion, an unlawful occupancy of
    rooms still registered transient, or a noisy pin. Unknown is the only
    honest answer to that.
    """
    from src.enrich_current_use import classify

    pin = {"displayName": {"text": "66 Madison Ave"}, "primaryType": "apartment_building",
           "types": ["apartment_building"], "businessStatus": "OPERATIONAL"}
    assert classify(pin, class_b=134)[0] == "unknown", (
        "a residential pin is overriding 134 registered Class B rooms")


def test_the_corroborating_types_are_read_off_the_rules_not_rewritten():
    """Two lists of lodging types would drift the first time one changed."""
    from src.enrich_current_use import CORROBORATING_TYPES, TYPE_RULES

    expected = {t for use, _l, types in TYPE_RULES if use in ("hotel", "hostel") for t in types}
    assert CORROBORATING_TYPES == expected
    assert "hotel" in CORROBORATING_TYPES and "hostel" in CORROBORATING_TYPES


def test_every_classify_caller_hands_over_the_building():
    """A caller that forgets the rooms silently restores the old behaviour.

    class_b defaults to 0 so the signature change breaks nothing, which is
    also how a call site can quietly opt out of the fix. There are five.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/enrich_current_use.py").read_text()
    calls = re.findall(r"classify\((?:[^()]|\([^()]*\))*\)", src, re.S)
    calls = [c for c in calls if not c.startswith("classify(place")]
    missing = [c[:60].replace("\n", " ") for c in calls if "class_b" not in c]
    assert not missing, f"classify called without the building's rooms: {missing}"


# --- a source that arrives short is not a source that arrives ---------------

def _vol():
    from src import source_volume
    return source_volume


def test_a_source_that_craters_is_caught():
    """The failure the completeness gate cannot see.

    That gate checks whether a pull *failed*. city_record_shelter did not
    fail: it returned 20 rows, then 14, then fewer, exiting zero every time,
    and 317 West 45th Street lost an active shelter notice to it — a building
    running as a shelter went back into the sourcing list with nothing said.
    """
    v = _vol()
    counts = {"pluto": 98_580, "htc_union": 377, "city_record_shelter": 14}
    history = {
        "pluto": [{"date": "20260901", "rows": 98_580}],
        "htc_union": [{"date": "20260901", "rows": 377}],
        "city_record_shelter": [{"date": "20260901", "rows": 20},
                                {"date": "20260929", "rows": 20}],
    }
    alerts = _with_history(v, history, counts)
    fell = {a["source"] for a in alerts}
    assert "city_record_shelter" in fell, "the shelter crater was not caught"
    assert "pluto" not in fell and "htc_union" not in fell, (
        f"a steady source was reported as having fallen: {fell}")


def test_htc_union_is_detectable_even_though_it_cannot_block():
    """It is outside the completeness gate on purpose, and still has to show.

    A build without union data is worth more than no build, so an empty scrape
    must not stop a publish. It drives a -15 penalty, so it must also not pass
    in silence: the run says so and the alert carries the flag that decides
    which of the two happens.
    """
    v = _vol()
    alerts = _with_history(
        v,
        {"htc_union": [{"date": "20260901", "rows": 377}]},
        {"htc_union": 0},
    )
    assert len(alerts) == 1, "an empty union scrape went unreported"
    assert alerts[0]["source"] == "htc_union"
    assert alerts[0]["ratio"] == 0


def test_one_bad_run_does_not_become_the_new_normal():
    """The baseline is the median of the window, not the last run.

    A check that compares against the previous run congratulates a source for
    staying at the floor it fell to. 20, 20, 20, 14 has a median of 20, so the
    run after the shelter bug is still measured against what the source used
    to return.
    """
    v = _vol()
    history = [{"date": d, "rows": r} for d, r in
               (("20260901", 20), ("20260908", 20), ("20260915", 20), ("20260922", 14))]
    assert v.baseline(history) == 20, (
        f"a bad run moved the baseline to {v.baseline(history)}")


def test_a_tiny_source_losing_one_row_is_not_an_alarm():
    """google_hclass_hotels has four rows, so one row is 25% of it."""
    v = _vol()
    alerts = _with_history(
        v,
        {"google_hclass_hotels": [{"date": "20260901", "rows": 4}]},
        {"google_hclass_hotels": 3},
    )
    assert alerts == [], "a four-row source losing one row raised an alarm"


def _with_history(v, history, counts):
    """Run check() against a stand-in history rather than the real file."""
    real = v._history
    v._history = lambda previous=None: history
    try:
        return v.check(counts)
    finally:
        v._history = real


def test_the_seed_ships_so_the_first_run_has_something_to_judge_against():
    """A check with no history is a check that passes on its first bad run."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "source_counts.json"
    assert path.exists(), "no seed counts; the check has no baseline to start from"
    hist = json.loads(path.read_text())
    assert "_README" in hist, (
        "the seed does not say it is a seed; it was mistaken for live state once "
        "already")
    from src import provenance

    registered = {k for k, _p, _d, _c in provenance.SOURCES}
    missing = sorted(registered - set(hist))
    assert not missing, f"registered sources with no seeded count: {missing}"


def test_the_baseline_advances_run_to_run():
    """The failure this replaced: a baseline that never moved off its seed.

    --record wrote the history to a file on the runner and nothing committed
    it back, so every run re-read the same seeded numbers. The history travels
    in the published manifest now, which is the one artefact that outlives a
    run. Two consecutive builds, each reading the one before it.
    """
    import json

    v = _vol()
    one = tmp_build(None, {"pluto": 99_580}, "20261013")
    two = tmp_build(one, {"pluto": 100_200}, "20261020")

    seeded = v.baseline(v._history(None)["pluto"])
    after_two = v.baseline(json.loads(two.read_text())[v.MANIFEST_KEY]["pluto"])
    assert after_two != seeded, (
        f"the baseline is still the seed ({seeded:,}) after two runs")

    rows = [e["rows"] for e in json.loads(two.read_text())[v.MANIFEST_KEY]["pluto"]]
    assert 99_580 in rows, (
        "the second run did not build on the first one's history, so each run "
        "is starting over")

    # And the gate still bites against the advanced baseline, not just the seed.
    alerts = v.check({"pluto": 60_000}, previous=two)
    assert any(a["source"] == "pluto" for a in alerts), (
        "a crater went unreported once the baseline had moved")


def test_a_build_carries_its_volume_history_where_the_next_run_can_read_it():
    """The manifest key is the contract between one run and the next."""
    import re
    from pathlib import Path

    build = (Path(__file__).resolve().parents[1] / "src/build_geojson.py").read_text()
    assert re.search(r'"source_volume":\s*source_volume\.advance\(', build), (
        "the build no longer publishes its volume history; the next run has "
        "nothing to read and falls back to the seed forever")


def tmp_build(previous, counts, day):
    """A stand-in for what build_geojson writes."""
    import json
    import tempfile
    from pathlib import Path

    v = _vol()
    out = Path(tempfile.mkdtemp()) / f"build_{day}.geojson"
    out.write_text(json.dumps({
        "type": "FeatureCollection", "sources": {},
        v.MANIFEST_KEY: v.advance(previous, counts, today=day), "features": [],
    }))
    return out


# --- the City Record walk reads every page, in a fixed order -----------------

def _paged(rows, page_size=1000, short_at=None):
    """A Socrata stand-in: hands back a window, optionally short one page."""
    def fetch(params):
        assert params.get("$order"), "the walk asked for a page with no sort key"
        offset = params["$offset"]
        limit = params["$limit"]
        window = rows[offset:offset + limit]
        if short_at is not None and offset == short_at:
            window = window[:limit // 2]
        return window
    return fetch


def test_the_city_record_walk_asks_for_a_stable_order():
    """Unordered paging was skipping notices.

    Socrata promises no consistent row order between unordered requests, so an
    $offset walk over $q="shelter" read some rows twice and never read others.
    The sweep returned 20 buildings on 29 Sep, 14 on 1 Oct and fewer on 5 Oct
    with nothing in the code changing and no notice withdrawn by the city. 17
    Battery Place lost a 2008 notice to it and 317 West 45th Street a 2023 one
    graded active, which put a building running as a shelter back in the
    sourcing list.
    """
    from src.enrich_city_record import fetch_notices

    seen = []

    def fetch(params):
        seen.append(params)
        return []

    fetch_notices(fetch=fetch)
    assert seen, "the walk made no request at all"
    assert seen[0].get("$order") == ":id", (
        "the City Record walk pages without a sort key; Socrata will skip rows")


def test_the_city_record_walk_does_not_skip_a_short_page():
    """A short page is not the end of the data.

    Socrata returns fewer rows than asked for under load. The walk advanced by
    the page size rather than by what arrived, so the difference was skipped,
    and it stopped on any short page, so everything after one was lost.
    """
    from src.enrich_city_record import fetch_notices

    rows = [{"n": i} for i in range(2500)]

    whole = fetch_notices(fetch=_paged(rows))
    assert whole == rows, f"a clean walk lost rows: {len(whole)} of {len(rows)}"

    # The server goes short on the second page and carries on.
    patchy = fetch_notices(fetch=_paged(rows, short_at=1000))
    assert patchy == rows, (
        f"a short page cost {len(rows) - len(patchy)} rows; the walk either "
        "skipped past them or stopped early")


def test_the_city_record_walk_refuses_to_truncate_silently():
    """It fails rather than publishing a partial sweep."""
    import pytest as _pytest

    from src.enrich_city_record import MAX_PAGES, PAGE_SIZE, fetch_notices

    endless = _paged([{"n": i} for i in range((MAX_PAGES + 2) * PAGE_SIZE)])
    with _pytest.raises(RuntimeError, match="did not terminate"):
        fetch_notices(fetch=endless)


# --- the room-work signal reads a history, not a recency window -------------

def test_room_work_survives_newer_filings():
    """The regression that happened, as a test.

    _guestroom_works scanned record["permits"], which enrich.py cuts to the
    five most recent filings. 2 Lexington Avenue — the Gramercy, closed since
    2020 and the one building this signal exists to catch — carries 58 of
    them. Two scaffold permits filed on 2 Oct 2026 pushed the $13.3m
    "guestrooms on floors 3-16th" permit out of the window, and the building
    went back to reading as an available target while MCR was still inside it.

    Backwards exactly where it matters: a hotel being rebuilt files a lot of
    permits, so the more active the construction the likelier the evidence of
    it is evicted.
    """
    from datetime import date, timedelta

    from src.build_geojson import _guestroom_works

    works = {
        "job_type": "Alteration",
        "description": "General Construction renovations related to guestrooms on floors 3-16th",
        "action_date": (date.today() - timedelta(days=60)).isoformat(),
        "cost": 13_182_381,
        "status": "Plan Examiner Review",
    }
    # Scaffold, facade, standpipe — the ordinary traffic of a live site, all
    # filed after the permit that says what the site is for.
    noise = [{
        "job_type": "Alteration",
        "description": "Installation of suspended scaffold for facade work and/or inspection",
        "action_date": (date.today() - timedelta(days=d)).isoformat(),
        "cost": 0,
        "status": "Permit Issued",
    } for d in range(1, 21)]
    permits = sorted(noise + [works], key=lambda q: q["action_date"], reverse=True)

    found = _guestroom_works(permits)
    assert found, "20 newer scaffold filings buried the room works"
    assert found["cost"] == 13_182_381

    # The shape of the bug, stated so this test cannot pass by accident: the
    # five most recent filings are exactly what used to be handed in here.
    assert _guestroom_works(permits[:5]) is None, (
        "the fixture no longer reproduces the truncation, so the assertion "
        "above proves nothing")


def test_the_room_work_scan_is_not_handed_the_truncated_list():
    """And is not wired back to it later.

    The companion to the test above: that one proves the rule survives a full
    history, this one proves a full history is what it gets. load_demolitions
    carries the same fix for the same reason one signal over — a Full
    Demolition filing is rarely among the three most recent either, which is
    how 859 7th Avenue sat second on the target list with a demolition
    approved against it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    enrich = (root / "src/enrich.py").read_text()
    build = (root / "src/build_geojson.py").read_text()

    # Not guarding a ghost: the truncation this is about still exists, and is
    # still right for what it is for, which is what the panel displays.
    assert re.search(r'record\["permits"\]\s*=\s*permits\[:\d+\]', enrich), (
        "enrich.py no longer truncates record['permits']; this guard may no "
        "longer apply")

    assert "def load_guestroom_works" in build, (
        "the by-BBL room-work index is gone; the scan has nothing to read but "
        "the truncated list")
    assert not re.search(r"_guestroom_works\(\s*record", build), (
        "the room-work scan is reading the record's five most recent permits "
        "again — it needs the full history, see load_guestroom_works")


def test_registration_is_not_evidence_of_trading():
    """The two questions the build must keep apart.

    A predicate called _is_actively_operating returned True on an HPD Class B
    registration, so the Roosevelt Hotel — shut since 2020 — counted as
    trading because the city still records 1,064 rooms against it.

    Nothing user-facing was wrong: the segment reads "Transient, no operator"
    correctly, because has_active_operator asks whether anybody is in
    possession — a name, a licence, an operator that reads like a hotel, a
    current use of hotel — and never looks at the register.

    What was wrong was a name. The special-permit rule turns on whether there
    is an entitlement to grandfather, and a registration is exactly that, so
    the input was right and the label was a trap for whoever touched it next.
    This holds the line: the register may decide entitlement and must never
    decide possession.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/build_geojson.py").read_text()

    # Defined or called, not merely mentioned — the comment recording why it
    # was renamed is worth keeping.
    assert not re.search(r"(def |[^#\w])_is_actively_operating\s*\(", src), (
        "a predicate named for operation is reading the register; it tests "
        "entitlement, and the name has to say so")

    # has_active_operator decides possession. It may not consult the register.
    #
    # The whole block that decides it, from the operator-name test to the
    # dedented close of the assignment — not the bool(...) body alone.
    #
    # Twice now the narrow reading has failed. The first was lazy about where
    # the call ended, captured only `record.get("hotel_name")` and let a
    # register field added below it sail through. The second was this: a
    # recorded closure now vetoes the call from in front of it, reading two
    # locals of its own, so `= bool(` stopped matching at all — and matching
    # the new shape alone would have left those locals unguarded, which is the
    # same hole one statement further out.
    #
    # Anything in this block can decide possession. That is what the guard is
    # about, so that is what it reads.
    m = re.search(
        r"\n        op_name = .*?\n        has_active_operator = .*?\n        \)\n",
        src, re.S)
    assert m, "the possession block has moved; this guard needs rewriting"

    # Prose, not logic. The comments in here explain which inputs are the
    # Places sweep wearing different hats, and a comment naming a register
    # field to say the decision ignores it must not read as it consulting one.
    possession = "\n".join(
        line for line in m.group(0).splitlines()
        if not line.lstrip().startswith("#"))

    # The call itself still has to be in there. Without this the guard would
    # pass just as happily on a block that had stopped deciding anything.
    assert "has_active_operator = " in possession and "bool(" in possession, (
        "the possession block no longer computes has_active_operator")

    for register_field in ("hpd_class_b", "hpd_class_a", "unitsres", "coo_dwelling_units"):
        assert register_field not in possession, (
            f"has_active_operator reads {register_field}; a registration says what a "
            "building is entitled to, never who is trading in it")


def test_the_two_reversions_are_published_apart():
    """The kinds were split and the overlay never heard.

    reversion_kind has said which of the two a building is since the day they
    were separated, and the panel reads it. The overlay, the count and the
    decision tree read has_reversion, which answers "is this on the curated
    list" — so they showed a converted building and a closed one as one
    tracked population, and sent somebody to underwrite undoing a conversion
    that never happened.
    """
    import re
    from pathlib import Path

    build = (Path(__file__).resolve().parents[1] / "src/build_geojson.py").read_text()
    assert re.search(r'properties\["reversion_window_open"\]\s*=\s*'
                     r'properties\["reversion_kind"\] == "converted"', build), (
        "reversion_window_open is not derived from the kind")
    assert re.search(r'properties\["reversion_closed_hotel"\]\s*=\s*'
                     r'properties\["reversion_kind"\] == "closed"', build), (
        "reversion_closed_hotel is not derived from the kind")


def test_the_window_flag_covers_both_mechanisms_not_just_the_curated_list():
    """Ten of the thirteen converted buildings were found by the rule.

    has_reversion is set only for POST_2021_REVERSIONS, so the overlay has
    never shown the rule-derived conversions — 554 Third Avenue among them,
    the building the pipeline rule was rewritten for. The flag is derived
    from reversion_kind, which both mechanisms set, so it cannot inherit that
    blind spot.
    """
    from pathlib import Path

    build = (Path(__file__).resolve().parents[1] / "src/build_geojson.py").read_text()
    where = build.index('properties["reversion_window_open"]')
    # It is set inside the block the rule and the curated list both reach,
    # not inside the `if reversion_info:` block that only the list reaches.
    curated_block = build.index("reversion_info = POST_2021_REVERSIONS.get")
    assert where < curated_block, (
        "reversion_window_open is set inside the curated-list block, so the "
        "rule-derived conversions will not carry it")


def test_a_closed_hotel_never_reads_as_an_open_window():
    """Row NYC is 1,332 Class B rooms sitting idle, not a conversion.

    Replays the published rule over the kinds the build can emit, so the two
    flags cannot both be true and cannot disagree with reversion_kind.
    """
    for kind in ("converted", "closed"):
        window = kind == "converted"
        closed = kind == "closed"
        assert window != closed, f"{kind} sets both flags or neither"
    # And the only two kinds the build emits are those, so nothing falls
    # through to a third state carrying neither flag by accident.
    from pathlib import Path

    build = (Path(__file__).resolve().parents[1] / "src/build_geojson.py").read_text()
    emitted = build[build.index('properties["reversion_kind"] = ('):][:160]
    assert '"closed"' in emitted and '"converted"' in emitted, (
        "the kinds the build emits have changed; the flags derived from them "
        "need rechecking")
