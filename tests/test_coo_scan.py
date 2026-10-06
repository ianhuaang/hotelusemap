"""Reading a floor table off a scanned certificate.

parse_coo_pdf only reads certificates carrying a text layer, which is the
modern DOB NOW ones. The legacy BIS archive is scanned images, and it is most
of the archive: 113 of the 196 buildings with certificates on disk had nothing
readable at all, among them 51 Clark Street at 692 Class B rooms and 859 7th
Avenue at 687.

The risk in OCR here is not a missed building, it is a wrong one. These scans
degrade gradually — a hopeless one still yields a row or two of fragments —
and a floor table that confidently says "Ist .3toly 120-40 2nd t$ lt.th" is
worse than no floor table, because a reader cannot tell it is noise. So most
of what is tested here is the gate that throws those away.

Pure-function tests on the parsing. The OCR itself is not exercised.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.parse_coo_scan import (
    _floor_of, _split_columns, _trustworthy, _rows_from_lines,
)


def rows(*descs, floors=None):
    """Rows shaped the way the gate sees them."""
    floors = floors or [f"{i:02d}" for i in range(1, len(descs) + 1)]
    return [{"floor": f, "description": d} for f, d in zip(floors, descs)]


# --- finding the floor a row opens -------------------------------------------

def test_the_two_eras_use_different_words_for_a_storey():
    # 1960s-90s forms say FLOOR, the 1920s-50s ones say STORY. A parser that
    # knows only one of them reads nothing off half the archive.
    assert _floor_of("2ND FLOOR 40 J-2 SIX (6) APARTMENTS")[0] == ["02"]
    assert _floor_of("2nd Story 40 Six (6) apartments")[0] == ["02"]
    assert _floor_of("Second Story stores and offices")[0] == ["02"]


def test_ocr_misreads_the_word_floor_every_time():
    # Real output from the sample: ELOOR, FLCOR, 2NE. Matching the shape of the
    # word rather than enumerating the misreadings is what makes this hold.
    for line in ("1ST ELOOR 100 J2 ONE (1) CLASS A", "4TH FLCOR 40 J-2 NINE (9)",
                 "2NE FLOOR 40 J-2 SIX (6) CLASS A APARTMENTS"):
        got, _rest = _floor_of(line)
        assert got and got[0].isdigit(), f"no floor found in {line!r}"


def test_a_typewriter_1_and_an_ocr_I_are_the_same_glyph():
    # "Ist .3toly" is "1st Story". Corrected only in the label: an I in the
    # description is usually a letter.
    assert _floor_of("Ist Story 120 Tenement House")[0] == ["01"]
    assert "I" in _floor_of("3RD FLOOR 40 SUITE I AND II")[1]


def test_a_range_covers_every_floor_in_it():
    got, _rest = _floor_of("2ND TO 5TH FLOORS 40 J-2 APARTMENTS")
    assert got == ["02", "03", "04", "05"]


def test_the_named_floors_are_not_numbers():
    for line, want in (("CELLAR BOILER ROOM", "CEL"), ("Rusement storage", "BAS"),
                       ("PENTHOUSE one apartment", "PEN"), ("ROOF water tank", "ROO")):
        assert _floor_of(line)[0] == [want], line


# --- splitting the columns ---------------------------------------------------

def test_the_live_load_and_occupancy_group_come_off_the_front():
    load, group, desc = _split_columns(" 40 J-2 SIX (6) CLASS A APARTMENTS")
    assert (load, group) == (40, "J-2")
    assert desc.startswith("SIX (6)")


def test_the_hyphen_in_J2_is_optional_because_ocr_drops_it():
    assert _split_columns("40 J2 APARTMENTS")[1] == "J-2"


# --- what the row means ------------------------------------------------------

# These read the shared rule now. This module used to carry its own, shorter
# one: it knew the occupancy group, which the other did not, and knew nothing
# of SRO rooms, single-room occupancy or ancillary space, which the other did.
# A lobby on a scanned certificate counted as capacity and the same lobby on a
# text one did not. The behaviour worth keeping from this side — the group
# deciding before the prose — survived into the shared rule, and these are
# what say so.
def _classify(description, group):
    from src.parse_coo_pdf import floor_kind

    return floor_kind(description, None, group)


def test_the_occupancy_group_decides_before_the_prose():
    # J-1 transient, J-2 permanent. It is the certificate's own answer and it
    # survives OCR better than a sentence does.
    assert _classify("ROOMS", "J-1") == "transient"
    assert _classify("ROOMS", "J-2") == "residential"


def test_prose_is_read_only_where_there_is_no_group():
    assert _classify("THIRTY (30) HOTEL ROOMS", "") == "transient"
    assert _classify("SIX (6) CLASS A APARTMENTS", "") == "residential"
    # "other" split: a boiler room is a finding, an unreadable row is not.
    assert _classify("BOILER ROOM AND STORAGE", "") == "ancillary"


# --- the gate ----------------------------------------------------------------

def test_a_fragment_is_not_a_floor_table():
    # The failure this exists to stop: one row of noise published as fact.
    assert not _trustworthy(rows("Ist .3toly 120-40 2nd t$ lt.th 40 on Tenement"))


def test_a_ground_floor_alone_answers_nothing_anyone_is_asking():
    # The whole question is what is upstairs. Two storeys above ground, or it
    # is not worth showing.
    assert not _trustworthy(rows("LOBBY AND STORES", "OFFICE", "BOILER ROOM",
                                 floors=["01", "01", "CEL"]))


def test_noise_rows_outvoting_real_ones_fails():
    assert not _trustworthy(rows("APARTMENTS", "x8 ?? t$ q", "5ol-50? Lezirgton",
                                 "r#4 ,, ..", "zz 11 //"))


def test_floors_scattered_to_46_are_column_numbers_not_floors():
    # Real output: a 7-row table claiming floors 1, 2, 12, 21, 31, 45, 46. The
    # high numbers are live loads read as floor labels after the scan's grid
    # lines failed to survive.
    assert not _trustworthy(rows(*["APARTMENTS"] * 7,
                                 floors=["01", "02", "12", "21", "31", "45", "46"]))


def test_a_real_table_passes():
    assert _trustworthy(rows("BOILER ROOM AND STORAGE", "LOBBY RETAIL STORES",
                             "SIX (6) CLASS A APARTMENTS", "NINE (9) APARTMENTS",
                             "TWELVE (12) HOTEL ROOMS",
                             floors=["CEL", "01", "02", "03", "04"]))


def test_a_genuine_wide_range_still_passes():
    # "2ND TO 20TH FLOORS" is one line that legitimately covers twenty storeys,
    # so the density check has to leave room for it.
    assert _trustworthy(rows(*["CLASS A APARTMENTS"] * 20,
                             floors=[f"{n:02d}" for n in range(1, 21)]))


# --- two uses on one floor ---------------------------------------------------

def test_both_counts_on_a_floor_survive():
    # "SIX (6) CLASS A APARTMENTS" and "FOURTEEN (14) ROOMING UNITS" are the
    # same floor on two lines. Folding the second into the first loses a count,
    # and the interleaving of rooms through flats is the thing that decides
    # whether the rooms can be operated as a block.
    parsed, _groups = _rows_from_lines([
        "PERMISSIBLE USE AND OCCUPANCY",
        "2ND FLOOR 40 J-2 SIX (6) CLASS A APARTMENTS",
        "J-2 FOURTEEN (14) ROOMING UNITS",
    ])
    second = [r for r in parsed if r["floor"] == "02"]
    assert len(second) == 2, "the second entry on the floor was folded away"
    assert {r["units_described"] for r in second} == {6, 14}


def test_a_continuation_without_its_own_count_joins_the_row_above():
    parsed, _groups = _rows_from_lines([
        "PERMISSIBLE USE AND OCCUPANCY",
        "CELLAR BOILER ROOM",
        "AND STORAGE AND LOCKERS",
    ])
    assert len(parsed) == 1
    assert "STORAGE" in parsed[0]["description"]


def test_the_zoning_lot_description_below_the_table_is_not_read():
    # Metes and bounds are full of numbers and the word STORY never appears,
    # but "EAST 21'-8"" has bitten parsers before.
    parsed, _groups = _rows_from_lines([
        "PERMISSIBLE USE AND OCCUPANCY",
        "2ND FLOOR 40 J-2 SIX (6) CLASS A APARTMENTS",
        "OPEN SPACE USES PARKING SPACES LOADING BERTHS",
        "1ST FLOOR 100 beginning at a point 21'-7\" SOUTH 14'-0\"",
    ])
    assert [r["floor"] for r in parsed] == ["02"]


# --- which certificate a building is described by -----------------------------

def test_a_clean_reading_is_not_displaced_by_a_scan_of_equal_standing():
    """A building with both kinds of certificate keeps the one read from text.

    Most buildings have several certificates, and now they can arrive from two
    parsers. Both describe something real, but one was lifted from a field and
    the other from a photograph of a typewriter. Ranked only by date, a scan
    one year newer would quietly replace a clean table with "ROOMING UNITS
    FOURTEEN (14)".
    """
    from src.build_geojson import _coo_rank

    def effective(row):
        raw = str(row.get("effective_date") or "")
        p = raw.split("/")
        return f"{p[2]}-{p[0].zfill(2)}-{p[1].zfill(2)}" if len(p) == 3 else ""

    text = {"is_current": True, "effective_date": "01/01/1990"}
    scan = {"is_current": True, "effective_date": "01/01/1995", "source": "ocr"}
    assert _coo_rank(text, effective) > _coo_rank(scan, effective)

    # But a current scan still beats a superseded text layer: being about the
    # building as it stands now outranks how cleanly it was read.
    old_text = {"is_current": False, "effective_date": "01/01/1990"}
    assert _coo_rank(scan, effective) > _coo_rank(old_text, effective)
