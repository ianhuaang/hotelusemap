"""The readiness contract, and the specific wrong answers it was built against.

Every test here is a building that came out wrong during the build, not a
hypothetical. The state names and field names are a contract with the app —
TransientCapacityApp reads readiness_state, readiness_basis and
not_ready_kind by those exact names — so a rename here is a silent blank
panel there, which is the current_use_partial bug all over again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.deal_readiness import (
    apply_readiness, load_verified_occupancy, readiness)

LOCKED_FIELDS = {
    "readiness_state", "readiness_basis", "not_ready_kind", "occupied_flag",
    "occupied_basis", "not_ready_flag", "operator_answered", "reversion_window",
    "places_claim", "readiness_verified_url", "readiness_verified_on",
}

# The no-operator segment, where a lone Places reading is no longer enough.
GOLD = {"segment": "transient"}


def test_the_published_field_names_are_the_ones_the_app_reads():
    out = readiness({"occupancy_state": "clear"})
    assert set(out) == LOCKED_FIELDS


def test_only_the_four_states_exist():
    assert readiness({})["readiness_state"] in {
        "available", "occupied", "not_ready", "undetermined"}


def test_a_building_nobody_established_is_undetermined_not_available():
    """The one direction this must never fail is the optimistic one."""
    out = readiness({"occupancy_state": "thin"})
    assert out["readiness_state"] == "undetermined"
    assert "ground-floor" in out["readiness_basis"]


def test_a_managing_agent_is_not_an_operator():
    """237 Madison and 229 Duffield both read 'occupied' on their letting agent.

    operator_name carries the HPD managing agent on 272 buildings. Reading it
    as somebody running the hotel made blocks of flats into occupied
    buildings and emptied the prospect list of its best entries.
    """
    flats = {
        "occupancy_state": "clear",
        "operator_name": "LIVINGSTON MANAGEMENT SERVICES LLC",
        "operator_source": "hpd_managing_agent",
        "hpd_class_b": 157,
        "current_use_occupants": [{"name": "No. 237 Madison",
                                   "type": "apartment_building",
                                   "use": "residential"}],
    }
    out = readiness(flats)
    assert out["readiness_state"] == "available"
    assert out["occupied_flag"] is False


def test_a_departed_operator_is_not_a_present_one():
    """operator_source 'ground_truth' names who USED to be here."""
    out = readiness({"occupancy_state": "clear",
                     "operator_name": "Sonder", "operator_source": "ground_truth"})
    assert out["occupied_flag"] is False


def test_somebody_living_here_is_not_somebody_running_it():
    out = readiness({"occupancy_state": "clear",
                     "current_use": "residential",
                     "current_use_occupants": [{"name": "The Webster Apartments",
                                                "use": "residential"}]})
    assert out["readiness_state"] == "available"


def test_an_institution_in_the_building_is_running_it():
    out = readiness({"occupancy_state": "clear",
                     "current_use_occupants": [{"name": "Church of the Blessed Sacrament",
                                                "use": "religious"}]})
    assert out["readiness_state"] == "occupied"


def test_a_hostel_or_dormitory_is_occupied_not_restricted():
    """restricted_class carries SRO, hostel and dormitory in one field.

    Only the first is a restriction. Filing an operator under a condition
    said 'something is holding this back' about a building somebody is
    running right now.
    """
    for head in ("Hostel", "Dormitory"):
        out = readiness({"restricted_class": True,
                         "restricted_class_reason": f"{head} — institutional use",
                         "occupancy_state": "clear"})
        assert out["readiness_state"] == "occupied", head
    sro = readiness({"restricted_class": True,
                     "restricted_class_reason": "SRO — rooms people live in",
                     "occupancy_state": "clear"})
    assert sro["readiness_state"] == "not_ready"
    assert sro["not_ready_kind"] == "restricted_conversion"


LIC = {"nearby_use": "lodging",
       "nearby_use_basis": "hotel 'LIC Plaza Hotel Corp' 3m from the footprint"}


def test_method_2_still_decides_outside_the_no_operator_segment():
    """Where the cost of a wrong reading is not a wasted day, one is enough."""
    out = readiness({"occupancy_state": "clear", "segment": "partial"}, LIC)
    assert out["readiness_state"] == "occupied"
    assert "LIC Plaza" in out["readiness_basis"]


def test_a_lone_places_reading_no_longer_takes_a_gold_building_off_the_list():
    """40-40 27 Street: LIC Plaza Hotel Corp, 3m from the footprint, and
    nothing else in the building's record agreeing with it.

    23 buildings in the no-operator segment were occupied on one Nearby
    Search hit — among them a hotel called "Jordan Barbara Schwinn" at 11
    West 67 Street and a Hebrew-language listing at 859 7 Avenue. One source
    cannot both make the claim and corroborate it.
    """
    out = readiness({**GOLD, "occupancy_state": "clear"}, LIC)
    assert out["readiness_state"] == "undetermined"
    assert out["places_claim"] == "unscreened"
    assert "not yet corroborated" in out["readiness_basis"]


def test_the_web_confirming_it_closes_the_claim_as_occupied():
    out = readiness({**GOLD, "occupancy_state": "clear"}, LIC,
                    {"verdict": "confirmed",
                     "basis": "web names 'LIC Plaza Hotel Corp' at the address, in qns.com"})
    assert out["readiness_state"] == "occupied"
    assert out["places_claim"] == "confirmed"
    assert "LIC Plaza" in out["readiness_basis"]
    assert "confirmed on the web" in out["readiness_basis"]


def test_the_web_contradicting_it_sends_the_building_back_for_review():
    out = readiness({**GOLD, "occupancy_state": "clear"}, LIC,
                    {"verdict": "contradicted", "basis": "web:'apartments for rent' in streeteasy.com"})
    assert out["places_claim"] == "contradicted"
    assert out["readiness_state"] == "undetermined", "never straight to available"
    assert "needs review" in out["readiness_basis"]


def test_the_web_finding_nothing_leaves_the_building_undetermined():
    out = readiness({**GOLD, "occupancy_state": "clear"}, LIC, {"verdict": "none"})
    assert out["places_claim"] == "unconfirmed"
    assert out["readiness_state"] == "undetermined"
    assert "found nothing to confirm it" in out["readiness_basis"]


def test_an_open_claim_never_falls_through_to_available():
    """The 11 buildings this was written for.

    Retiring the lone reading without touching _operator_answered sent 11 of
    the 23 to available rather than undetermined — they carry a parsed
    certificate or a clear sweep, and either counts as answered on its own.
    2508 Broadway reads as Advent Lutheran Church and 310 Riverside Drive as
    Zoe Ministries; both would have arrived in the clean prospecting view.
    """
    for answered in ({"occupancy_state": "clear"},
                     {"occupancy_state": "onrecord",
                      "coo_floors": [{"floor": "02", "kind": "residential"}]}):
        for verdict in (None, {"verdict": "none"}, {"verdict": "contradicted", "basis": "x"}):
            out = readiness({**GOLD, **answered}, LIC, verdict)
            assert out["readiness_state"] == "undetermined", (answered, verdict)
            assert out["operator_answered"] is False


def test_a_named_condition_answers_before_an_open_claim_does():
    """25 of the 45 land here, and the condition was always the better answer.

    342 West 71 Street is SRO stock, 859 7 Avenue has a demolition filing and
    11 West 67 Street an active 421-a. Each was occupied on a Places reading
    that masked a condition the pipeline had already established. The claim
    stays published, so enrich_web_use still picks the building up.
    """
    out = readiness({**GOLD, "occupancy_state": "clear", "restricted_class": True,
                     "restricted_class_reason": "SRO — rent-regulated rooming stock"},
                    LIC)
    assert out["readiness_state"] == "not_ready"
    assert out["not_ready_kind"] == "restricted_conversion"
    assert out["places_claim"] == "unscreened", "the claim is still open and still screenable"


def test_corroboration_from_outside_google_is_not_a_places_claim_at_all():
    """A licence, a roster or a shelter notice answers it without the web."""
    for extra in ({"hotel_license_status": "Active", "hotel_license_name": "Highgate"},
                  {"htc_union": True, "htc_union_name": "The Mave"},
                  {"shelter_status": "active"},
                  {"current_use_conflict": True, "current_use_label": "Government / civic"}):
        out = readiness({**GOLD, "occupancy_state": "clear", **extra}, LIC)
        assert out["readiness_state"] == "occupied", extra
        assert out["places_claim"] == "", extra


def test_the_sweep_occupant_is_the_same_source_as_method_2():
    """115 East 92 Street is 'occupied' by Digital Piano Review.

    The occupant rule is Method 2's own source read a second way, and it is
    the weaker reading: no 30m radius, no guard-4 name test. The three names
    test_guard_4 asserts Method 2 must reject are all occupying a building
    through this rule. Leaving it out would have made the change a no-op on
    9 of the 23, which reach occupied both ways.
    """
    piano = {**GOLD, "occupancy_state": "clear",
             "current_use_occupants": [{"name": "Digital Piano Review", "use": "education"}]}
    out = readiness(piano, {})
    assert out["places_claim"] == "unscreened"
    assert out["readiness_state"] == "undetermined"
    # And it still decides on its own outside the segment.
    assert readiness({**piano, "segment": "partial"}, {})["readiness_state"] == "occupied"


def test_method_2_residential_does_not_make_a_building_occupied():
    out = readiness({"occupancy_state": "clear"},
                    {"nearby_use": "residential",
                     "nearby_use_basis": "apartment_building 'The Dian' 18m from the footprint"})
    assert out["readiness_state"] == "available"


def test_a_closed_hotel_is_not_ready_rather_than_occupied():
    out = readiness({"occupancy_state": "clear"},
                    {"nearby_use": "lodging_closed",
                     "nearby_use_basis": "hotel 'X' 5m from the footprint"})
    assert out["readiness_state"] == "not_ready"
    assert out["not_ready_kind"] == "closed_hotel"


def test_demolition_outranks_every_other_condition():
    out = readiness({"occupancy_state": "clear", "demolition": {"filed": "2026-05-28"},
                     "restricted_class": True,
                     "restricted_class_reason": "SRO — rooms people live in",
                     "coo_temp_only": True})
    assert out["not_ready_kind"] == "demolition"


def test_a_live_licence_outranks_an_inferred_use():
    out = readiness({"hotel_license_status": "Active",
                     "hotel_license_name": "Conrad Employer LLC",
                     "occupancy_state": "clear"})
    assert out["readiness_state"] == "occupied"
    assert "Conrad" in out["readiness_basis"]


def test_not_ready_kind_is_empty_unless_the_state_is_not_ready():
    for p in ({"occupancy_state": "clear"}, {"hotel_license_status": "Active"}, {}):
        out = readiness(p)
        if out["readiness_state"] != "not_ready":
            assert out["not_ready_kind"] == ""


def test_apply_readiness_writes_every_feature_and_counts_them():
    feats = [{"properties": {"bbl": "1", "occupancy_state": "clear"}},
             {"properties": {"bbl": "2", "occupancy_state": "thin"}}]
    counts = apply_readiness(feats, {})
    assert counts == {"available": 1, "undetermined": 1}
    for f in feats:
        assert LOCKED_FIELDS <= set(f["properties"])


def test_the_city_register_knowing_rooms_is_not_an_answer_about_the_operator():
    """occupancy_state 'onrecord' is tautological in the no-operator segment.

    It fires on hpd_class_b > 0, and a building is in that segment *because*
    it has Class B rooms. Counting it as an answer marked 113 of 221 buildings
    answered on the strength of the criterion that selected them, and put
    undetermined at zero — a model that cannot say "I don't know" is not a
    model, it is an opinion.
    """
    out = readiness({"occupancy_state": "onrecord", "hpd_class_b": 97})
    assert out["operator_answered"] is False
    assert out["readiness_state"] == "undetermined"


def test_counting_certificates_is_not_reading_one():
    """coo_count is how many are on file; coo_floors is one that parsed."""
    unread = readiness({"occupancy_state": "onrecord", "coo_count": 8})
    assert unread["operator_answered"] is False
    read_one = readiness({"occupancy_state": "onrecord", "coo_count": 8,
                          "coo_floors": [{"floor": "02", "kind": "residential"}]})
    assert read_one["operator_answered"] is True
    assert read_one["readiness_state"] == "available"


def test_nothing_reaches_available_without_being_answered():
    """The invariant the whole strict model rests on."""
    for p in ({}, {"occupancy_state": "onrecord"}, {"occupancy_state": "thin"},
              {"coo_count": 30}, {"hpd_class_b": 300}):
        out = readiness(p)
        if out["readiness_state"] == "available":
            assert out["operator_answered"], p
        assert out["readiness_state"] != "available"


def test_guard_4_rejects_a_practice_and_keeps_an_institution():
    """Places types a piano review site as a school and a mohel as a place of
    worship. Both would have taken a building off the prospect list."""
    from src.enrich_nearby_use import institution_is_plausible
    for tenant in ("Digital Piano Review", "Suffolk Addressing Services",
                   "Rabbi Zachary Hepner, Mohel", "Claudia Knafo Piano Studio",
                   "University of St Andrews Alumni Association",
                   "David William Phillips Concert Pianist"):
        assert not institution_is_plausible(tenant), tenant
    for real in ("Ascension Roman Catholic Church", "Masjid alfirdous",
                 "Escuela dylan", "Riverside Montessori School",
                 "Zoe Ministries Inc.", "Mision Guadalupana",
                 "Conservative Synagogue of Fifth Avenue"):
        assert institution_is_plausible(real), real


def test_a_rejected_institution_does_not_become_available():
    """Guard 4 must fail safe. Dropping the reading leaves the building with
    no building-level use, which is undetermined, not a clean bill of health."""
    out = readiness({"occupancy_state": "onrecord"}, {"nearby_use": ""})
    assert out["readiness_state"] == "undetermined"


# --- hand-checked verdicts from ground_truth.csv ------------------------------
#
# Nine buildings a person opened a source for and read, where both the city
# records and the Places sweep said available and something was plainly
# running in the building. They lived in a const in the app until now, which
# corrected the browser and left every weekly run deriving the same nine wrong
# answers. These guard the move and the asymmetry that makes it safe.

VERIFIED = {
    "basis": "the Jack Ryan Residence occupies floors 6 to 9, a 200-bed shelter",
    "url": "https://example.org/jack-ryan",
    "verified_on": "2026-10-07",
}


def test_a_hand_check_outranks_a_clean_derived_verdict():
    """The whole point. Without it the building reads available."""
    clean = {"occupancy_state": "clear", "segment": "transient"}
    assert readiness(clean)["readiness_state"] == "available"
    out = readiness(clean, verified=VERIFIED)
    assert out["readiness_state"] == "occupied"
    assert out["readiness_basis"] == VERIFIED["basis"]


def test_a_hand_check_publishes_where_it_came_from():
    """A corrected verdict nobody can re-check is worth less than one they can."""
    out = readiness({"occupancy_state": "clear"}, verified=VERIFIED)
    assert out["readiness_verified_url"] == VERIFIED["url"]
    assert out["readiness_verified_on"] == VERIFIED["verified_on"]


def test_an_underived_building_says_nothing_about_a_hand_check():
    out = readiness({"occupancy_state": "clear"})
    assert out["readiness_verified_url"] == ""
    assert out["readiness_verified_on"] == ""


def test_the_loader_refuses_a_row_that_is_not_toward_occupied(tmp_path):
    """Only ever toward occupied. Overriding toward occupied costs a building
    nobody looks at; overriding toward available costs a day somebody spends
    on a building that was never free."""
    csv = tmp_path / "gt.csv"
    csv.write_text(
        "label_type,notes,bbl,source_url,verified_on\n"
        "readiness_available,nobody is here,1000000001,https://e.org/a,2026-10-07\n"
        "readiness_occupied,a shelter runs it,1000000002,https://e.org/b,2026-10-07\n")
    got = load_verified_occupancy(csv)
    assert set(got) == {"1000000002"}


def test_the_loader_drops_an_entry_that_cannot_be_checked(tmp_path):
    """No link, no date or no basis sentence. An override is invisible once
    applied, so a stale entry looks exactly like a correct one."""
    csv = tmp_path / "gt.csv"
    csv.write_text(
        "label_type,notes,bbl,source_url,verified_on\n"
        "readiness_occupied,,1000000001,https://e.org/a,2026-10-07\n"
        "readiness_occupied,a shelter,1000000002,,2026-10-07\n"
        "readiness_occupied,a shelter,1000000003,https://e.org/c,\n"
        "readiness_occupied,a shelter,1000000004,https://e.org/d,2026-10-07\n")
    assert set(load_verified_occupancy(csv)) == {"1000000004"}


def test_the_real_file_carries_all_nine_and_every_one_is_checkable():
    """The backfill itself. These BBLs are the app's old READINESS_OVERRIDES."""
    got = load_verified_occupancy()
    expected = {
        "4003640004", "3001460014", "4010030011", "1008017503", "1016760011",
        "3001727501", "1019630009", "1018290026", "1012537504",
    }
    assert expected <= set(got), f"missing: {expected - set(got)}"
    for bbl, e in got.items():
        assert e["url"].startswith("http"), bbl
        assert len(e["basis"]) > 20, bbl
        assert e["verified_on"].count("-") == 2, bbl


