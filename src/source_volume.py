#!/usr/bin/env python3
"""How many rows each source returned, against what it has returned before.

The workflow already refuses to publish when a pull *fails*. It had nothing to
say when a pull *succeeds and returns less*, which is the failure that has
actually happened here:

  - city_record_shelter paged Socrata with no sort key and quietly returned 20
    rows, then 14, then fewer, with no code change in between. 17 Battery Place
    and 317 West 45th Street lost shelter notices that way, and the second was
    graded active — a building running as a shelter went back into the
    sourcing list and nothing said so.
  - htc_union is scraped from one page and sits outside the completeness gate
    on purpose. It drives a -15 penalty, so an empty scrape moves every score
    that depends on it without failing anything.
  - guestroom_works read a truncated list and lost the one building it exists
    for, while permit_count went on reporting the full total.

All three look identical downstream to "the city has no record". The only
thing that separates them is how much arrived last time.

    python3 src/source_volume.py                       # check against the seed
    python3 src/source_volume.py --previous BUILD.json # against a published build
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import provenance  # noqa: E402

# The seed, and only the seed.
#
# This used to be where the history lived, and CI wrote it with --record on
# every run. Nothing committed it back, so the file on the runner disappeared
# with the runner and the baseline never moved off these numbers. The history
# travels in the published manifest now; this is read when there is no
# published manifest to read — the first run after that change, and any local
# run. It is never written by CI.
SEED = ROOT / "data" / "source_counts.json"

# Where the history lives in the published build, beside `sources`. Provenance
# says which file a source was read from; this says how much was in it.
MANIFEST_KEY = "source_volume"

# How far a source may fall below its own baseline before this says so.
#
# Measured rather than picked. Across every dated pull on disk, the city
# sources move by under 1% between runs — dob_occupancy +0.03%, landmarks and
# hotel_licenses and rent_stabilization exactly 0.00%, hpd_registrations
# +0.73% — and the Google sweeps only grow, +18.6% and +7.1%, because they
# accumulate. In the whole set there is exactly one fall: city_record_shelter,
# 20 rows to 14, -30%, which is the bug this exists to catch.
#
# So normal is under 1% and the one real failure was 30%. A tenth is far
# outside anything a working source has done and leaves the shelter case
# caught three times over. Tighter would start reporting the city having a
# quiet week.
FLOOR = 0.90

# Below this many rows lost, a percentage is noise rather than signal:
# google_hclass_hotels has four rows, so one row is 25% of it. A source that
# small cannot crater in a way that matters without losing at least three.
MIN_DROP = 3

# How many past runs the baseline is drawn from. The median of these, not the
# last one — so a single bad run cannot become the standard the next run is
# judged against, which is how this kind of check normally fails.
WINDOW = 8


def count_rows(path: Path) -> int | None:
    """Rows in a pulled file, whatever shape it arrived in."""
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("records", "features", "notices", "rows", "results"):
            if isinstance(data.get(key), list):
                return len(data[key])
        return len(data)
    return None


def measure() -> dict[str, int]:
    """This run's row count for every registered source."""
    counts = {}
    for key, prefix, directory, _ci in provenance.SOURCES:
        n = count_rows(provenance.resolve(prefix, directory))
        if n is not None:
            counts[key] = n
    return counts


def _read_manifest_history(previous: Path | None) -> dict[str, list] | None:
    """The volume history carried by a previously published build."""
    if not previous or not Path(previous).exists():
        return None
    try:
        data = json.loads(Path(previous).read_text())
    except (json.JSONDecodeError, OSError):
        return None
    hist = data.get(MANIFEST_KEY)
    return hist if isinstance(hist, dict) else None


