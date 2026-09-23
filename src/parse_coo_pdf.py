"""Read the Permissible Use and Occupancy table off a Certificate of Occupancy.

The C of O feed gives a building-level dwelling-unit count. The certificate
itself gives the table that count is summed from:

    Floor | Occupancy load | Zoning use group | Building code group | Description

Two things come out of it that exist nowhere else in public data. The zoning
use group, which reply 3 to the regulatory review said is not in ZoLa, PLUTO
or the DOB feed. And the floor layout, which decides whether a building's
Class B rooms can be operated as a block: 130 transient rooms on floors 2-6 is
a hotel, the same 130 interleaved through 814 apartments is not.

LIMIT: only certificates with a text layer. Of 107 sampled, 50 parse and 57 are
scanned images returning zero characters — the split is era, not luck. DOB NOW
documents (ids like 104844581-37) carry text; legacy BIS scans (M000082568) do
not, and would need OCR. This skips them rather than guessing.

Usage:
    python3 src/parse_coo_pdf.py data/raw/coo_pdfs/1088437_104844581-41.PDF
    python3 src/parse_coo_pdf.py --all          # every PDF in data/raw/coo_pdfs
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED

PDF_DIR = DATA_RAW / "coo_pdfs"

# A row opens with its floor: CEL, SUB, MEZ, ROO, PEN, or a zero-padded number.
FLOOR_ROW = re.compile(r"^(CEL|SUB|MEZ|ROO|PEN|GAR|\d{1,3})\s+(.*)$", re.I)
# Occupancy load, then dwelling units where present, then the zoning use group.
LOAD_UG = re.compile(r"^(OG|\d+)\s+(?:(\d+)\s+)?([0-9]+[A-Z]?(?:,\s*[0-9]+[A-Z]?)*)\s+(.*)$")

# 1968 code occupancy groups. J-1 is transient, J-2 is permanent residence —
# the distinction the whole tool turns on.
TRANSIENT_MARKS = ("J-1", "HOTEL", "TRANSIENT")
RESIDENTIAL_MARKS = ("J-2", "RESIDENTIAL", "APARTMENT", "DWELLING")


def _feed_latest() -> dict:
    """Newest C of O issue date per BIN, from the Socrata feed.

    BIS only serves legacy certificates. A building whose current certificate
    was filed through DOB NOW — which blocks automated access entirely — gives
    up its superseded ones instead, and they can be a decade out of date and
    describe a different building. 37-02 10 Street returns a 2014 certificate
    for an automobile repair shop; the 381-room hotel standing there now was
    certified in 2022 and is only in DOB NOW.

    65% of what we parse has been superseded, so every record says so.
    """
    import glob
    files = sorted(DATA_RAW.glob("coo_*.json"), reverse=True)
    files = [f for f in files if f.name != "coo_pdfs"]
    if not files:
        return {}
    rows = json.loads(files[0].read_text())
    rows = rows if isinstance(rows, list) else rows.get("data", [])
    out = {}
    for r in rows:
        b, d = str(r.get("bin") or ""), _parse_date(r.get("issue_date"))
        if b and d and (b not in out or d > out[b]):
            out[b] = d
    return out


def _parse_date(s):
    from datetime import datetime
    s = (s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def parse(path: Path) -> dict:
    try:
        import pypdf
    except ImportError:
        raise SystemExit("pypdf not installed — python3 -m pip install --user pypdf")

    reader = pypdf.PdfReader(str(path))
    text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len("".join(lines)) < 200:
        return {"file": path.name, "readable": False, "reason": "scanned image, no text layer"}

    header = {}
    for i, l in enumerate(lines):
        if l.startswith("Address:"):
            header["address"] = l.split(":", 1)[1].strip()
        elif "Building Identification Number" in l:
            m = re.search(r"(\d{7})", l)
            if m:
                header["bin"] = m.group(1)
        elif l.startswith("Certificate Type:"):
            header["co_type"] = l.split(":", 1)[1].strip() or (lines[i + 1] if i + 1 < len(lines) else "")
        elif l.startswith("Effective Date:"):
            header["effective_date"] = l.split(":", 1)[1].strip()

    rows, use_groups = [], set()
    for l in lines:
        m = FLOOR_ROW.match(l)
        if not m:
            continue
        floor, rest = m.group(1).upper(), m.group(2)
        lm = LOAD_UG.match(rest)
        if not lm:
            continue
        load, units, ug, desc = lm.groups()
        for g in re.split(r",\s*", ug):
            use_groups.add(g.strip())
        upper = desc.upper()
        kind = ("transient" if any(k in upper for k in TRANSIENT_MARKS)
                else "residential" if any(k in upper for k in RESIDENTIAL_MARKS)
                else "other")
        # The numeric column is not reliably the dwelling-unit count — it
        # reports 9 units for a lobby, 27 for a gym and 53 for a lounge, which
        # is the column to its left bleeding across in the extracted text. The
        # description is trustworthy and states the count outright: "EIGHT (8)
        # CLASS A HOTEL ROOMS". Read that instead, and only where the row
        # actually describes somewhere people sleep.
        described = None
        # HOTEL alone counts: one floor reads "EIGHT (8) CLASS A HOTEL" without
        # the word rooms. Safe because the count must be a bare parenthesised
        # number, and the accessory rows carry "(NAMED FLOOR:005)" instead.
        if re.search(r"\b(ROOM|APARTMENT|DWELLING|UNIT|SUITE|HOTEL|SRO|ROOMING)", desc, re.I):
            m = re.search(r"\((\d{1,3})\)", desc)
            if m:
                described = int(m.group(1))
        rows.append({
            "floor": floor,
            "occupancy_load": None if load == "OG" else int(load),
            "units_described": described,
            "units_column_raw": int(units) if units else None,
            "use_group": ug,
            "kind": kind,
            "description": desc.strip()[:120],
        })

    # Contiguity is asked of the numbered floors only; a cellar or a roof tank
    # room says nothing about whether the rooms can be run as a block.
    numbered = [r for r in rows if r["floor"].isdigit()]
    sleeping = [r for r in rows if r["units_described"]]
    rooms_by_kind = {}
    for r in sleeping:
        rooms_by_kind[r["kind"]] = rooms_by_kind.get(r["kind"], 0) + r["units_described"]
    t_floors = sorted({int(r["floor"]) for r in numbered if r["kind"] == "transient"})
    r_floors = sorted({int(r["floor"]) for r in numbered if r["kind"] == "residential"})
    contiguous = (len(t_floors) <= 1) or (t_floors == list(range(t_floors[0], t_floors[-1] + 1)))

    shared = sorted(set(t_floors) & set(r_floors))
    # A hotel lobby beside a residential lobby on the ground floor is how a
    # mixed building is supposed to work, and 24 of 41 certificates share
    # exactly that and nothing else. Counting it as interleaving turned 6 real
    # problems into 14 flags. Sharing a *guest* floor is the thing that stops
    # you keying, cleaning and fire-separating a block.
    shared_guest = [f for f in shared if f > 1]

    return {
        "file": path.name,
        "readable": True,
        **header,
        "use_groups": sorted(use_groups),
        "rooms_described": rooms_by_kind,
        "rows": len(rows),
        "transient_floors": t_floors,
        "residential_floors": r_floors,
        "shared_floors": shared,
        "shared_guest_floors": shared_guest,
        "lobby_shared_only": bool(shared) and not shared_guest,
        "transient_contiguous": contiguous,
        "floors": rows,
    }


def annotate_currency(results: list, feed: dict) -> None:
    """Mark each parsed certificate against the newest one on file."""
    for r in results:
        if not r.get("readable"):
            continue
        newest = feed.get(r.get("bin") or "")
        mine = _parse_date(r.get("effective_date"))
        if not newest or not mine:
            r["is_current"] = None
            continue
        gap = (newest - mine).days
        r["newest_filed"] = newest.date().isoformat()
        r["years_superseded"] = round(gap / 365.25, 1)
        r["is_current"] = gap <= 365


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args[0] == "--all":
        paths = sorted(PDF_DIR.glob("*.PDF"))
    else:
        paths = [Path(a) for a in args]

    out, readable = [], 0
    for p in paths:
        r = parse(p)
        out.append(r)
        if r["readable"]:
            readable += 1
            if len(paths) == 1:
                print(json.dumps({k: v for k, v in r.items() if k != "floors"}, indent=2))
                for f in r["floors"][:40]:
                    print(f"    {f['floor']:>4}  UG {f['use_group']:<8} {f['kind']:<11} {f['description'][:60]}")

    annotate_currency(out, _feed_latest())

    if len(paths) > 1:
        dest = DATA_PROCESSED / "coo_parsed.json"
        dest.write_text(json.dumps(out, indent=2))
        print(f"{readable} of {len(paths)} certificates parsed -> {dest}")
        mixed = [r for r in out if r.get("readable") and r.get("shared_guest_floors")]
        lobby = [r for r in out if r.get("readable") and r.get("lobby_shared_only")]
        print(f"  sharing a guest floor with permanent residents: {len(mixed)}")
        print(f"  sharing only the lobby (normal, not a problem): {len(lobby)}")
        split = [r for r in out if r.get("readable") and not r.get("transient_contiguous")]
        print(f"  with non-contiguous transient floors: {len(split)}")
        superseded = [r for r in out if r.get("is_current") is False]
        print(f"  SUPERSEDED by a newer filing: {len(superseded)} — BIS serves "
              f"legacy certificates only, so a DOB NOW-era building gives up its old ones")


if __name__ == "__main__":
    main()
