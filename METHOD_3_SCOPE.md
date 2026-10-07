# Method 3 — OTA listing check (scope, not built)

A final screen on the buildings that survive deal readiness as **Available**,
to catch an operating hotel the certificate and Places both missed.

Nothing here has been measured. Method 2's numbers in this document are
measured; every number about Method 3 is a plan.

## Why it is worth doing at all

Method 2 found ten operating hotels sitting in the no-operator segment —
the Beekman Tower, The Court, AKA Sutton Place, the michelangelo, The MAve.
Those are not obscure buildings, and nothing in the city record or the
certificate said they were trading. If Places alone found ten, the assumption
that the Available list is clean does not survive contact.

An OTA index is a different kind of source from everything upstream. The city
records say what a building may be; Places says what registered a business
there; an OTA says **somebody is selling rooms in it tonight**, which is the
only one of the three that is present-tense by construction.

## Volume

Measured against the 6 Oct build with Method 2 applied:

| Set | Count |
|---|---|
| Available, all segments | **222** |
| Available, no-operator segment | 48 |
| Available with 20+ Class B rooms | 23 |

222 lookups is the whole job, once, and roughly that again on each refresh.
This is a very small API consumer — which is the single most important fact
for choosing a provider.

## Recommended aggregator

**Amadeus Self-Service — Hotel List by Geocode**
(`/v1/reference-data/locations/hotels/by-geocode`)

Why this one:

- **Self-serve.** An account and a key, no partner agreement and no commercial
  negotiation. Booking.com's Demand API and Expedia's Rapid API both index far
  more inventory and both require an approved partnership; for 222 lookups a
  quarter that approval is more work than the entire rest of this method.
- **Property-level by construction.** It returns hotel properties with a
  `hotelId`, name and geocode. It is not an index of rooms or of listings, so
  the single biggest failure mode — an individual flat sold on a booking site
  reading as a hotel — is excluded by the shape of the source rather than by a
  guard we have to get right. That is exactly the error that made 19 of
  Method 2's 23 raw "hotels" wrong.
- **Searches the way we need.** Radius-from-a-point, which is how our
  buildings are addressed. Address matching is what broke Text Search.

**Cross-check: TripAdvisor Content API** (`/location/search`, lat/long). Free
tier, property-level, and independent of Amadeus's supply. Worth wiring only
if Amadeus's NYC coverage turns out thin — which the probe is there to find
out.

**Not recommended:** scraping Google Hotels through SerpApi or similar. It
would work, it costs per search, and it puts a scraper in a pipeline that
currently has none.

## Setup it needs

1. Amadeus for Developers account; create a Self-Service app; take the API
   key and secret.
2. OAuth2 `client_credentials` to mint a bearer token; tokens are short-lived
   so the script refreshes rather than caching to disk.
3. **Move to the Production environment before trusting a single result.** The
   Test environment serves a reduced, partly synthetic dataset. A probe run
   against Test would produce a confident-looking miss rate that means
   nothing. Production needs a payment method on the account.
4. `AMADEUS_API_KEY` / `AMADEUS_API_SECRET` into `.env.local` and into the
   repo's Actions secrets, beside `GOOGLE_API_KEY`.

## Cost at our volume

222 calls per refresh. On any published self-serve tier this is inside the
free allowance or close enough to it that the per-call cost is not the
consideration — **verify the current free quota and per-call price when the
account is created rather than trusting a figure quoted here**, because they
move and this document will age.

The real cost is the setup above, and the standing cost is one more key to
rotate and one more source that can fail silently. Budget the decision on
that, not on the per-call price.

## The probe, when it is built

Run over the 222 Available buildings, one geocode query each, and report the
same way Method 2 was reported: measured counts, per building, with what came
back, and a building that returns nothing says so.

Four guards. The first three are Method 2's, which were each written against a
specific wrong answer; the fourth is the one this method needs on its own.

1. **Geo-verify within 30m** of the footprint centroid. At 150m the answer is
   as often the building next door — 37-35 21 Street answered "library" on a
   branch 67m away.
2. **Property type must be a hotel type.** Reject anything the aggregator
   classifies as an apartment, aparthotel, vacation rental or residence.
3. **Reject unit-shaped names.** A listing called "2BR/2BA w/ 4 Beds near
   Times Square" or "91-2A Stylish 3BR WD GYM" is a flat. Pattern: bedroom
   or bathroom counts, unit numbers, "studio", "sleeps N", a host's name.
4. **House number must agree.** The matched property's own street address is
   compared against the building's address and its alt-address list. This is
   the guard Method 2 did not have, and it is what would have caught "Lovely
   2 Bedroom In Brooklyn Sleeps 5" being returned for a Sutton Place
   footprint.

A building only counts as an OTA match when all four hold. Everything else
stays Available and unchanged — this method can take a building off the list,
never put one on it.

## What it cannot do

It cannot show a building is empty. An absence from an OTA index means the
building is not being sold as a hotel *through that supply*, which for a
genuinely independent property may simply mean it sells direct. A miss is not
evidence, and the probe must not report one as though it were.
