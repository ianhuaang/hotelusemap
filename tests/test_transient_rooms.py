"""The one room count, and the buildings that showed four fields disagreeing.

Records are cut down from the 2026-10-04 build to the fields the count reads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.enrich import _transient_rooms


def rooms(**record):
    return _transient_rooms(record)


def test_a_hotel_hpd_registers_few_rooms_in_is_counted_from_dob():
    """102 West 128 Street: HH, 9 Class B, no Class A, 27 DOB transient."""
    net, gross, stab, basis = rooms(bldgclass="HH", hpd_class_b=9, dob_transient_units=27)
    assert (net, basis) == (27, "dob_transient_units")


def test_dob_units_are_the_apartments_where_hpd_registers_apartments():
    """333 West 86 Street: H6, 219 DOB units, 216 Class A. The DOB figure is
    the apartments, not hotel rooms. 410 East 58 Street has 125 of each."""
    assert rooms(bldgclass="H6", hpd_class_a=216, hpd_class_b=5,
                 dob_transient_units=219)[::3] == (5, "hpd_class_b")
    assert rooms(bldgclass="HS", hpd_class_a=125, hpd_class_b=0,
                 dob_transient_units=125)[::3] == (0, "none")


def test_no_registration_at_all_reads_dob():
    """130 East 39th Street: RC, no Class B, 203 DOB transient units."""
    assert rooms(bldgclass="RC", hpd_class_b=0, dob_transient_units=203)[0] == 203


def test_an_old_filing_does_not_inflate_an_apartment_house():
    """11 West 67 Street: D4 with 4 Class B and 153 DOB units. A registered
    count on a non-hotel class is believed over a filing."""
    net, _, _, basis = rooms(bldgclass="D4", hpd_class_b=4, dob_transient_units=153)
    assert (net, basis) == (4, "hpd_class_b")


def test_stabilised_homes_are_not_rooms():
    """477 West 57 Street: 222 Class B, 179 of them stabilised tenancies."""
    net, gross, stab, basis = rooms(bldgclass="RM", hpd_class_b=222,
                                    rent_stab_class_b_exposure=179)
    assert (net, gross, stab, basis) == (43, 222, 179, "hpd_class_b")


def test_stabilised_never_goes_below_zero():
    assert rooms(hpd_class_b=10, rent_stab_class_b_exposure=40)[:3] == (0, 10, 10)


def test_a_hotel_with_only_a_certificate_reads_the_certificate():
    net, _, _, basis = rooms(bldgclass="H2", hpd_class_a=0, coo_dwelling_units=97)
    assert (net, basis) == (97, "coo_dwelling_units")


def test_a_certificate_is_not_rooms_where_there_are_apartments():
    """310 Riverside Drive: D4, 324 C of O dwelling units, all permanent."""
    assert rooms(bldgclass="D4", hpd_class_b=6, coo_dwelling_units=324)[0] == 6


def test_a_guess_is_not_a_room():
    """The guest-room count falls back to floors x 15; this never does."""
    net, gross, _, basis = rooms(bldgclass="H3", numfloors=12)
    assert (net, gross, basis) == (0, 0, "none")


# --- the blocker follows the room count --------------------------------------

def test_the_blocker_fires_only_when_nothing_is_left():
    """Mirrors the enrich step: stabilised rooms come off the count, and the
    building is held back only when every room was stabilised."""
    partial = rooms(hpd_class_b=134, rent_stab_class_b_exposure=1)   # 66 Madison
    whole = rooms(hpd_class_b=317, rent_stab_class_b_exposure=317)   # 143 East 23
    assert partial[0] == 133 and partial[2] == 1
    assert whole[0] == 0 and whole[2] == 317
