"""Which Places listing belongs to which building.

Nearby Search returns whatever sits within 30m of the footprint centroid, and
on a Manhattan block that is the building next door as often as the building.
These cover the test that replaced proximity: the pin is inside the footprint,
or the listing's address is one the building answers to.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.enrich_nearby_use import _norm_addr, attaches

# A small rectangle standing in for a footprint.
FOOTPRINT = {
    "type": "MultiPolygon",
    "coordinates": [[[[-73.9800, 40.7800], [-73.9790, 40.7800],
                      [-73.9790, 40.7810], [-73.9800, 40.7810],
                      [-73.9800, 40.7800]]]],
}
INSIDE = {"latitude": 40.7805, "longitude": -73.9795}
OUTSIDE = {"latitude": 40.7850, "longitude": -73.9700}

ADDRESSES = ["340 AMSTERDAM AVENUE", "205 WEST 76 STREET"]


def place(loc, addr):
    return {"location": loc, "formattedAddress": addr}


def test_a_pin_inside_the_footprint_attaches():
    attached, inside, _ = attaches(place(INSIDE, "nowhere at all"), FOOTPRINT, ADDRESSES)
    assert (attached, inside) == (True, True)


def test_an_address_the_building_answers_to_attaches():
    """The Harrison is listed at 340 Amsterdam Avenue and its pin falls 28m
    away, outside the footprint. It is still this building."""
    attached, inside, _ = attaches(
        place(OUTSIDE, "340 Amsterdam Ave, New York, NY 10024, USA"),
        FOOTPRINT, ADDRESSES)
    assert (attached, inside) == (True, False)


def test_an_alternate_address_counts():
    """A corner building is listed under whichever frontage its tenant gave."""
    attached, _, _ = attaches(
        place(OUTSIDE, "205 W 76th St, New York, NY 10023, USA"),
        FOOTPRINT, ADDRESSES)
    assert attached is True


def test_neither_test_passing_does_not_attach():
    attached, inside, _ = attaches(
        place(OUTSIDE, "1 Nowhere Street, Brooklyn, NY"), FOOTPRINT, ADDRESSES)
    assert (attached, inside) == (False, False)


def test_proximity_is_not_a_third_way_to_attach():
    """The whole point. A pin a few metres outside the footprint, at an
    address the building does not answer to, is the building next door."""
    just_outside = {"latitude": 40.78105, "longitude": -73.97895}
    attached, _, _ = attaches(place(just_outside, "9 Other Street"),
                              FOOTPRINT, ADDRESSES)
    assert attached is False


def test_a_building_with_no_footprint_can_still_attach_by_address():
    """14 buildings carry a Point geometry or none, and a Point has no
    inside. Those are exactly the buildings that need the address half."""
    attached, inside, _ = attaches(
        place(OUTSIDE, "340 Amsterdam Ave, New York, NY"), None, ADDRESSES)
    assert (attached, inside) == (True, False)


def test_a_listing_with_no_address_falls_back_to_the_footprint():
    """formattedAddress arrived with this change, so cached responses from
    before it have none. Those can still attach on geometry."""
    assert attaches({"location": INSIDE}, FOOTPRINT, ADDRESSES)[0] is True
    assert attaches({"location": OUTSIDE}, FOOTPRINT, ADDRESSES)[0] is False


@pytest.mark.parametrize("a,b", [
    ("340 Amsterdam Ave, New York, NY 10024, USA", "340 AMSTERDAM AVENUE"),
    ("205 W 76th St", "205 WEST 76 STREET"),
    ("203 W 113th St, New York, NY 10026, USA", "203 WEST 113 STREET"),
])
def test_the_two_datasets_spell_the_same_address_differently(a, b):
    assert _norm_addr(a) == _norm_addr(b)


def test_different_buildings_do_not_normalise_together():
    assert _norm_addr("340 Amsterdam Ave") != _norm_addr("342 Amsterdam Ave")
    assert _norm_addr("205 W 76th St") != _norm_addr("205 E 76th St")
