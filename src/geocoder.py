"""Address -> BBL/BIN geocoder using NYC Planning Labs GeoSearch.

Every HTTP response is cached to disk keyed by URL. The pipeline will re-run
many times and GeoSearch rate-limits.
"""

import hashlib
import json
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import certifi

from config import CACHE_DIR, GEOSEARCH_URL


@dataclass
class GeoResult:
    bbl: str
    bin: str
    address: str
    borough: str
    lat: float
    lon: float
    confidence: float
    raw: dict


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"geosearch_{key}.json"


def _fetch_cached(url: str) -> dict:
    """Fetch URL with disk cache. Returns parsed JSON."""
    cache = _cache_path(url)
    if cache.exists():
        return json.loads(cache.read_text())

    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-transient-capacity/0.1"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        data = json.loads(resp.read())

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2))
    time.sleep(0.25)  # polite rate limit
    return data


def geocode(address: str) -> GeoResult | None:
    """Geocode a street address to BBL/BIN.

    Returns None if GeoSearch can't resolve the address or if the response
    lacks BBL data (e.g. non-NYC addresses).
    """
    params = urllib.parse.urlencode({"text": address})
    url = f"{GEOSEARCH_URL}?{params}"
    data = _fetch_cached(url)

    features = data.get("features", [])
    if not features:
        return None

    feat = features[0]
    props = feat.get("properties", {})
    pad = props.get("addendum", {}).get("pad", {})
    bbl = pad.get("bbl")

    if not bbl:
        return None

    coords = feat["geometry"]["coordinates"]
    return GeoResult(
        bbl=bbl,
        bin=pad.get("bin", ""),
        address=props.get("name", address),
        borough=props.get("borough", ""),
        lat=coords[1],
        lon=coords[0],
        confidence=props.get("confidence", 0),
        raw=feat,
    )


def geocode_batch(addresses: list[str]) -> dict[str, GeoResult | None]:
    """Geocode a list of addresses. Returns {address: GeoResult|None}."""
    results = {}
    for addr in addresses:
        results[addr] = geocode(addr)
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        addr = " ".join(sys.argv[1:])
    else:
        addr = "222 East 39th Street, Manhattan, NY"

    result = geocode(addr)
    if result:
        print(f"Address: {result.address}")
        print(f"BBL:     {result.bbl}")
        print(f"BIN:     {result.bin}")
        print(f"Borough: {result.borough}")
        print(f"Coords:  {result.lat}, {result.lon}")
        print(f"Conf:    {result.confidence}")
    else:
        print(f"No result for: {addr}")
