"""Re-apply the HTC roster join to an existing buildings GeoJSON, in place.

build_geojson.py rebuilds the file from the pipeline and writes only what the
pipeline carries, so running it drops the post-processing applied afterwards —
the live file and the newest pipeline build already differ. This re-runs the
roster join and the roster-derived current-use rule on their own, which is what
you want when that logic changed and the underlying records did not.

Imports the logic from build_geojson rather than restating it, so the file this
writes and the file a full rebuild writes cannot disagree.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.build_geojson import (
    apply_roster_current_use,
    load_htc_union,
    match_htc_union,
)

HTC_FIELDS = {
    "htc_union": False,
    "htc_union_name": "",
    "htc_shop_type": "",
    "htc_union_status": "",
    "htc_union_distance_m": None,
    "htc_converted_use": False,
    "htc_match_basis": "",
}


def apply(path: Path) -> None:
    data = json.loads(path.read_text())
    features = data["features"]

    roster = load_htc_union()
    if not roster:
        raise SystemExit("No HTC roster on disk — run src/pull_htc_union.py")

    # Clear anything a previous run left, so a shop that moved or a match that
    # no longer holds does not survive as a stale flag.
    before_union = 0
    before_conflict = 0
    for f in features:
        p = f["properties"]
        before_union += bool(p.get("htc_union"))
        before_conflict += bool(p.get("current_use_conflict"))
        if p.get("current_use_source") == "htc_roster":
            for key in ("current_use_conflict", "current_use_label",
                        "current_use_name", "current_use_confidence",
                        "current_use_basis", "current_use_source"):
                p[key] = False if key == "current_use_conflict" else ""
        p.update(HTC_FIELDS)

    hits = match_htc_union(features, roster)
    applied = apply_roster_current_use(features)

    inside = sum(1 for f in features
                 if f["properties"].get("htc_match_basis") == "footprint")
    after_conflict = sum(1 for f in features
                         if f["properties"].get("current_use_conflict"))
    path.write_text(json.dumps(data))

    print(f"{path.name}")
    print(f"  union matches      {before_union} -> {hits}")
    print(f"    inside footprint {inside}")
    print(f"    proximity only   {hits - inside}")
    print(f"  current-use conflicts {before_conflict} -> {after_conflict} "
          f"({applied} added by the roster)")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        raise SystemExit("usage: python3 src/apply_htc_match.py <buildings.geojson>")
    apply(target)
