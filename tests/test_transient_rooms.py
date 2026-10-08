"""The one room count, and the buildings that showed four fields disagreeing.

Records are cut down from the 2026-10-04 build to the fields the count reads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.enrich import _transient_rooms


def rooms(**record):
    return _transient_rooms(record)


def test_a_hotel_hpd_never_registered_is_counted_from_dob():
    """333 West 86 Street: H6, 5 Class B, 219 transient units on DOB. The
    ten-room filter dropped it on the registration."""
    net, gross, stab, basis = rooms(bldgclass="H6", hpd_class_b=5, dob_transient_units=219)
    assert (net, basis) == (219, "dob_transient_units")


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
