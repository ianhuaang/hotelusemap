"""Which file each source was actually read from, and when it was pulled.

The build stamps every record with one date: `source_pulled_on`, the vintage of
the newest PLUTO pull. For the fifteen sources refresh-data.yml pulls in a
single job that is honest — they all land the same morning, and a gate refuses
to publish unless every one of them completed.

It was not honest for the sources CI never pulled. `enrich_current_use.py`,
`enrich_hotel_names.py` and `enrich_hclass_hotels.py` are the only scripts that
call Google Places, and none of them appeared in the workflow; their output was
committed to data/raw instead, so every weekly build read the same Places sweep
and stamped it with that Monday's date. On 28 Sep 2026 the published file
carried current-use data pulled on the 23rd and said nothing about it.
`enrich_city_record.py` had no route into a run at all. All four are in
refresh-data.yml as of 30 Sep 2026, behind the same completion gate as the
fifteen city pulls, so this table's `ci` column is now True throughout — and
the check that matters is that it stays that way.

Which would matter less if those fields were decorative. current_use_conflict is
the largest single term in the score at -35, current_use_label titles the
Current use panel, and hotel_name titles the building.

So: one resolver, used both to open a file and to report on it. The manifest
cannot claim a date the loaders did not read, because it is the loaders' own
answer. That is the same rule _fields.js and SCORE_SIGNALS already follow in the
app repo, and for the same reason — a second hand-kept list drifts.
"""

import re
from datetime import date
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_PROCESSED, DATA_RAW

TODAY = date.today().strftime("%Y%m%d")

_VINTAGE = re.compile(r"_(\d{8})$")

# Every input the build reads, and whether refresh-data.yml regenerates it.
#
# `ci` is the field worth reading twice. False does not mean "stale" — it means
# nothing on a schedule keeps it fresh, so its date is whatever the last manual
# run produced and only this table will say so.
SOURCES = [
    # key                    prefix                      dir             ci
    ("pluto",                "pluto",                    DATA_RAW,       True),
    ("hpd",                  "hpd",                      DATA_RAW,       True),
    ("footprints",           "footprints",               DATA_RAW,       True),
    ("sales",                "sales",                    DATA_RAW,       True),
    ("permits",              "permits",                  DATA_RAW,       True),
    ("hotel_licenses",       "hotel_licenses",           DATA_RAW,       True),
    ("dob_occupancy",        "dob_occupancy",            DATA_RAW,       True),
    ("coo",                  "coo",                      DATA_RAW,       True),
    ("acris_owners",         "acris_owners",             DATA_RAW,       True),
    ("hpd_registrations",    "hpd_registrations",        DATA_RAW,       True),
    ("hpd_violations",       "hpd_violations",           DATA_RAW,       True),
    ("ecb_violations",       "ecb_violations",           DATA_RAW,       True),
    ("tax_liens",            "tax_liens",                DATA_RAW,       True),
    ("lis_pendens",          "lis_pendens",              DATA_RAW,       True),
    ("landmarks",            "landmarks",                DATA_RAW,       True),
    ("tax_benefits",         "tax_benefits",             DATA_RAW,       True),
    ("rent_stabilization",   "rent_stabilization",       DATA_RAW,       True),
    ("alt_addresses",        "alt_addresses",            DATA_PROCESSED, True),
    # Pulled by CI but deliberately outside its completeness gate: a build
    # without union data is worth more than no build. htc_union drives a -15
    # penalty, so a silent failure here moves scores quietly — the manifest is
    # where that becomes visible.
    ("htc_union",            "htc_union",                DATA_RAW,       True),
    # Google Places. Pulled by refresh-data.yml since 30 Sep 2026 and gated
    # like the rest; before that they were committed by hand and every weekly
    # build re-read the last sweep somebody remembered to run.
    ("google_current_use",   "google_current_use",       DATA_RAW,       True),
    ("google_hotel_names",   "google_hotel_names",       DATA_RAW,       True),
    ("google_hclass_hotels", "google_hclass_hotels",     DATA_RAW,       True),
    # City Record shelter notices — the one source with no route into a run at
    # all until the same change.
    ("city_record_shelter",  "city_record_shelter",      DATA_RAW,       True),
    # The Certificate of Occupancy floor tables, and the one source CI cannot
    # refresh however much anybody wants it to. DOB BIS sits behind Akamai and
    # returns 403 to everything automated — curl with browser headers, headless
    # Chromium with a session established first, the servlet directly. Only a
    # headed browser gets 200, so src/pull_coo_pdfs.js opens a visible window
    # and runs on somebody's desk. The parsed output is committed so a build
    # can read it; refreshing it is a hand operation by physics, not neglect.
    ("coo_parsed",           "coo_parsed",               DATA_PROCESSED, False),
]

_DIRS = {key: (prefix, directory) for key, prefix, directory, _ in SOURCES}


def resolve(prefix, directory=None, path=None):
    """Today's file for a source, else the most recent one, else where it would go.

    Returning the would-be path when nothing matches is deliberate: the caller
    then fails with FileNotFoundError naming the file it wanted, which is a
    better error than None dereferenced three frames later.
    """
    if path is not None and Path(path).exists():
        return Path(path)
    directory = directory or _DIRS.get(prefix, (None, DATA_RAW))[1]
    today_path = directory / f"{prefix}_{TODAY}.json"
    if today_path.exists():
        return today_path
    # [0-9]* so google_current_use_cache.json cannot outrank a dated sweep:
    # "cache" sorts above every digit, and reverse=True would have picked it.
    files = sorted(directory.glob(f"{prefix}_[0-9]*.json"), reverse=True)
    return files[0] if files else today_path


def vintage(path):
    """The YYYYMMDD a pull file is stamped with, or None if it carries no date."""
    if path is None:
        return None
    m = _VINTAGE.search(Path(path).stem)
    return m.group(1) if m else None


def manifest():
    """{source: {pulled_on, file, refreshed_by_ci}} for everything the build read.

    Sources with no file on disk are reported as present-and-missing rather than
    omitted. A source that silently produced nothing is exactly the case a
    reader needs told about, and an absent key reads as "not applicable".
    """
    out = {}
    for key, prefix, directory, ci in SOURCES:
        path = resolve(prefix, directory)
        found = path.exists()
        out[key] = {
            "pulled_on": vintage(path) if found else None,
            "file": path.name if found else None,
            "refreshed_by_ci": ci,
        }
    return out


def summarise(man=None):
    """One line per source, newest last. Printed by the build so a run says it."""
    man = man or manifest()
    rows = sorted(man.items(), key=lambda kv: (kv[1]["pulled_on"] or "", kv[0]))
    lines = []
    for key, info in rows:
        when = info["pulled_on"] or "MISSING"
        mark = "" if info["refreshed_by_ci"] else "  (manual — CI never refreshes this)"
        lines.append(f"  {when}  {key}{mark}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summarise())