def test_apply_readiness_corrects_the_feature_the_map_draws():
    """Applied to the properties bag itself, so the table, the filter and the
    polygon layer all read one answer. The app-side list split them."""
    feats = [{"properties": {"bbl": "4003640004", "occupancy_state": "clear",
                             "segment": "transient"}},
             {"properties": {"bbl": "9999999999", "occupancy_state": "clear",
                             "segment": "transient"}}]
    counts = apply_readiness(feats)
    assert feats[0]["properties"]["readiness_state"] == "occupied"
    assert "Comfort Inn" in feats[0]["properties"]["readiness_basis"]
    assert feats[1]["properties"]["readiness_state"] == "available"
    assert counts["occupied"] >= 1


def test_apply_readiness_reads_the_file_when_nobody_passes_one():
    """A caller cannot forget the hand checks — that is how they went nine
    weeks doing nothing for anyone not looking through the browser."""
    feats = [{"properties": {"bbl": "3001727501", "occupancy_state": "clear"}}]
    apply_readiness(feats)
    assert feats[0]["properties"]["readiness_state"] == "occupied"
    assert feats[0]["properties"]["readiness_verified_url"]


def test_a_hand_check_never_publishes_the_app_s_field_name():
    """readiness_override belongs to the app. The published-fields test in the
    app repo fails if the pipeline starts emitting it, and that entry exists
    to catch a producer inventing the provenance of a hand check."""
    out = readiness({"occupancy_state": "clear"}, verified=VERIFIED)
    assert "readiness_override" not in out
