#!/usr/bin/env python3
"""Whether a building is one somebody could have a conversation about today.

Four states, derived once in the producer so the app shows one verdict rather
than four half-verdicts a reader has to reconcile:

    available      nothing in the way
    occupied       somebody is running it
    not_ready      a specific, named condition
    undetermined   we looked and could not tell

The field names here are a contract with the app and are not free to change:
readiness_state, readiness_basis, not_ready_kind, occupied_flag,
occupied_basis, not_ready_flag, operator_answered, reversion_window,
places_claim, readiness_verified_url, readiness_verified_on. The app reads
the first three and keys its filter off readiness_state; the rest are
published as the inputs behind it.

Not readiness_override. That name belongs to the app, which writes it onto a
feature it corrected in the browser, and the published-fields test in the app
repo fails if the pipeline ever starts emitting it -- a producer inventing the
provenance of a hand check is the thing that entry exists to catch. The two
fields above are this side's own, and they say the same thing about a row in
ground_truth.csv that readiness_override says about an entry in the app.

One source cannot both make a claim and corroborate it. In the no-operator
segment a Google Places reading on its own no longer takes a building off the
list: it opens a claim, src/enrich_web_use.py --corroborate puts that claim to
the open web, and only a confirmation closes it as occupied. Contradicted and
not-found both land on undetermined, which keeps the building out of the
prospecting view without asserting something nothing has established. See
places_claim.

Two things this deliberately does not do. It does not rank or date anything —
the watch list that ordered buildings by how long a condition had stood was
cut, and with it not_ready_since and not_ready_trackable. And it never fails
optimistic: anything unrecognised is undetermined, never available, because
the one error that costs real time is a building nobody has established
arriving in the clean prospecting view wearing a clean bill of health.

Residential occupancy is not an occupier. 237 Madison Avenue is a block of
flats with 157 Class B rooms and 229 Duffield Street is another with 130, and
both are exactly what this list is for — rooms that are transient-capable with
nobody running them as rooms. An institution or a hotel operator in the
building is what takes it off the table, and those are what occupied_flag
tests. Reading "someone lives here" as "someone is running it" would empty the
list of its best entries.
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LIVE_LICENCE_STATUSES = frozenset({"Active", "Ready for Renewal"})

# A use that means an institution has the building. Residential is absent on
# purpose — see the module docstring.
INSTITUTIONAL_USES = frozenset({
    "religious", "education", "government", "medical", "institutional",
    "student_housing", "supportive_housing", "shelter",
})


# Buildings somebody opened a source for and read, from the producer's own
# ground_truth.csv. Nine of them, and on every one both the city records and
# the Places sweep were wrong in the expensive direction: each arrived in the
# clean prospecting view while something was plainly running in it. 37-35 21st
# Street is a trading Comfort Inn with 94 Class B rooms, 50 Nevins is 78
# supportive units, 127 West 25th is a 200-bed shelter on floors 6 to 9.
#
# This list used to live in the app, in a const the browser applied after
# parsing. That worked and corrected nothing upstream: every weekly run
# re-derived the same nine wrong answers, and anything reading the published
# data without going through the app -- HubSpot, the exports, this module's
# own counts -- still saw them available. A correction belongs where the
# answer is made.
#
# A record is not a rule. This corrects named buildings and teaches the
# pipeline nothing; where a rule is the right answer it goes in the rules
# above, as the treatment-programme reading did.
VERIFIED_LABEL = "readiness_occupied"


def load_verified_occupancy(path=None) -> dict:
    """The hand-checked verdicts from ground_truth.csv, keyed by BBL.

    Only ever toward occupied, and the loader drops a row claiming anything
    else even if somebody writes one -- the asymmetry is the whole argument
    for letting a hand-written list outrank the pipeline at all. Overriding
    toward occupied costs a building nobody looks at. Overriding toward
    available costs a day somebody spends on a building that was never free,
    which is the error this list was built to stop.

    A row with no source url, no date or no basis sentence is dropped too.
    An override is invisible once applied -- the panel shows a readiness state
    like any other -- so a stale entry looks exactly like a correct one, and
    the only thing that makes a hand verdict better than a derived one is that
    somebody can open the link and check it.
    """
    path = Path(path) if path else ROOT / "ground_truth.csv"
    if not path.exists():
        return {}
    out = {}
    for row in csv.DictReader(path.open()):
        if (row.get("label_type") or "").strip() != VERIFIED_LABEL:
            continue
        bbl = (row.get("bbl") or "").strip()
        basis = (row.get("notes") or "").strip()
        url = (row.get("source_url") or "").strip()
        on = (row.get("verified_on") or "").strip()
        if not (bbl and basis and url and on):
            continue
        out[bbl] = {"basis": basis, "url": url, "verified_on": on}
    return out


# The no-operator segment: buildings with transient-capable rooms and nobody
# running them. The list exists for these, which is why the bar for taking one
# off it is set here and not anywhere else.
NO_OPERATOR_SEGMENT = "transient"

# A Places listing Google puts at a condominium. Reading one as an answer
# about the building is backwards twice over.
#
# It cannot argue for availability. "Somebody lives here" is not "nobody is
# running it" — this module already says so about residential readings
# generally — and a condominium says something stronger: the building may have
# as many owners as it has units, which is the opposite of a counterparty. The
# tax-lot rule decides that question from the DOF billing lot and the unit-lot
# sales, which is evidence about ownership rather than a pin on a map, and it
# is the only thing that should decide it.
#
# So a condominium reading may withhold an answer and never give one. A
# building whose only Places evidence is a condominium listing comes back
# undetermined, which is what nobody having established anything looks like.
CONDO_PLACES_TYPES = frozenset({
    "condominium_complex", "condominium", "condo_complex",
})


def _places_reading_is_about_this_building(nearby: dict) -> bool:
    """Does this Places listing belong to the building, or just stand near it.

    Proximity was the whole test and it is not one. Nearby Search returns
    whatever sits within 30m of the footprint centroid, and on a Manhattan
    block that is the building next door as often as the building: "Harrison
    Condominiums", 28m away, was answering the operator question for a
    building it is not in.

    Two ways to belong and either is enough — the pin falls inside the
    building's own footprint, or the listing's address is one the building
    answers to, main or alternate, since corner lots and through-block
    buildings have several. A listing that satisfies neither is ignored
    entirely rather than discounted: a reading that might be about the
    building next door is not weak evidence about this one, it is evidence
    about a different building, and averaging it in is how a neighbour's name
    reaches this building's panel.

    `nearby_use_attached` is computed in enrich_nearby_use, which is where the
    footprint geometry and the alternate addresses are. Absent means the file
    predates this change, and those rows keep the old behaviour so that a
    stale file does not silently empty the segment. Remove that fallback once
    a build has shipped carrying the field.
    """
    if not nearby:
        return False
    attached = nearby.get("nearby_use_attached")
    return True if attached is None else bool(attached)


def _is_condo_reading(nearby: dict) -> bool:
    """Is this Places reading a condominium listing. See CONDO_PLACES_TYPES."""
    return (nearby or {}).get("nearby_use_type", "") in CONDO_PLACES_TYPES


def _operator_off_the_sweep(p: dict) -> tuple[bool, str]:
    """Somebody running the building, according to something that is not Places.

    Ordered by how directly the source speaks to the question. A live licence
    and a union roster name an operator outright; the rest are a status the
    city or the pipeline has already established.
    """
    if p.get("hotel_license_status") in LIVE_LICENCE_STATUSES:
        name = p.get("hotel_license_name") or p.get("hotel_name") or "a licensed operator"
        return True, f"a live DCWP hotel licence held by {name}"

    if p.get("htc_union") and (p.get("htc_union_name") or "").strip():
        return True, f"the hotel union roster names {p['htc_union_name']} here"

    if (p.get("shelter_status") or "").strip():
        return True, f"an active shelter use ({p['shelter_status']})"

    # Deliberately NOT operator_name. It carries the HPD managing agent on 272
    # buildings and whatever business Google found on 681, so reading it as an
    # operator makes a letting agent into a hotelier: 237 Madison Avenue came
    # back "occupied" on LIVINGSTON MANAGEMENT SERVICES LLC and 229 Duffield
    # on WEBSTER APARTMENTS, which are the managing agents of two blocks of
    # flats. On operator_source "ground_truth" it is worse than useless — it
    # names an operator who has LEFT, which is the bug that had Sonder running
    # 20 Broad Street months after they moved out.

    # The pipeline has already concluded this one is trading.
    if p.get("segment") == "active_hotel":
        name = p.get("hotel_name") or p.get("operator_name") or "a hotel"
        return True, f"it is trading as {name}"

    # restricted_class carries three different findings in one field. SRO is a
    # legal restriction on converting rooms people live in, and belongs under
    # not_ready. A hostel and a dormitory are not restrictions at all — they
    # are somebody running the building, and calling them "restricted" files
    # an operator under a condition. 8 hostels and 4 dormitories in the
    # no-operator segment, including 117 West 70 Street, which is AMDA's.
    reason = (p.get("restricted_class_reason") or "").strip()
    head = reason.split("—")[0].strip().lower()
    if head in ("hostel", "dormitory"):
        return True, f"it is in {head} use ({reason})"

    # Places-derived like the two readings below, and kept here rather than
    # with them because it is the one that is guarded: current_use_conflict
    # only fires when the use holds the building, which _use_holds_building
    # tests against the other occupants at the address. The penalty used to
    # fire on any occupant and landed on 278 buildings, 238 of them wrongly.
    if p.get("current_use_conflict"):
        label = p.get("current_use_label") or "a use that is not transient"
        return True, f"the current-use sweep found {label}, against the room count"

    return False, ""


def _operator_off_the_places_sweep(p: dict, nearby: dict) -> tuple[bool, str]:
    """What Google Places alone says is standing in the building.

    Two readings of one source, and they are not independent of each other.
    Method 2 asks Nearby Search what is at the point; the occupant list is the
    same directory read through the current-use sweep. enrich_nearby_use tried
    corroborating one against the other and found it worthless — "checking
    Places against Places agreed with itself on all 29 and changed nothing" —
    because a wrong listing is wrong in both readings at once.

    Of the two, the occupant list is the weaker: Method 2 at least applies a
    30m radius and the guard-4 name test, and the occupant rule applies
    neither. The three names test_deal_readiness asserts Method 2 must reject
    — Digital Piano Review, Rabbi Zachary Hepner Mohel, Claudia Knafo Piano
    Studio — are all occupying a building here, on 115 East 92, 2651 Broadway
    and 610 West 111.
    """
    # Method 2. Only ever a building-level type within 30m — the guards live
    # in enrich_nearby_use, and the measured resolution rate behind them is
    # 16%, so most buildings reach this line with nothing to say.
    use = (nearby or {}).get("nearby_use") or ""
    if use in ("lodging", "institutional"):
        return True, f"Places has {nearby['nearby_use_basis']}"

    # The sweep found a building-level use that is not somewhere people live.
    occupants = p.get("current_use_occupants") or []
    if isinstance(occupants, str):
        occupants = []
    for o in occupants:
        if o.get("use") in ("ground_floor_tenant", "unknown", "other"):
            continue
        if o.get("use") in INSTITUTIONAL_USES:
            return True, f"{o.get('name') or 'an occupant'} occupies the building"
    if p.get("current_use") in INSTITUTIONAL_USES and p.get("current_use_name"):
        return True, f"{p['current_use_name']} occupies the building"

    return False, ""


def places_claim(p: dict, nearby: dict | None = None, web: dict | None = None) -> str:
    """Where a lone Places reading of this building's operator currently stands.

    Empty unless the building is in the no-operator segment AND the only thing
    saying somebody runs it is the Places sweep. Otherwise one of:

        unscreened    the web screen has not reached it yet
        confirmed     the web named the same thing at the same address
        contradicted  the web said something that argues against it
        unconfirmed   the web was asked and found nothing either way

    Only `confirmed` is an operator. The other three leave the building
    undetermined, which is the honest answer and, just as importantly, not
    `available` — see _operator_answered.
    """
    if p.get("segment") != NO_OPERATOR_SEGMENT:
        return ""
    if _operator_off_the_sweep(p)[0]:
        return ""
    if not _operator_off_the_places_sweep(p, nearby or {})[0]:
        return ""
    verdict = (web or {}).get("verdict") or ""
    if verdict in ("confirmed", "contradicted"):
        return verdict
    if verdict == "none":
        return "unconfirmed"
    return "unscreened"


def _occupied(p: dict, nearby: dict, web: dict | None = None) -> tuple[bool, str]:
    """Is somebody running this building, and what says so.

    A reading off the Places sweep and nothing else does not answer this in
    the no-operator segment. 23 buildings were occupied on one Nearby Search
    hit and 22 more on one sweep occupant, and the readings include a hotel
    called "Jordan Barbara Schwinn", a post box at 147 Graham Avenue and a
    Chinese restaurant at 411 West End Avenue. Each of those is a building the
    list exists to surface, taken off it by a single line in a business
    directory with nothing agreeing with it.

    Outside that segment the sweep still decides on its own: an active hotel
    or a partial-use building is not what the prospect list is for, and the
    cost of a wrong reading there is not a wasted day.
    """
    off_sweep, basis = _operator_off_the_sweep(p)
    if off_sweep:
        return True, basis

    from_sweep, sweep_basis = _operator_off_the_places_sweep(p, nearby)
    if not from_sweep:
        return False, ""

    claim = places_claim(p, nearby, web)
    if not claim:
        return True, sweep_basis
    if claim == "confirmed":
        where = (web or {}).get("basis") or "the open web"
        return True, f"{sweep_basis}, confirmed on the web ({where})"
    return False, ""


def _not_ready(p: dict, nearby: dict) -> tuple[bool, str, str]:
    """A named condition holding the building back: flag, machine key, phrase.

    Ordered hardest-first. A building being demolished is not merely
    restricted, and saying "restricted" about it would be true and useless.
    """
    if p.get("demolition"):
        d = p["demolition"] if isinstance(p["demolition"], dict) else {}
        when = d.get("filed") or d.get("date") or ""
        return True, "demolition", f"a demolition filing{f' ({when})' if when else ''}"

    works = p.get("guestroom_works")
    if works:
        w = works if isinstance(works, dict) else {}
        when = w.get("filed") or w.get("date") or ""
        return True, "guest_room_works", (
            f"work on the guest rooms{f' (permit {when})' if when else ''}")

    if (nearby or {}).get("nearby_use") == "lodging_closed":
        return True, "closed_hotel", f"Places has {nearby['nearby_use_basis']}, closed"

    if p.get("coo_temp_only"):
        return True, "temporary_certificate", (
            "only a temporary certificate of occupancy on record")

    if p.get("restricted_class"):
        reason = (p.get("restricted_class_reason") or "").strip()
        return True, "restricted_conversion", (
            reason or "rent-stabilisation restricts a change of use")

    blockers = p.get("blockers") or []
    if blockers:
        return True, "restricted_conversion", str(blockers[0])

    return False, "", ""


def _operator_answered(p: dict, nearby: dict, claim: str = "") -> bool:
    """Did anything establish what occupies the building, either way.

    An open Places claim is not an answer, and this is the line that stops it
    becoming one. Without it, retiring a lone Places reading would have sent
    11 of the 23 buildings it decided straight to `available` rather than to
    undetermined — 2508 Broadway, which reads as Advent Lutheran Church, and
    310 Riverside Drive, which reads as Zoe Ministries, among them. They have
    a parsed certificate or a clear sweep, and either is enough to count as
    answered on its own. Neither says who is running the building, which is
    the question the Places reading raised and nothing has yet settled.

    Ground-floor tenants are not an answer. The app already says so on the
    panel — "What occupies the rest of the building is not established" — and
    a readiness model that disagreed with that sentence would be the same
    claim made twice in two voices.

    Two things that look like answers and are not.

    occupancy_state "onrecord" is tautological in this segment. It fires when
    hpd_class_a or hpd_class_b is above zero, and a building is in the
    no-operator segment *because* it has Class B rooms — so it was true of 156
    of 221 and counted 113 buildings as answered on the strength of the
    criterion that selected them. That alone put undetermined at zero, which
    is how a model that is supposed to admit ignorance stopped admitting any.

    coo_count is the number of certificates on file, not a readable one. The
    certificate only answers anything when its floor table parsed, which is
    coo_floors — 121 buildings map-wide. Counting the records rather than the
    readings credited Method 1 with buildings it never resolved.
    """
    if claim in ("unscreened", "unconfirmed", "contradicted"):
        return False
    if (nearby or {}).get("nearby_use"):
        # A condominium listing is evidence against availability and never
        # for it, so it is allowed to withhold an answer and not to give one.
        # See CONDO_PLACES_TYPES: who owns the units is the tax-lot rule's
        # question, decided on the billing lot and the unit-lot sales.
        return not _is_condo_reading(nearby)
    # The sweep found a building-level occupant, or found a use that argues
    # with the room count. Both are about this building.
    if p.get("occupancy_state") in ("occupied", "clear"):
        return True
    # Method 1, properly: a certificate whose floor table was readable.
    return bool(p.get("coo_floors"))


def _reversion_window(p: dict) -> str:
    """open where a conversion is recent enough to be reversible, else closed.

    Published, and deliberately not read by the app: it has a control of its
    own in Overlays and does not belong in a single readiness verdict.
    """
    if not (p.get("has_reversion") or p.get("reversion_kind")):
        return ""
    return "open" if p.get("reversion_window_open") else "closed"


def readiness(p: dict, nearby: dict | None = None, web: dict | None = None,
              verified: dict | None = None) -> dict:
    """The eleven published fields for one building.

    A hand-checked verdict outranks everything below it. It is the only input
    here a person wrote, it is the only one with a url somebody can open, and
    on the nine buildings that carry one the derived answer was wrong in the
    direction that costs a day.
    """
    nearby = nearby or {}
    # Ignored entirely, as if the sweep had returned nothing for this
    # building. Done here rather than in each reader so that _occupied,
    # _not_ready, _operator_answered and places_claim cannot disagree about
    # whether a listing counts.
    if nearby and not _places_reading_is_about_this_building(nearby):
        nearby = {}
    claim = places_claim(p, nearby, web)
    occ, occ_basis = _occupied(p, nearby, web)
    nr, nr_kind, nr_basis = _not_ready(p, nearby)
    answered = _operator_answered(p, nearby, claim)

    if verified:
        state, basis = "occupied", verified["basis"]
    elif occ:
        state, basis = "occupied", occ_basis
    elif nr:
        state, basis = "not_ready", nr_basis
    elif answered:
        state, basis = "available", _available_basis(p, nearby)
    else:
        state, basis = "undetermined", _undetermined_basis(p, nearby, web, claim)

    return {
        "readiness_state": state,
        "readiness_basis": basis,
        "not_ready_kind": nr_kind if state == "not_ready" else "",
        "occupied_flag": occ,
        "occupied_basis": occ_basis,
        "not_ready_flag": nr,
        "operator_answered": answered,
        "reversion_window": _reversion_window(p),
        # Empty on all but the no-operator buildings whose only operator
        # evidence is the Places sweep. Published so the panel can say which
        # of the four it is, and so enrich_web_use can select exactly the
        # buildings a search query would change the answer for.
        "places_claim": claim,
        # Empty unless a person checked this building. Present, they are what
        # lets the panel say so and show the source: a corrected verdict that
        # cannot be told apart from a derived one is worth less than one that
        # can, because nobody can re-check it when it goes stale.
        "readiness_verified_url": (verified or {}).get("url", ""),
        "readiness_verified_on": (verified or {}).get("verified_on", ""),
    }


def _available_basis(p: dict, nearby: dict) -> str:
    use = (nearby or {}).get("nearby_use")
    if use == "residential":
        return f"Places has {nearby['nearby_use_basis']}, and nobody is running it"
    if p.get("occupancy_state") == "clear":
        return "the sweep found a building-level use and no operator"
    if p.get("coo_floors"):
        return "the certificate says what the floors are and no operator is recorded"
    return "the sweep established the building and no operator is recorded"


def _undetermined_basis(p: dict, nearby: dict | None = None,
                        web: dict | None = None, claim: str = "") -> str:
    """Why we cannot say — and for an open Places claim, what is outstanding.

    An undetermined building with no explanation reads as a gap in the data.
    These three are not a gap: something was read, and it did not hold up.
    """
    if claim:
        _, sweep_basis = _operator_off_the_places_sweep(p, nearby or {})
        if claim == "contradicted":
            where = (web or {}).get("basis") or "the open web"
            return (f"{sweep_basis} — contradicted on the web ({where}); "
                    "needs review before this building moves either way")
        if claim == "unconfirmed":
            return f"{sweep_basis}, and the web screen found nothing to confirm it"
        return f"{sweep_basis}, not yet corroborated"
    if p.get("occupancy_state") == "thin":
        return "swept, ground-floor tenants only — the rest of the building is not established"
    return "nothing on record establishes what occupies the building"


def apply_readiness(features: list, nearby_rows: dict | None = None,
                    web_rows: dict | None = None,
                    verified_rows: dict | None = None) -> dict:
    """Write the fields onto every feature. Returns the count per state.

    verified_rows is read off disk when not supplied, so a caller cannot
    forget it: build_geojson asks for readiness and gets the hand checks with
    it. Tests pass their own.
    """
    nearby_rows = nearby_rows or {}
    web_rows = web_rows or {}
    if verified_rows is None:
        verified_rows = load_verified_occupancy()
    counts = {}
    applied = 0
    for f in features:
        p = f["properties"]
        bbl = str(p.get("bbl") or "")
        hand = verified_rows.get(bbl)
        fields = readiness(p, nearby_rows.get(bbl), web_rows.get(bbl), hand)
        p.update(fields)
        counts[fields["readiness_state"]] = counts.get(fields["readiness_state"], 0) + 1
        applied += bool(hand)
    # A silent count is how the app-side list went nine weeks without anybody
    # noticing it was doing the pipeline's job. If a BBL is retired or
    # re-lotted this number drops and the build says so.
    if verified_rows:
        print(f"    hand-checked verdicts applied: {applied} of {len(verified_rows)}")
    return counts
