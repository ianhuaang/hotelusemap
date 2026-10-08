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

## Rent-stabilised Class B rooms are still counted as Safe Hotels guest rooms

**Where** — `safe_hotels_guest_rooms`, from `_guest_rooms` in `src/enrich.py`.

**History, because the last version of this entry was wrong about it.** It said
a fix sat unmerged on `regulatory-review-fixes`. That branch was merged on
2026-09-21 (`ef2f56e`) and holds nothing further. What it fixed is the
building-class half: SRO, dormitory and hostel classes (HR, RS, H8, HH) count
zero guest rooms. It does not reach a building like 477 West 57 Street — the
Dorothy Ross Friedman Residence — which is `RM` with 222 Class B rooms, 179 of
them rent-stabilised homes (DHCR, 2023), and still publishes 222 guest rooms.

**What is fixed.** Nothing in the app reads that count as a room count any
more. `transient_rooms` (same file) is the one count the app filters, sizes
and sorts on: Class B or DOB transient units, net of the stabilised rooms the
Class A side cannot absorb. 477 West 57 Street reads 43 there.

**What is left, and why it no longer matters for sourcing.** Decided
2026-10-08: a rent-stabilised room is never a sellable room, whether or not
the Safe Hotels Act would call it a guest room. `transient_rooms` already
encodes that, so no sourcing number depends on the legal reading.
`safe_hotels_guest_rooms` still counts those rooms and is now only the
statutory figure the panel's Safe Hotels lines quote; if Legal ever reads the
Act the other way, the change is to subtract `transient_rooms_stabilized` in
`_guest_rooms` and re-run the threshold counts in `reviewer-response.txt`.
