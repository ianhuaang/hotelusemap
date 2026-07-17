"""Test the geocoder against ground-truth addresses."""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.geocoder import geocode

GROUND_TRUTH = Path(__file__).parent.parent / "ground_truth.csv"


def _load_ground_truth():
    with open(GROUND_TRUTH) as f:
        return list(csv.DictReader(f))


def test_all_ground_truth_addresses_geocode_to_bbl():
    """Every ground-truth address must resolve to a BBL."""
    rows = _load_ground_truth()
    assert len(rows) > 0, "ground_truth.csv is empty"

    failures = []
    for row in rows:
        result = geocode(row["address"])
        if result is None or not result.bbl:
            failures.append(f"{row['name']}: {row['address']} -> no BBL")

    assert not failures, "Addresses failed to geocode:\n" + "\n".join(failures)


def test_geocode_returns_10_digit_bbl():
    """BBL should be a 10-digit string (borough + block + lot)."""
    result = geocode("222 East 39th Street Manhattan NY")
    assert result is not None
    assert len(result.bbl) == 10, f"BBL {result.bbl} is not 10 digits"
    assert result.bbl.isdigit(), f"BBL {result.bbl} is not all digits"


def test_geocode_returns_manhattan_borough():
    result = geocode("222 East 39th Street Manhattan NY")
    assert result is not None
    assert result.borough == "Manhattan"


def test_geocode_cache_hit(tmp_path, monkeypatch):
    """Cached response should be used without HTTP."""
    import src.geocoder as geo
    import json

    monkeypatch.setattr(geo, "CACHE_DIR", tmp_path)

    # Pre-populate cache with a known response
    address = "155 West 66th Street Manhattan NY"
    fake_response = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-73.98, 40.77]},
            "properties": {
                "name": "155 WEST 66 STREET",
                "borough": "Manhattan",
                "confidence": 0.9,
                "addendum": {"pad": {"bbl": "1011387503", "bin": "1028838"}},
            },
        }],
    }
    url = f"{geo.GEOSEARCH_URL}?{geo.urllib.parse.urlencode({'text': address})}"
    cache_path = geo._cache_path(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(fake_response))

    # Monkeypatch urlopen to fail — should still work from cache
    def _fail(*a, **kw):
        raise RuntimeError("should not fetch")

    monkeypatch.setattr(geo.urllib.request, "urlopen", _fail)
    r = geocode(address)
    assert r is not None
    assert r.bbl == "1011387503"