def _history(previous: Path | None = None) -> dict[str, list]:
    """Prefer the last published manifest; fall back to the seed.

    The fallback is what makes the first run after this change work, and what
    lets this be run on a laptop. Once a build has published a manifest the
    seed stops being consulted.
    """
    from_manifest = _read_manifest_history(previous)
    if from_manifest is not None:
        return from_manifest
    if not SEED.exists():
        return {}
    try:
        return json.loads(SEED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def baseline(entries: list[dict]) -> int | None:
    """The median of the recorded runs, or None with nothing to go on."""
    rows = [e["rows"] for e in entries[-WINDOW:]
            if isinstance(e, dict) and isinstance(e.get("rows"), int)]
    return int(statistics.median(rows)) if rows else None


def advance(previous: Path | None, counts: dict[str, int],
            today: str | None = None) -> dict[str, list]:
    """The history a build should publish: the last one, plus this run.

    Appended whatever the verdict. A run that fell is part of the record and
    the manifest should say so; the median over the window is what stops it
    becoming the standard the next run is judged against.
    """
    # Registered sources only. The seed carries a _README saying it is a seed,
    # which is right there and wrong in a published manifest — the dry run
    # found it riding into the build as a 25th "source". Filtering on the
    # registry also means a source retired from SOURCES stops being carried
    # forward run after run.
    known = {k for k, _p, _d, _ci in provenance.SOURCES}
    hist = {k: list(v) for k, v in _history(previous).items() if k in known}
    stamp = today or date.today().strftime("%Y%m%d")
    for key, n in counts.items():
        entries = [e for e in hist.get(key, []) if e.get("date") != stamp]
        entries.append({"date": stamp, "rows": n})
        hist[key] = entries[-WINDOW:]
    return hist


def check(counts: dict[str, int] | None = None,
          previous: Path | None = None) -> list[dict]:
    """Every source against its baseline. Returns the ones that fell."""
    counts = measure() if counts is None else counts
    hist = _history(previous)
    gated = {k: ci for k, _p, _d, ci in provenance.SOURCES}
    alerts = []

    print(f"\nSource volume against baseline (floor {FLOOR:.0%}, "
          f"median of last {WINDOW} runs)")
    print("  " + "-" * 74)
    for key, _prefix, _dir, _ci in provenance.SOURCES:
        now = counts.get(key)
        base = baseline(hist.get(key, []))
        if now is None:
            print(f"  {'--':>9}  {key:24} not read this run")
            continue
        if base is None:
            print(f"  {now:>9,}  {key:24} no baseline yet — recording")
            continue
        ratio = now / base if base else 1.0
        dropped = base - now
        if ratio < FLOOR and dropped >= MIN_DROP:
            alerts.append({"source": key, "rows": now, "baseline": base,
                           "ratio": ratio, "gated": gated.get(key, True)})
            print(f"  {now:>9,}  {key:24} FELL from {base:,} ({ratio:.0%})")
        else:
            print(f"  {now:>9,}  {key:24} ok ({ratio:+.0%} of {base:,})".replace("+", ""))

    if not alerts:
        print("\n  Every source returned what it has been returning.")
    return alerts


def main() -> None:
    # --previous points at the last published build, which is where the
    # history is. Without it the seed is used and the run still gates, it
    # just gates against numbers that stopped moving.
    previous = None
    if "--previous" in sys.argv:
        previous = Path(sys.argv[sys.argv.index("--previous") + 1])
        if not previous.exists():
            print(f"  no previous build at {previous} — falling back to {SEED.name}")

    counts = measure()
    alerts = check(counts, previous=previous)

    # htc_union is pulled by CI but deliberately outside the completeness
    # gate: a build without union data is worth more than no build. That
    # policy is about a pull that fails, and it is kept. What it must not mean
    # is that an empty scrape passes in silence, because the -15 penalty it
    # drives moves scores either way.
    fatal = [a for a in alerts if a["gated"] and a["source"] != "htc_union"]
    soft = [a for a in alerts if a not in fatal]
    for a in soft:
        print(f"::warning title=Source returned less than usual::"
              f"{a['source']} returned {a['rows']:,} rows against a baseline of "
              f"{a['baseline']:,} ({a['ratio']:.0%}). Not fatal by policy, but "
              f"whatever it feeds is running on less than it was.")
    if fatal:
        for a in fatal:
            print(f"::error title=Source returned less than usual::"
                  f"{a['source']} returned {a['rows']:,} rows against a baseline "
                  f"of {a['baseline']:,} ({a['ratio']:.0%}).")
        print("\nNot publishing a build on a source that quietly shrank.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
