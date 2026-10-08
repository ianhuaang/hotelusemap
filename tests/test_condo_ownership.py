"""The Condos filter hides separately owned units, not condominium regimes.

Three buildings carry the whole argument. All three are condominium regimes
on 7501 billing lots, so `_is_condo` is true of every one of them and cannot
tell them apart; what separates them is whether anyone sold the units.

    175 Water Street        thirty of thirty-one office units traded, but
                            Finance still carries the original declarant as
                            owner — only the sale count knows
    200 East 89th Street    board-managed, and quiet enough in any given
                            decade that the sale count could miss it — only
                            the owner name knows
    1980 Amsterdam Avenue   fourteen-unit rental, one LLC, nothing ever sold
                            — neither test fires, and it should not be hidden

The first two are the two halves of the rule, each shown failing where the
other holds. The third is the building the old rule got wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.build_geojson import (  # noqa: E402
    CONDO_UNIT_SALE_THRESHOLD,
    _has_separately_owned_units,
    _is_condo,
    _sales_address_key,
    _units_sold_for,
)

WATER = {
    "bbl": "1000717501",
    "address": "175 WATER STREET",
    "bldgclass": "RC",
    "ownername": "AMERICAN INTERNATIONAL RLTY CORP",
}
EAST_89 = {
    "bbl": "1015347501",
    "address": "200 EAST 89 STREET",
    "bldgclass": "RM",
    "ownername": "MONARCH CONDO BD/MGRS",
}
AMSTERDAM = {
    "bbl": "1021177501",
    "address": "1980 AMSTERDAM AVENUE",
    "bldgclass": "RM",
    "ownername": "1980 AMSTERDAM AVE ASSOCIATES LLC",
}


@pytest.mark.parametrize("record", [WATER, EAST_89, AMSTERDAM],
                         ids=["175-water", "200-e-89", "1980-amsterdam"])
def test_all_three_are_condominium_regimes(record):
    """The old test cannot separate them, which is the whole problem."""
    assert _is_condo(record) is True


def test_175_water_is_hidden_on_its_sale_count():
    """Thirty units have traded. The owner name would say single-owner."""
    assert _has_separately_owned_units(WATER, units_sold=30) is True
    # The name half alone gets this one wrong — that is why both exist.
    assert _has_separately_owned_units(WATER, units_sold=0) is False


def test_200_east_89_is_hidden_on_its_owner_name():
    """Caught with no sales at all: Finance names the board of managers."""
    assert _has_separately_owned_units(EAST_89, units_sold=0) is True


def test_1980_amsterdam_is_shown():
    """One LLC, nothing ever sold. This is the building the old rule hid."""
    assert _has_separately_owned_units(AMSTERDAM, units_sold=0) is False


def test_the_threshold_is_three_sold_units():
    """Two is a sponsor closing on a couple of units; three is a pattern."""
    assert _has_separately_owned_units(AMSTERDAM, units_sold=2) is False
    assert _has_separately_owned_units(
        AMSTERDAM, units_sold=CONDO_UNIT_SALE_THRESHOLD) is True


def test_a_rental_with_no_owner_on_file_is_not_hidden():
    """The regime precondition. Without it the placeholder test would hide
    every ordinary building whose owner Finance happens not to carry."""
    rental = {
        "bbl": "1012345678",
        "address": "1 EXAMPLE STREET",
        "bldgclass": "C7",
        "ownername": "UNAVAILABLE OWNER",
    }
    assert _is_condo(rental) is False
    assert _has_separately_owned_units(rental, units_sold=0) is False


def test_sro_is_not_read_as_a_condominium():
    """RS is the one R class that is not a condominium, and it is a class the
    prospect list is full of."""
    sro = {
        "bbl": "1012340056",
        "address": "2 EXAMPLE STREET",
        "bldgclass": "RS",
        "ownername": "NAME NOT ON FILE",
    }
    assert _is_condo(sro) is False
    assert _has_separately_owned_units(sro, units_sold=99) is False


def test_sale_addresses_match_pluto_addresses():
    """The two datasets spell the same building differently — ordinals, unit
    suffixes and street-type words all have to come off before they agree."""
    assert (_sales_address_key("1", "1534", "200 EAST 89TH STREET, 12B")
            == _sales_address_key("1", "01534", "200 EAST 89 STREET"))
    assert (_sales_address_key("1", "71", "175 WATER STREET, PH")
            == _sales_address_key("1", "0071", "175 WATER STREET"))


def test_sales_are_not_credited_to_the_rest_of_the_block():
    """Block 746 carries 313 West 22nd Street and four other condominiums.
    Counting by block would hand all of them each other's sales."""
    here = _sales_address_key("1", "746", "313 WEST 22 STREET")
    neighbour = _sales_address_key("1", "746", "300 WEST 23RD STREET, 4C")
    assert here != neighbour
    assert _units_sold_for(
        {"bbl": "1007467504", "address": "313 WEST 22 STREET"},
        {neighbour: 51}) == 0


def test_units_sold_lookup_survives_a_missing_sales_file():
    """An absent sales file falls back to the owner name rather than raising."""
    assert _units_sold_for(WATER, {}) == 0
    assert _has_separately_owned_units(EAST_89, _units_sold_for(EAST_89, {})) is True
