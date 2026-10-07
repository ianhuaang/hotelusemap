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

from src.deal_readiness import apply_readiness, readiness

LOCKED_FIELDS = {
    "readiness_state", "readiness_basis", "not_ready_kind", "occupied_flag",
    "occupied_basis", "not_ready_flag", "operator_answered", "reversion_window",
}


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


def test_method_2_lodging_makes_a_building_occupied():
    """40-40 27 Street: LIC Plaza Hotel Corp, 3m from the footprint."""
    out = readiness({"occupancy_state": "clear"},
                    {"nearby_use": "lodging",
                     "nearby_use_basis": "hotel 'LIC Plaza Hotel Corp' 3m from the footprint"})
    assert out["readiness_state"] == "occupied"
    assert "LIC Plaza" in out["readiness_basis"]


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
