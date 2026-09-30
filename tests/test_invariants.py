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
