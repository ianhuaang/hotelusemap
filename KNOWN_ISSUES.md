# Known issues

Things that are wrong, or right only for now, written down so the next person
does not have to rediscover them. Each entry says what breaks, how you would
notice, and what fixing it would take.

This file is for defects and soft spots in shipped behaviour. Work that was
scoped and deliberately not built belongs in its own document — see
`METHOD_3_SCOPE.md`.

---

## The multi-owner condo test leans on a Finance naming convention

**Where** — `src/build_geojson.py`, `_has_separately_owned_units`.

**What it is.** The Condos filter hides buildings whose units were sold off to
separate owners. Two sources answer that, and only one of them is evidence:

- **Distinct unit lots sold**, counted from DOF rolling sales. Direct, and the
  thing we actually want to know.
- **The Finance owner name on the billing lot**, treated as multi-owner when it
  is a board, a condominium's own name, or a placeholder such as
  `UNAVAILABLE OWNER` or `NAME NOT ON FILE`.

The sale count is **positive-only by construction**. `pull_sales.py` fetches ten
years of sales above $10,000, so a condominium that sold out in 2005 and has sat
quiet since reads zero. Absence of sales is not absence of owners, which is why
the owner name is there at all.

**Why that is a weakness.** The owner-name half is carrying every quiet
condominium on its own, and it is not evidence — it is an inference from how the
assessor fills a field. Three ways it goes wrong:

- **It is a convention, not a contract.** Nothing obliges Finance to keep
  writing `UNAVAILABLE OWNER`. If the placeholder vocabulary changes, or a data
  refresh starts carrying managing agents where it used to carry placeholders,
  the test silently stops firing and quiet condominiums start appearing in the
  Available list as if they had one owner. Nothing fails. The list just gets
  wrong in the direction that costs a day.
- **It already has known false negatives.** 175 Water Street carries
  `AMERICAN INTERNATIONAL RLTY CORP`, the original declarant, thirty sold units
  later; 1295 Madison Avenue carries `1295 PROPERTY LLC` with twenty-two sold.
  Both are caught today only because their sales are recent enough to count. A
  building with that shape and no recent sales would be missed.
- **It cannot be audited from the app.** `has_separately_owned_units` is a
  boolean. Nothing published says which half decided it, so a wrong answer looks
  identical to a right one.

**How you would notice.** The count of hidden condominiums moves sharply between
refreshes without a corresponding change in sales volume. Or a building reaches
the deal team as a single-owner prospect and turns out to have a board.

**What fixing it would take.** In rough order of cost:

1. **Publish the reason.** Emit which half fired and the unit-lot count beside
   the boolean, so a wrong answer can be traced without re-deriving it. Cheap,
   and it makes the next two diagnosable.
2. **Assert the convention.** A test that fails when the share of condominium
   billing lots carrying a recognised placeholder moves more than a few points
   between refreshes. Turns a silent drift into a loud one.
3. **Replace the inference with evidence.** Count distinct current owners from
   ACRIS deeds against the condominium's unit lots, rather than reading the
   billing lot's owner field. `pull_acris_owners.py` already queries ACRIS
   Legals, but by billing BBL and keeping only the most recent deed per BBL, so
   it would need to fan out to unit lots and keep one grantee per lot. That is
   the real fix and it removes the naming convention from the chain entirely.

**Do not** fix this by lowering `CONDO_UNIT_SALE_THRESHOLD`. The threshold is 3
because two sold units is a sponsor closing on a couple in a building it still
controls. Dropping it to catch quiet condominiums trades this failure for a
worse one, in the direction that hides buildings we want to see.

---

## SRO and dormitory Class B stock is counted as Safe Hotels guest rooms

**Where** — `safe_hotels_guest_rooms`, built in `src/build_geojson.py`; a fix
exists unmerged on the `regulatory-review-fixes` branch ("Stop counting SRO and
dormitory stock as Safe Hotels guest rooms").

**What it is.** When nothing better is available the room count falls back to
HPD's Class B registration, and the building carries
`safe_hotels_room_basis: "hpd_class_b"` to say so. HPD Class B covers rooming
units, SRO units and dormitories as well as transient hotel rooms, and the
fallback does not separate them. Every Class B room becomes a guest room.

**Why that is wrong.** A Class B room occupied by a permanent, rent-stabilised
tenant is not a room anyone can sell a night in. It is somebody's home, and the
tenancy is the thing that makes it unavailable.

477 West 57 Street is the clearest case on the list. It is the Dorothy Ross
Friedman Residence, run by the Actors Fund as nonprofit housing:

    hpd_class_b              222
    rent_stabilized_units    179   (DHCR, 2023)
    safe_hotels_room_basis   hpd_class_b
    safe_hotels_guest_rooms  222

So the building is published as 222 guest rooms when 179 of those rooms have
stabilised tenants in them. The number is not a stale layout — HPD's
registration is current, and the four DOB alterations filed in March 2026 all
state no change to use, egress or occupancy — it is a current count of the
wrong thing.

**How you would notice.** A building ranks high on rooms and turns out on
inspection to be occupied housing. It sorts to the top of exactly the lists
people read first, because the miscount is largest where the SRO stock is
largest.

**What saves it today, and why that is not enough.** 477 West 57 Street does
not reach the prospect list, because the rent-stabilisation blocker puts it in
`not_ready` — see the tests in `tests/test_deal_readiness.py`. That is the
readiness gate doing its job, and it is a different question from the room
count. The published figure is still wrong, it is still what the app sorts and
filters on, and any building whose Class B stock is institutional without being
rent-stabilised has nothing holding it at all.

**What fixing it would take.** The `regulatory-review-fixes` branch is the
work; it needs rebasing and a look at what it does to the published counts
before it merges. The shape of the fix is to stop treating `hpd_class_b` as a
guest-room count on its own and to read it against what else the record says —
the DOB occupancy class, the rent-stabilised count, and the building class —
rather than falling back to it whenever a better source is missing.

**Related.** `restricted_class` does not fire here either: it reads building
classes HR/RS/H8/HH and an HPD `dobbuildingclass` containing
SINGLE ROOM OCCUPANCY, and this building is `RM` / "HEREAFTER ERECTED CLASS B".
Two rules aimed at the same stock, and neither matches this shape of it.
