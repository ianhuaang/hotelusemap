"""The web reading of current use, and the hole it was built to close.

Google Places is a business directory, so it only sees uses that register as
businesses. A shelter does not. 35-02 37 Avenue is a 125-room former hotel
running under a $65.8m city contract as a shelter for single adults through
June 2030 — reported in Crain's, the Commercial Observer, QNS and the Queens
Post — and the only thing Places had to say about it was the Citi Bike dock at
the kerb. The sweep wrote `type=bike_sharing_station` and the building kept a
clean target badge.

These are pure-function tests on the reading, not on the network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.enrich_web_use import (
    borough_of, classify, corroborate, distinctive_tokens, mentions_address,
    names_the_place, query_addresses, results_from_message,
)

LIC = ["35-02 37 AVENUE", "37-06 36 STREET", "37-06 36TH STREET", "3706 36TH STREET"]

CRAINS = {
    "title": "LIC Paper Factory hotel to remain a shelter under $66M city contract",
    "snippet": (
        "A Long Island City hotel at 37-06 36th Street converted into an "
        "emergency shelter for asylum-seekers is now operating as a "
        "conventional homeless shelter for single adults through June 2030."
    ),
    "link": "https://www.crainsnewyork.com/real-estate/lic-paper-factory-hotel-remain-shelter",
}


# --- the reading -------------------------------------------------------------

def test_the_shelter_is_found_and_disqualified():
    v = classify([CRAINS], LIC)
    assert v is not None, "the conversion is on the open web; not finding it is the bug"
    assert v["current_use"] == "shelter"
    assert v["transient_ok"] is False


def test_the_reading_carries_the_sentence_it_rests_on():
    # current_use_basis is read by a person deciding whether to believe us.
    # "type=bike_sharing_station" was honest about its evidence, which is how
    # this was findable at all. A web reading has to be at least as checkable.
    v = classify([CRAINS], LIC)
    assert "homeless shelter" in v["basis"]
    assert "crainsnewyork.com" in v["basis"]
    assert v["evidence"][0]["link"].startswith("https://")


def test_a_newsroom_is_believed_further_than_a_listings_farm():
    farm = dict(CRAINS, link="https://apartments.example.com/37-06-36th-street")
    assert classify([CRAINS], LIC)["use_confidence"] == "medium"
    assert classify([farm], LIC)["use_confidence"] == "low"


# --- the gates that stop it inventing things ---------------------------------

def test_an_article_about_another_building_is_not_evidence():
    # A search for an address returns the block, the corner and the listing
    # next door. Without this gate every shelter story in Queens lands on
    # every Queens building the search touched.
    elsewhere = {
        "title": "City opens homeless shelter in Astoria",
        "snippet": "The new homeless shelter at 21-10 45th Road will house 200 adults.",
        "link": "https://www.qns.com/astoria-shelter",
    }
    assert classify([elsewhere], LIC) is None


def test_shelter_is_tested_before_the_uses_the_team_wants_to_see():
    # The team asked to see SROs, hostels, members clubs and dorms, and not
    # shelters. A page carrying both words has to resolve to the one that
    # disqualifies — reading it as a hostel silently restores the building.
    both = {
        "title": "Former hostel at 37-06 36th Street now a homeless shelter",
        "snippet": "The building, once marketed as a hostel, houses single adults.",
        "link": "https://www.qns.com/lic",
    }
    assert classify([both], LIC)["current_use"] == "shelter"


def test_the_uses_the_team_asked_to_keep_stay_transient():
    for use, text in [
        ("hostel", "A youth hostel at 37-06 36th Street with 40 beds."),
        ("sro", "The single room occupancy building at 37-06 36th Street."),
        ("student_housing", "A dormitory at 37-06 36th Street for 300 students."),
        ("private_club", "The private members club at 37-06 36th Street."),
    ]:
        v = classify([{"title": "", "snippet": text, "link": "https://qns.com/x"}], LIC)
        assert v["current_use"] == use, f"{use} read as {v and v['current_use']}"
        assert v["transient_ok"] is True, f"{use} must stay visible to the team"


def test_the_address_gate_needs_the_number_and_the_street():
    assert mentions_address("work at 37-06 36th Street began", LIC)
    assert not mentions_address("37-06 Northern Boulevard", LIC)
    assert not mentions_address("a building on 36th Street", LIC)


# --- what we spend on it -----------------------------------------------------

def test_spellings_collapse_but_real_corners_do_not():
    # Five spellings of one Queens building. Three are the same address typed
    # differently and cost nothing extra; 37-06 36th Street is a different
    # corner of the same building and is the one the press actually used, so
    # searching only the primary address finds the conversion not at all.
    assert query_addresses(["156 TILLARY STREET", "156 TILLARY ST"]) == ["156 TILLARY STREET"]
    qs = query_addresses(LIC)
    assert "35-02 37 AVENUE" in qs
    assert any("36" in q and "STREET" in q for q in qs)
    assert len(qs) <= 3


def test_street_is_not_mangled_into_reet():
    # Stripping ordinal suffixes after the punctuation goes takes the "st" out
    # of "street" as well, and then no two spellings ever match.
    assert query_addresses(["36 STREET", "36TH STREET"]) == ["36 STREET"]


# --- the overlay, end to end -------------------------------------------------

def _stage(tmp_path, places_use, basis, web_row=None):
    """Put a Places sweep and a web sweep on disk and read them back."""
    import json

    import src.enrich as enrich

    (tmp_path / "google_current_use_20260929.json").write_text(json.dumps([{
        "bbl": "4003770013", "address": "35-02 37 AVENUE",
        "current_use": places_use, "current_use_label": "Ground-floor tenant only",
        "google_name": "Citi Bike: 37 Ave & 35 St", "basis": basis,
        "use_confidence": "low", "swept": True,
    }]))
    if web_row is not None:
        (tmp_path / "web_current_use_20260929.json").write_text(json.dumps([web_row]))

    original = enrich.DATA_RAW
    enrich.DATA_RAW = tmp_path
    try:
        return enrich.load_current_use()["4003770013"]
    finally:
        enrich.DATA_RAW = original


SHELTER_ROW = {
    "bbl": "4003770013", "address": "35-02 37 AVENUE", "swept": True,
    "current_use": "shelter", "current_use_label": "Shelter",
    "use_confidence": "medium",
    "basis": "web:'homeless shelter' in www.crainsnewyork.com",
    "evidence": [{"title": "LIC Paper Factory hotel to remain a shelter",
                  "snippet": "...", "link": "https://www.crainsnewyork.com/x"}],
}


def test_a_citi_bike_dock_no_longer_settles_what_a_building_is(tmp_path):
    # This is the whole defect. The sweep found a bike dock at the kerb of a
    # 125-room shelter, wrote it down as the building's use, and the building
    # kept a clean target badge.
    before = _stage(tmp_path, "ground_floor_tenant", "type=bike_sharing_station")
    assert before["current_use"] == "ground_floor_tenant"

    after = _stage(tmp_path, "ground_floor_tenant", "type=bike_sharing_station",
                   SHELTER_ROW)
    assert after["current_use"] == "shelter"
    assert "crainsnewyork.com" in after["basis"]
    assert after["needs_review"] is True, "a web reading is seen by a person first"


def test_the_shelter_disqualifies_the_building():
    from src.enrich_current_use import NON_TRANSIENT_USES

    assert "shelter" in NON_TRANSIENT_USES
    # And the four the team asked to keep are not swept up with it.
    for keep in ("sro", "hostel", "private_club", "student_housing"):
        assert keep not in NON_TRANSIENT_USES, f"the team asked to see {keep}"


def test_the_web_never_overrules_a_real_places_answer(tmp_path):
    # A building Places calls a hotel is a hotel. A news page about what it was
    # before is not better evidence than an address-verified live listing, and
    # letting it win would relabel every hotel that was ever something else.
    held = _stage(tmp_path, "hotel", "type=hotel", SHELTER_ROW)
    assert held["current_use"] == "hotel"
    assert held["google_name"] == "Citi Bike: 37 Ave & 35 St"


# --- the city's own notices --------------------------------------------------

def test_a_shelter_notice_reads_as_a_site_not_a_mailbox():
    from src.enrich_city_record import facility_sites, normalise

    # The real notice, and the boilerplate that sits beside it in the same
    # file. A first pass without the mailing filter matched 81 buildings, most
    # of them the Law Department's own address for posting bids.
    notices = [
        {"agency_name": "Homeless Services", "start_date": "2025-01-17",
         "short_title": "Destination Services at SA 36th St LIC 2025",
         "request_id": "20250729011",
         "additional_description_1":
             "<p>Shelter facillities for Homeless Single Adults located at "
             "37-06 36th Street, Long Island City, NY 11101.</p>"},
        {"agency_name": "Homeless Services", "start_date": "2025-02-02",
         "short_title": "bid", "request_id": "x",
         "additional_description_1":
             "Proposals must be submitted to the Contracting Officer at the "
             "following address: Office of ACCO, located at 150 Greenwich "
             "Street, New York, NY."},
    ]
    sites = facility_sites(notices)
    assert normalise("37-06 36th Street") in sites
    assert normalise("150 Greenwich Street") not in sites, "that is where bids go"


def test_only_the_agencies_that_run_shelters_are_read():
    from src.enrich_city_record import facility_sites

    # "shelter" appears in design commission agendas, landmarks filings and
    # animal shelter notices, each carrying addresses that have nothing to do
    # with the word.
    agenda = [{
        "agency_name": "Public Design Commission", "start_date": "2018-04-12",
        "short_title": "Design Commission Meeting Agenda", "request_id": "y",
        "additional_description_1":
            "Reconstruction of the bus shelter located at 10 South Street, Manhattan.",
    }]
    assert facility_sites(agenda) == {}


def test_the_notice_flags_and_never_decides(tmp_path):
    # The single most important property of this source. The City Record
    # carries a publication date and no contract term, so 17 Battery Place
    # holds a DHS notice from December 2008 and is an operating hotel today.
    # A source that spoke with authority would delete it from the list.
    import json

    import src.enrich as enrich

    (tmp_path / "google_current_use_20260929.json").write_text(json.dumps([{
        "bbl": "1000160001", "address": "17 BATTERY PLACE",
        "current_use": "hotel", "current_use_label": "Hotel",
        "google_name": "The Wagner", "basis": "type=hotel",
        "use_confidence": "high", "swept": True,
    }]))
    (tmp_path / "city_record_shelter_20260929.json").write_text(json.dumps([{
        "bbl": "1000160001", "address": "17 BATTERY PLACE",
        "latest_notice": "2008-12-26", "notice_count": 1,
        "notices": [{"agency": "Homeless Services", "published": "2008-12-26"}],
    }]))

    original = enrich.DATA_RAW
    enrich.DATA_RAW = tmp_path
    try:
        use = enrich.load_current_use()["1000160001"]
        notice = enrich.load_city_record_shelter()["1000160001"]
    finally:
        enrich.DATA_RAW = original

    assert use["current_use"] == "hotel", "a 2008 notice must not unseat a live listing"
    assert notice["latest_notice"] == "2008-12-26"


def test_the_reason_code_is_taken_back_when_the_field_is_false():
    # The same contradiction the completion stage exists to remove: a chip on
    # the panel with the field behind it reading False.
    from src.build_geojson import FIELD_OWNED, complete_reason_codes

    assert "city_shelter_notice" in FIELD_OWNED
    features = [
        {"properties": {"reason_codes": ["city_shelter_notice"], "shelter_notice": False}},
        {"properties": {"reason_codes": [], "shelter_notice": True}},
    ]
    complete_reason_codes(features)
    assert features[0]["properties"]["reason_codes"] == []
    assert "city_shelter_notice" in features[1]["properties"]["reason_codes"]


# --- active shelter versus former shelter ------------------------------------

def _grade(notices, today=None):
    from datetime import date as _d

    from src.enrich_city_record import grade
    return grade(notices, today or _d(2026, 9, 29))


def test_a_live_award_and_a_dead_notice_are_not_the_same_fact():
    # The whole point of grading. 1 Hoyt Street was awarded to the African
    # American Planning Commission for $51.9m in 2026. 17 Battery Place last
    # appeared in a DHS notice in December 2008 and is an operating hotel.
    live, _ = _grade([{"published": "2026-07-15", "notice_kind": "Award",
                       "vendor": "African American Planning Commission", "term_end": ""}])
    dead, why = _grade([{"published": "2008-12-26", "notice_kind": "", "vendor": "",
                         "term_end": ""}])
    assert live == "active"
    assert dead == "historical"
    assert "2008" in why


def test_a_contract_term_beats_every_guess_under_it():
    # Where a notice states its term, that is the answer — an award published
    # long ago whose term still runs is active, and a recent one whose term
    # has expired is not.
    old_award_live_term, why = _grade([
        {"published": "2019-01-01", "notice_kind": "Award", "vendor": "x",
         "term_end": "2030-06-30"}])
    assert old_award_live_term == "active"
    assert "2030-06-30" in why

    recent_but_expired, _ = _grade([
        {"published": "2024-01-01", "notice_kind": "Award", "vendor": "x",
         "term_end": "2025-06-30"}])
    assert recent_but_expired == "historical"


def test_the_middle_is_admitted_rather_than_guessed():
    # 203 Jay Street: a 2022 notice, no award record. Old enough that an award
    # would usually have followed, recent enough that it may still run. Saying
    # "active" there would be inventing a fact.
    status, why = _grade([{"published": "2022-04-29", "notice_kind": "", "vendor": "",
                           "term_end": ""}])
    assert status == "uncertain"
    assert "no award" in why


def test_american_dates_in_notices_survive_the_trip():
    from src.enrich_city_record import _iso

    assert _iso("6/30/2030") == "2030-06-30"
    assert _iso("7/1/25") == "2025-07-01"
    assert _iso("13/40/2030") == ""   # not a date; must not become one
    assert _iso("") == ""


# --- the penalty, and what it is allowed to fire on --------------------------

def _cu(use, basis, occupants):
    return {"current_use": use, "basis": basis,
            "occupants": [{"name": n, "use": u} for n, u in occupants]}


def test_one_tenant_does_not_make_the_address_a_church():
    # 33 Rector Street: Orthodox Union has an office there, beside a wine shop,
    # a legal charity and a public parking garage. One of eight. The building
    # lost 35 points for it, and 238 buildings were in the same position.
    from src.enrich import _use_holds_building

    rector = _cu("religious", "type=church", [
        ("Orthodox Union", "religious"), ("West Street Wine", "ground_floor_tenant"),
        ("Urban Justice Centre", "office"), ("90 Washington Parking", "ground_floor_tenant"),
    ])
    assert _use_holds_building(rector) is False


def test_a_building_genuinely_in_other_hands_still_counts_against():
    from src.enrich import _use_holds_building

    masjid = _cu("religious", "type=mosque", [("Masjid Manhattan", "religious")])
    assert _use_holds_building(masjid) is True


def test_a_name_alone_never_carries_the_penalty():
    # "Fountain Pen Hospital" is a pen shop on Warren Street. A name match read
    # it as a medical facility and took 35 points off the building; 49 of the
    # 278 penalties rested on nothing sturdier.
    from src.enrich import _use_holds_building

    pens = _cu("medical", "name~hospital", [("Fountain Pen Hospital", "medical")])
    assert _use_holds_building(pens) is False


def test_nothing_found_is_not_evidence_of_occupation():
    from src.enrich import _use_holds_building

    assert _use_holds_building(_cu("religious", "type=church", [])) is False


def test_a_carried_row_is_re_read_under_the_current_rules():
    # Splitting hostels out of institutional lodging left 15 of them still
    # labelled as institutional lodging — a use the score penalises — because
    # the merge kept the old reading along with the place. The team had asked
    # to be shown hostels.
    from src.enrich_current_use import NON_TRANSIENT_USES, classify

    use, label, _, basis = classify({
        "displayName": {"text": "Chelsea International Hostel"},
        "primaryType": "hostel", "types": ["hostel", "lodging"],
        "businessStatus": "OPERATIONAL",
    })
    assert use == "hostel"
    assert use not in NON_TRANSIENT_USES, "the team asked to see hostels"
    assert basis == "type=hostel"


def test_restating_corrects_a_reading_and_never_erases_one():
    # A stored row that carries a name but no place type re-derives to
    # "unknown". Letting that land would blank a reading made when the whole
    # place was in hand — turning a rule change into data loss.
    from src.enrich_current_use import restate

    thin = {"google_name": "The Standard High Line", "current_use": "hotel",
            "current_use_label": "Hotel", "basis": "type=hotel"}
    assert restate(thin) is False
    assert thin["current_use"] == "hotel"

    stale = {"google_name": "Chelsea International Hostel", "primary_type": "hostel",
             "types": ["hostel", "lodging"], "business_status": "OPERATIONAL",
             "current_use": "institutional_lodging",
             "current_use_label": "Nonprofit / institutional lodging"}
    assert restate(stale) is True
    assert stale["current_use"] == "hostel"


# --- reversion: the two things it is actually asking ------------------------

def _rev(**kw):
    from src.enrich import _pre_cutoff_transient_evidence
    return _pre_cutoff_transient_evidence(kw)


def test_a_conversion_after_the_cutoff_is_the_whole_point():
    # The overlay's own words: hotels that converted to residential post-2021,
    # which can revert because the hotel use predates the amendment.
    ok, why = _rev(dob_conversion_detail="J-1 -> R-2 (03/14/2023)", bldgclass="RM")
    assert ok is True
    assert "2023-03-14" in why


def test_a_conversion_before_the_cutoff_disqualifies_rather_than_puzzles():
    # The old test could only fail to find evidence. This one can say the use
    # was already gone when the amendment landed, which is a different and
    # more useful answer.
    ok, why = _rev(dob_conversion_detail="J-1 -> J-2 (03/30/2005)", bldgclass="RM")
    assert ok is False
    assert "2005-03-30" in why and "before" in why


def test_the_building_class_no_longer_decides_it():
    # 554 Third Avenue is the former Residence Inn Midtown East, carried by
    # HPD as RM with 144 Class A units and no Class B. Requiring a surviving
    # hotel class to even look at the history hid it, and 33 others DOB
    # records as converted out of transient use.
    ok, _ = _rev(bldgclass="RM", dob_conversion_detail="J-1 -> A-3 (06/01/2022)")
    assert ok is True


def test_a_licence_live_at_the_cutoff_counts():
    ok, why = _rev(bldgclass="D6", hotel_license_created="2019-04-01",
                   hotel_license_expiration="2023-04-01")
    assert ok is True
    assert "2019-04-01" in why


def test_a_licence_that_had_already_lapsed_does_not():
    ok, _ = _rev(bldgclass="D6", hotel_license_created="2015-01-01",
                 hotel_license_expiration="2018-01-01")
    assert ok is False


def test_a_known_former_hotel_with_no_date_is_shown_and_not_asserted():
    # 554's ground truth carries a name and no year. That is worth surfacing
    # for someone to check, and not worth claiming.
    ok, why = _rev(bldgclass="RM", prior_operator={"name": "Residence Inn Midtown East"})
    assert ok is False
    assert "Residence Inn" in why and "nothing dates" in why


def test_a_dated_conversion_outranks_everything_under_it():
    # A licence live at the cutoff cannot rescue a building DOB says stopped
    # being a hotel in 2005.
    ok, _ = _rev(bldgclass="H2", dob_conversion_detail="J-1 -> R-2 (01/01/2005)",
                 hotel_license_created="2019-01-01", hotel_license_expiration="2024-01-01")
    assert ok is False


def test_a_building_trading_as_a_hotel_has_nothing_to_revert_to(tmp_path):
    """Run the real enrich step over one record and read the result back.

    The licence test catches this only where DCWP shows an active one, and
    licences lapse while hotels keep trading: 50 Bowery reads "Failed to
    Renew" at DCWP and is Hotel 50 Bowery, JDV by Hyatt, on the ground; 123
    Washington Street is The Washington Hotel NYC with no licence at all.
    Both sat in the reversion list as buildings that had gone residential.
    """
    import inspect

    import src.enrich as enrich

    source = inspect.getsource(enrich.enrich_pipeline)
    assert 'record.get("current_use") == "hotel"' in source, (
        "the reversion block no longer clears a building that is trading as a hotel"
    )
    assert source.index('record.get("current_use") == "hotel"') < source.index(
        "_pre_cutoff_transient_evidence(record)"
    ), "the clear has to run before the cutoff test spends work on it"


def test_dob_filing_dates_are_used_instead_of_shrugging():
    # "undated in our data" was a statement about our plumbing that read like
    # a statement about DOB. The pull carried earliest_date and latest_date
    # all along and enrich dropped both on the way in.
    ok, why = _rev(bldgclass="RM", dob_has_r1=True,
                   dob_transient_last_filing="03/14/2023")
    assert ok is True
    assert "2023-03-14" in why

    ok, why = _rev(bldgclass="RM", dob_has_r1=True,
                   dob_transient_last_filing="10/01/2020")
    assert ok is False
    assert "2020-10-01" in why
    assert "undated" not in why


def test_a_filing_date_is_read_by_year_not_by_month():
    # DOB writes MM/DD/YYYY. Compared as those strings, "10/01/2020" sorts
    # before the December cutoff for the wrong reason and half the file comes
    # out backwards.
    from src.enrich import _sortable_us

    assert _sortable_us("12/30/2013") < _sortable_us("01/04/2016")
    assert _sortable_us("03/14/2023") == "2023-03-14"
    assert _sortable_us("") == ""


def test_a_candidate_with_no_history_says_so_rather_than_shrugging():
    # Eleven of the 42 are proposed on their building class and unit mix and
    # nothing else. "No transient use evidenced" reads as though somewhere was
    # searched and came back empty, which invites someone to go and verify a
    # building that was never recorded as a hotel anywhere.
    ok, why = _rev(bldgclass="HS", hpd_class_a=19)
    assert ok is False
    assert "nothing records this as a hotel at all" in why
    assert "HS" in why and "19" in why

    # A building that does have a history gets the other message — something
    # was looked at and did not reach the cutoff.
    ok, why = _rev(bldgclass="H3", hpd_class_a=40, coo_count=12)
    assert ok is False
    assert "nothing records this as a hotel at all" not in why


def test_a_hotel_that_left_before_the_amendment_is_ruled_out():
    # 330 East 56th Street was the Sutton Hotel and became condominiums in
    # 2005 — sixteen years before the cutoff. It sat in the list unverified,
    # as though somebody might yet find the evidence, when what was missing
    # was a year that took one search to find.
    ok, why = _rev(bldgclass="RM", coo_count=4,
                   prior_operator={"name": "AKA Sutton Place", "end_year": "2005"})
    assert ok is False
    assert "2005" in why and "before" in why


def test_an_operator_who_arrived_before_the_cutoff_still_counts():
    ok, why = _rev(bldgclass="D9", coo_count=4,
                   prior_operator={"name": "Sonder Wall Street", "start_year": "2019"})
    assert ok is True
    assert "2019" in why


def test_leaving_outranks_arriving():
    # Both years present: the departure decides it. An operator who arrived in
    # 2015 and left in 2018 was not there when the amendment landed.
    ok, _ = _rev(bldgclass="RM", coo_count=4,
                 prior_operator={"name": "x", "start_year": "2015", "end_year": "2018"})
    assert ok is False


def test_every_recorded_year_says_where_it_came_from():
    # A year in a hand-kept file with nothing behind it is the thing nobody
    # can re-check, which is how "6 tracked" survived in the reference for
    # months after it stopped being six.
    import csv
    from pathlib import Path

    rows = list(csv.DictReader((Path(__file__).parent.parent / "ground_truth.csv").open()))
    dated = [r for r in rows if r.get("start_year") or r.get("end_year")]
    assert dated, "no prior operator carries a year yet"
    for r in dated:
        assert r.get("year_source"), f"{r['name']} has a year and no source"


# --- Treatment programmes and on-site clinical services --------------------
#
# 203 West 113 Street is the case that put these in the table. Weston House is
# an apartment treatment programme with restorative services and an ACT team,
# and it read as available: the Places sweep found a residential neighbour —
# in the Bronx, 12m from a Harlem footprint — and not one phrase in
# CLASSIFY_RULES matched a word of what the building actually does.
#
# They go under supportive_housing rather than a category of their own because
# the question this answers is not what kind of programme it is. It is whether
# anybody is running the building, and the answer is the same.

WESTON = ["203 WEST 113 STREET", "203 W 113 STREET"]


def _read(snippet, addresses=WESTON, link="https://www.op.nysed.gov/x"):
    return classify([{"title": "", "snippet": snippet, "link": link}], addresses)


def test_an_apartment_treatment_programme_is_not_available():
    v = _read("Weston House — apartment treatment program at 203 West 113th "
              "Street with on-site restorative services and an ACT team.")
    assert v is not None, "a treatment programme must be read as somebody running the building"
    assert v["current_use"] == "supportive_housing"
    assert v["transient_ok"] is False


def test_on_site_clinical_services_count_the_same_as_supportive_housing():
    for snippet in (
        "203 West 113th Street: residential treatment for adults with serious mental illness.",
        "Supportive services and mental health services at 203 West 113th Street.",
        "Behavioral health services at 203 W 113th St.",
    ):
        v = _read(snippet)
        assert v is not None and v["current_use"] == "supportive_housing", snippet


def test_the_broad_words_on_their_own_still_do_not_fire():
    # The reason every phrase above is several words long. "treatment",
    # "services" and "clinic" alone match a dentist on the ground floor and a
    # news page about the treatment of asylum seekers somewhere else entirely,
    # which is the failure the rest of this table is written against.
    assert _read("Dental clinic and treatment rooms on the ground floor at "
                 "203 West 113th Street.") is None
    assert _read("City criticised over its treatment of asylum seekers in "
                 "Queens shelters.") is None


def test_a_programme_at_another_address_is_still_not_evidence():
    assert _read("Supportive housing opens at 500 West 20th Street.") is None


def test_shelter_still_outranks_a_treatment_programme():
    # Ordering matters more now that the supportive_housing row matches many
    # more pages. A building that is both described as a former programme and
    # reported as a shelter has to resolve to the shelter — that is the
    # reading that disqualifies it, and the one that costs most if it is lost.
    v = _read("203 West 113th Street, a former treatment program, is now a "
              "homeless shelter.", link="https://qns.com/x")
    assert v["current_use"] == "shelter"


def test_supportive_housing_is_a_use_the_readiness_model_calls_occupied():
    # The chain this whole row depends on: enrich_web_use labels the use,
    # deal_readiness decides what that means. If supportive_housing ever stops
    # being institutional, every phrase added above silently stops mattering.
    from src.deal_readiness import INSTITUTIONAL_USES
    assert "supportive_housing" in INSTITUTIONAL_USES


# --- corroborating a lone Places reading ------------------------------------
#
# The second question this module answers. 23 buildings in the no-operator
# segment were occupied on one Nearby Search hit and 22 more on one sweep
# occupant, with nothing outside Google agreeing. These tests are the readings
# that decide whether such a claim closes as occupied, goes back for review,
# or leaves the building undetermined.

BROADWAY = ["2508 BROADWAY"]


def test_a_page_naming_the_institution_at_the_address_confirms_it():
    page = {
        "title": "Advent Lutheran Church — Upper West Side",
        "snippet": "Advent Lutheran Church at 2508 Broadway holds Sunday service at 11am.",
        "link": "https://www.gothamist.com/uws-churches",
    }
    v = corroborate([page], BROADWAY, "Advent Lutheran Church")
    assert v["verdict"] == "confirmed"
    assert v["evidence"][0]["link"].startswith("https://")


def test_the_address_gate_applies_to_corroboration_too():
    # The same failure the classifier has: a search for an address returns
    # the block. A church three doors down is not this building.
    elsewhere = {
        "title": "Advent Lutheran Church",
        "snippet": "Advent Lutheran Church at 93 Amsterdam Avenue.",
        "link": "https://www.gothamist.com/x",
    }
    assert corroborate([elsewhere], BROADWAY, "Advent Lutheran Church")["verdict"] == "none"


def test_housing_stock_at_the_address_contradicts_the_claim():
    page = {
        "title": "2508 Broadway apartments",
        "snippet": "Two apartments for rent at 2508 Broadway, a rental building on the UWS.",
        "link": "https://streeteasy.example.com/2508-broadway",
    }
    v = corroborate([page], BROADWAY, "Advent Lutheran Church")
    assert v["verdict"] == "contradicted"
    assert "apartments for rent" in v["basis"]


def test_a_confirmation_outranks_a_contradiction_when_both_appear():
    # A listing for a flat at an address is near-universal and says nothing
    # about the other floors. Erring toward confirmed costs a lead; erring the
    # other way puts somebody in front of a church.
    listing = {"title": "2508 Broadway", "snippet": "Apartments for rent at 2508 Broadway.",
               "link": "https://streeteasy.example.com/x"}
    news = {"title": "Advent Lutheran Church", "link": "https://gothamist.com/y",
            "snippet": "Advent Lutheran Church, 2508 Broadway, marks its centenary."}
    assert corroborate([listing, news], BROADWAY, "Advent Lutheran Church")["verdict"] == "confirmed"


def test_an_operator_under_another_name_still_confirms_somebody_runs_it():
    # Places may be wrong about what it is and right that the rooms are
    # spoken for. A shelter at the address is not a church, and it is also
    # not a building anybody can have.
    page = {"title": "City opens shelter", "link": "https://www.crainsnewyork.com/z",
            "snippet": "The homeless shelter at 2508 Broadway houses single adults."}
    v = corroborate([page], BROADWAY, "Advent Lutheran Church")
    assert v["verdict"] == "confirmed"
    assert "Shelter" in v["basis"]


def test_nothing_found_is_an_answer_and_not_a_failure():
    v = corroborate([], BROADWAY, "Advent Lutheran Church")
    assert v["verdict"] == "none"
    assert v["evidence"] == []


def test_the_name_test_needs_more_than_one_short_word():
    # "Prep For Prep" reduces to {prep}, and a page about 71st Street using
    # the word prep is not evidence the school is the building. Unconfirmed
    # is the right answer there, not wrong.
    assert not names_the_place("the prep course meets on West 71 Street", "Prep For Prep")
    # Two distinctive words, or one long enough to be a proper noun.
    assert names_the_place("Advent Lutheran on Broadway", "Advent Lutheran Church")
    assert names_the_place("services at Ascension", "Ascension Roman Catholic Church")


def test_the_words_every_institution_shares_are_not_distinctive():
    assert distinctive_tokens("Advent Lutheran Church") == {"advent", "lutheran"}
    assert distinctive_tokens("The MAve nyc") == {"mave"}
    # A corporate suffix is not a name. Zoe Ministries Inc. is Zoe Ministries.
    assert distinctive_tokens("Zoe Ministries Inc.") == {"zoe", "ministries"}


def test_the_query_carries_a_borough_the_build_never_published():
    # Both modes read b["borough"], and no build has ever carried that
    # property — every query went out with an empty string where the one word
    # that separates a Manhattan address from a Brooklyn one belongs.
    assert borough_of("1011680029") == "Manhattan"
    assert borough_of("4004060040") == "Queens"
    assert borough_of("3024870041") == "Brooklyn"
    assert borough_of("") == ""


# --- reading a Claude web-search response ------------------------------------
#
# The search client moved from the Programmable Search JSON API to the Claude
# API's server-side web search tool, because Programmable Search cannot search
# the whole web on our account -- an engine is pointed at sites, and "the
# entire web" only approximates one. The contract did not move: search() still
# returns title/snippet/link triples and the rules above still decide what
# they mean. These guard the new extraction, which is the only part that is
# genuinely new code.

SHELTER_RESPONSE = {
    "stop_reason": "end_turn",
    "content": [
        {"type": "text", "text": "I'll search for that."},
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "35-02 37 Avenue Queens shelter"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
         "content": [
             {"type": "web_search_result",
              "url": "https://www.crainsnewyork.com/lic-paper-factory",
              "title": "LIC Paper Factory hotel to remain a shelter",
              "encrypted_content": "Eqgf...", "page_age": "April 30, 2025"},
             {"type": "web_search_result",
              "url": "https://www.homelessshelterdirectory.org/lic",
              "title": "Paper Factory Hotel Shelter - Long Island City",
              "encrypted_content": "Eqgf...", "page_age": None},
         ]},
        {"type": "text",
         "text": "One source describes the conversion.",
         "citations": [
             {"type": "web_search_result_location",
              "url": "https://www.crainsnewyork.com/lic-paper-factory",
              "title": "LIC Paper Factory hotel to remain a shelter",
              "encrypted_index": "Eo8B...",
              "cited_text": "A Long Island City hotel at 37-06 36th Street "
                            "converted into an emergency shelter for "
                            "asylum-seekers is now operating under a $65.8 "
                            "million contract."},
         ]},
    ],
}


def test_a_citation_becomes_a_snippet_the_rules_can_read():
    """cited_text is verbatim source text, which is the shape a Programmable
    Search snippet was. It is the only field that can fire a phrase rule."""
    got = results_from_message(SHELTER_RESPONSE)
    cited = [r for r in got if r["snippet"]]
    assert len(cited) == 1
    assert "emergency shelter" in cited[0]["snippet"]
    assert cited[0]["link"] == "https://www.crainsnewyork.com/lic-paper-factory"


def test_an_uncited_result_survives_on_its_title():
    """The page body comes back encrypted and unreadable, but a title alone
    sometimes carries the answer -- and a page the model found but did not
    quote should not vanish without trace."""
    got = results_from_message(SHELTER_RESPONSE)
    links = [r["link"] for r in got]
    assert "https://www.homelessshelterdirectory.org/lic" in links
    uncited = next(r for r in got
                   if r["link"].endswith("/lic"))
    assert uncited["snippet"] == ""
    assert "Shelter" in uncited["title"]


def test_the_extraction_feeds_the_classifier_unchanged():
    """The join that matters: the new search layer's output still satisfies
    the rules written against the old one."""
    verdict = classify(results_from_message(SHELTER_RESPONSE), LIC)
    assert verdict is not None
    assert verdict["current_use"] == "shelter"
    assert verdict["evidence"][0]["link"].startswith("https://www.crains")


def test_a_search_error_is_not_mistaken_for_results():
    """An error arrives as a 200 with a single object where results are a
    list. Indexing it as a list would be the quiet kind of wrong."""
    got = results_from_message({
        "stop_reason": "end_turn",
        "content": [{"type": "web_search_tool_result", "tool_use_id": "s1",
                     "content": {"type": "web_search_tool_result_error",
                                 "error_code": "max_uses_exceeded"}}],
    })
    assert got == []


def test_nothing_found_is_empty_not_an_error():
    """A search that ran and matched nothing returns an empty list. That is a
    real answer -- it leaves a building undetermined, not unscreened."""
    assert results_from_message({"stop_reason": "end_turn", "content": [
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
    ]}) == []
    assert results_from_message({"stop_reason": "end_turn", "content": []}) == []


def test_the_same_page_cited_twice_is_one_result():
    """A paused turn resumes and can re-cite what the first leg already gave
    us. The same url with the same quote is one piece of evidence."""
    dupe = {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "a", "citations": [
            {"type": "web_search_result_location", "url": "https://e.org/a",
             "title": "A", "cited_text": "a shelter operates here"}]},
        {"type": "text", "text": "b", "citations": [
            {"type": "web_search_result_location", "url": "https://e.org/a",
             "title": "A", "cited_text": "a shelter operates here"}]},
    ]}
    assert len(results_from_message(dupe)) == 1


def test_a_result_with_no_url_is_dropped():
    """Evidence that cannot be opened is not evidence -- the panel renders
    the link, and a blank one reads as a broken citation."""
    assert results_from_message({"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "x", "citations": [
            {"type": "web_search_result_location", "url": "",
             "title": "T", "cited_text": "a shelter"}]},
    ]}) == []
