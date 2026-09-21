"""Pull the Hotel Trades Council's own roster of union shops.

hotelworkers.org publishes the list its members use to find union properties.
The page renders client-side, but it feeds from one endpoint that serves the
whole roster with coordinates, which makes this a real dataset rather than a
scrape: 377 records, every one geocoded, no pagination, no key.

Two fields carry the weight.

  shopType tells you what the building is to the union — Hotel, Residence,
  Club, Shelter, Extended Stay, Timeshare. A residence or a shelter on this
  list is a former hotel whose contract survived the conversion, which is
  exactly the cohort the tool ranks as available capacity.

  latitude/longitude let us match on position instead of name. The street
  address field is empty on all 377 records, so coordinates are the only
  join available, and they turn out to be the better one.

Replaces the room-count guess in the independent-hotels tool, where 76% of
the NYC union flag came from a single rule: 100 or more rooms.
"""

import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW

SOURCE_URL = "https://hotelworkers.org/book-union.json"
TODAY = date.today().strftime("%Y%m%d")
OUTFILE = DATA_RAW / f"htc_union_{TODAY}.json"

# Everything that is not a conventional operating hotel. A union shop in one
# of these categories has usually converted away from transient use while the
# labour agreement stayed with the building.
NON_HOTEL_SHOP_TYPES = frozenset({
    "Residence", "Club", "Shelter", "Office", "Restaurant", "Audio Visual",
})


def pull() -> Path:
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "nyc-transient-capacity/0.1"}
    )
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        rows = json.loads(resp.read())

    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"Unexpected payload from {SOURCE_URL}")

    keep = []
    for r in rows:
        try:
            lon, lat = float(r["longitude"]), float(r["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        keep.append({
            "htc_id": r.get("id"),
            "name": (r.get("name") or "").strip(),
            "shop_type": r.get("shopType") or "",
            "status": r.get("covid19Status") or "",
            "location": r.get("location") or "",
            "longitude": lon,
            "latitude": lat,
        })

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(keep, indent=2))

    from collections import Counter
    print(f"Saved {len(keep)} HTC union shops -> {OUTFILE}")
    print(f"  dropped for missing coordinates: {len(rows) - len(keep)}")
    for t, n in Counter(r["shop_type"] for r in keep).most_common():
        mark = "  <- converted away from hotel use" if t in NON_HOTEL_SHOP_TYPES else ""
        print(f"    {t or '(blank)':18} {n:4}{mark}")
    return OUTFILE


if __name__ == "__main__":
    pull()
