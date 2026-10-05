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
                    "src/report_step_budget.py"}
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
