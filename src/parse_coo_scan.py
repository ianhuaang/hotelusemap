"""Read the floor table off a scanned Certificate of Occupancy, via OCR.

parse_coo_pdf.py reads certificates that carry a text layer — DOB NOW
documents, ids like 104844581-37. Legacy BIS scans (M000082568) are images
and return zero characters, so it skips them. That skip is most of the
archive: of 448 certificates on disk across 196 buildings, 113 buildings have
nothing but scans, and they are not the small ones. 51 Clark Street is 692
Class B rooms. 859 7th Avenue is 687. Both were invisible here.

Two things had to be solved rather than one. OCR gets the characters back,
but a legacy certificate is also a different document: no "Certificate Type:"
header, no "02 150 12 A RESIDENTIAL" rows. It is a typewritten form with a
PERMISSIBLE USE AND OCCUPANCY block read by eye:

    2ND FLOOR   40  J-2  SIX (6) CLASS A APARTMENTS
                    J-2  FOURTEEN (14) ROOMING UNITS

So this module supplies both, and hands the rows to parse_coo_pdf.summarise
so an OCR'd scan and a text layer produce the same record. Nothing downstream
can tell which parser read a building.

OCR is local and free: rapidocr-onnxruntime bundles its models and needs no
system packages, which matters because DOB BIS 403s automation and this
cannot run in CI either way.

Usage:
    python3 src/parse_coo_scan.py data/raw/coo_pdfs/1028948_M000102746.PDF
    python3 src/parse_coo_scan.py --all      # every scan not already readable
    python3 src/parse_coo_scan.py --merge-only   # fold the cache in after a halt
"""

import json
from datetime import date
import os
import re
import sys
from pathlib import Path

# Before onnxruntime is imported anywhere. Each engine would otherwise try to
# use every core, and the work is parallel across certificates, not within one.
os.environ.setdefault("OMP_NUM_THREADS", "1")

sys.path.insert(0, str(Path(__file__).parent.parent))
# Also this directory, so the shared parser resolves whether this is run as a
# script (python3 src/parse_coo_scan.py) or imported as src.parse_coo_scan.
sys.path.insert(0, str(Path(__file__).parent))
from config import DATA_RAW, DATA_PROCESSED
from src import provenance
from parse_coo_pdf import summarise, annotate_currency, merge_parsed, _feed_latest

PDF_DIR = DATA_RAW / "coo_pdfs"

# Rendering resolution. 200 dpi loses the typewriter's thin strokes on the
# worst scans; 400 doubles the time for nothing. 300 is where the sample
# stopped improving.
DPI = 300
# Certificates run to three or four sheets ("PAGE 1 OF 3 PAGES") and the table
# continues across them. Past six is boilerplate and zoning-lot metes.
MAX_PAGES = 6

# OCR confuses the same few glyphs every time, and always inside words we have
# to match on: FLOOR arrives as ELOOR, FLCOR, FL00R, and 2ND as 2NE. Rather
# than enumerate the misreadings, match the shape — F, a tall letter, two
# round letters, R — and accept what lands in it.
# Two eras, two words for the same thing. 1960s-90s forms say FLOOR; the
# 1920s-50s ones say STORY, and a parser that only knows FLOOR reads nothing
# off half the archive.
FLOOR_WORD = r"(?:[FEP][LIT1!][O0CQD][O0CQDR]?R?S?|ST[O0QD][RBP][YVI1J]E?S?|STY\.?)"
# Those older forms also spell the ordinal out, and number it in the column
# beside: "First Story", "Second", "Third".
WORD_ORD = {
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6,
    "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ELEVENTH": 11,
    "TWELFTH": 12, "THIRTEENTH": 13, "FOURTEENTH": 14, "FIFTEENTH": 15,
    "SIXTEENTH": 16, "SEVENTEENTH": 17, "EIGHTEENTH": 18, "NINETEENTH": 19,
    "TWENTIETH": 20,
}
WORD_RE = re.compile(r"^\W*(" + "|".join(WORD_ORD) + r")\b\s*" + FLOOR_WORD + r"?", re.I)
NAMED = {
    "SUB": r"SUB[\s\-.]*CELLAR|SUBCELLAR",
    "CEL": r"CELLAR|CELIAR|CELLER",
    "BAS": r"BASEMENT|RUSEMENT|BASMENT",
    "MEZ": r"MEZZANINE|MEZZ\b",
    "ROO": r"ROOF|ROOE\b",
    "PEN": r"PENTHOUSE|PENT\s*HOUSE|\bP\.?H\.?\b",
}
NAMED_RE = re.compile(r"^\W*(" + "|".join(NAMED.values()) + r")\b", re.I)
# "2ND FLOOR", "4TH FLCOR", and the ranges: "2ND TO 5TH FLOORS", "6-10 FLOORS".
RANGE_RE = re.compile(
    rf"^\W*(\d{{1,2}})\s*[A-Z]{{0,2}}\.?\s*(?:TO|THRU|THROUGH|AND|&|[-–])\s*"
    rf"(\d{{1,2}})\s*[A-Z]{{0,2}}\.?\s+{FLOOR_WORD}\b", re.I)
SINGLE_RE = re.compile(rf"^\W*(\d{{1,2}})\s*[A-Z]{{0,2}}\.?\s+{FLOOR_WORD}\b", re.I)

# 1968 code occupancy groups, as typed on these forms. J-1 transient, J-2
# permanent — the distinction the whole tool turns on. OCR drops the hyphen
# about half the time, so it is optional.
GROUP_RE = re.compile(r"\b(J\s*[-–]?\s*[123]|[A-HK])\b(?![A-Z])")
# A live load in pounds per square foot opens the numeric columns.
LOAD_RE = re.compile(r"^\s*(\d{2,4})\b")
# Where the table starts and stops. Everything above the first is letterhead;
# the zoning-lot description below the second is metes and bounds.
TABLE_START = re.compile(r"PERMISSIBLE\s+USE|USE\s+AND\s+OCCUPANCY", re.I)
TABLE_END = re.compile(
    r"OPEN\s+SPACE\s+USES|ZONING\s+LOT|BOUNDED\s+AS|CONSTRUCTION\s+CLASSIF"
    r"|FIRE\s+DETECTION|NO\s+CHANGES\s+OF\s+USE", re.I)
# A row says somewhere people sleep, so its parenthesised number is a count.
SLEEPS = re.compile(r"\b(ROOM|APARTMENT|DWELLING|UNIT|SUITE|HOTEL|SRO|ROOMING|FAMIL)", re.I)


# Words that mean a row actually described a use. A row of OCR noise has
# none of them, and a certificate mostly made of such rows was not really read.
USE_WORD = re.compile(
    r"\b(APART|DWELL|ROOM|HOTEL|STORE|OFFICE|RETAIL|RESID|LOBBY|STORAG|BOILER"
    r"|LAUNDR|RESTAUR|CLASS|SRO|LOFT|TENANT|MECHANIC|ELEVATOR|KITCHEN|GARAGE"
    r"|LOCKER|UTILIT|TERRACE|ACCESSORY)", re.I)


def _trustworthy(rows: list) -> bool:
    """Whether an OCR'd certificate was read well enough to publish.

    A gate rather than a best effort, because the failure here is not a
    missing building, it is a wrong one. These scans degrade from readable to
    nonsense gradually — the bad ones still yield a row or two of fragments
    like "Ist .3toly 120-40 2nd t$ lt.th" — and a floor table saying that is
    worse than no floor table at all. The reader cannot tell it is noise, and
    the whole point of the table is to be trusted about upper floors.

    Three things have to hold: enough rows to be a table, at least two storeys
    above the ground, since a ground floor alone answers nothing anyone is
    asking here, and half the rows naming a use a person would recognise.
    """
    if len(rows) < 3:
        return False
    numbered = {int(r["floor"]) for r in rows if r["floor"].isdigit()}
    if len({n for n in numbered if n > 1}) < 2:
        return False
    # A real floor table is dense: a building with rows for floors 1 and 2 does
    # not then skip to 45. When it looks like that, the high numbers are
    # occupancy loads and live loads from the columns alongside, picked up as
    # floor labels because the scan's grid lines did not survive. Allowing
    # twice as many storeys as rows read leaves room for a genuine "2ND TO 20TH
    # FLOORS" range while rejecting 1, 2, 12, 21, 31, 45, 46 off a 7-row table.
    if numbered and max(numbered) > 2 * len(numbered) + 2:
        return False
    hits = sum(1 for r in rows if USE_WORD.search(r["description"]))
    return hits >= max(2, len(rows) // 2)


_ENGINE = None


def _engine():
    """One OCR engine for the process.

    Built once and held: each RapidOCR() loads three ONNX models and holds
    native memory that Python's collector does not own, so constructing one
    per certificate segfaults a few hundred files into a run.
    """
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise SystemExit(
                "OCR needs two packages:\n"
                "    python3 -m pip install --user rapidocr-onnxruntime onnxruntime\n"
                "Both are pure wheels with bundled models — no system packages, no API key.")
        _ENGINE = RapidOCR()
    return _ENGINE


def _ocr_lines(path: Path, dpi: int = DPI, max_pages: int = MAX_PAGES) -> list:
    """The page as lines in reading order, rebuilt from OCR boxes.

    RapidOCR returns boxes, not lines, and a floor row is only a row because
    of where it sits on the page: group boxes by vertical centre, then order
    each group left to right. Without this the live load, the use group and
    the description arrive as three unrelated fragments.
    """
    import pymupdf

    engine = _engine()
    out = []
    doc = pymupdf.open(path)
    for page in list(doc)[:max_pages]:
        # Everything below the table is the zoning lot's metes and bounds, the
        # fire-protection checkboxes and the commissioner's signature, and OCR
        # is the expensive step: reading all six pages of every certificate
        # when the table ends on page one put a 239-file run at five hours.
        if out and any(TABLE_END.search(l) for l in out):
            break
        pix = page.get_pixmap(dpi=dpi)
        result, _ = engine(pix.tobytes("png"))
        if not result:
            continue
        height = pix.height
        boxes = []
        for box, text, _conf in result:
            ys = [p[1] for p in box]
            boxes.append((sum(ys) / len(ys) / height, min(p[0] for p in box), text))
        boxes.sort()
        # 1.2% of page height. Tighter splits a row whose columns sit a line
        # apart; looser merges the row below into it.
        row, centre = [], None
        for y, x, text in boxes:
            if centre is not None and abs(y - centre) > 0.012:
                out.append(" ".join(t for _x, t in sorted(row)))
                row, centre = [], None
            row.append((x, text))
            if centre is None:
                centre = y
        if row:
            out.append(" ".join(t for _x, t in sorted(row)))
    return out


def _floor_of(line: str):
    """The floors a line opens, and what is left after the label."""
    # A typewriter's 1 and OCR's I are the same glyph: "Ist .3toly" is "1st
    # Story". Corrected only in the label, never in the description, where an
    # I is usually a letter.
    line = re.sub(r"^(\W*)([Il|])(?=(?:st|ST)\b)", r"\g<1>1", line)
    m = WORD_RE.match(line)
    if m:
        return [f"{WORD_ORD[m.group(1).upper()]:02d}"], line[m.end():]
    m = NAMED_RE.match(line)
    if m:
        hit = m.group(1).upper()
        for key, pattern in NAMED.items():
            if re.fullmatch(pattern, hit, re.I):
                return [key], line[m.end():]
        return ["CEL"], line[m.end():]
    m = RANGE_RE.match(line)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 < lo <= hi <= 99 and hi - lo < 60:
            return [f"{n:02d}" for n in range(lo, hi + 1)], line[m.end():]
    m = SINGLE_RE.match(line)
    if m:
        n = int(m.group(1))
        if 0 < n <= 99:
            return [f"{n:02d}"], line[m.end():]
    return None, line


def _split_columns(rest: str):
    """Live load, occupancy group and description out of one row's tail."""
    load = None
    m = LOAD_RE.match(rest)
    if m:
        load = int(m.group(1))
        rest = rest[m.end():]
    group = ""
    m = GROUP_RE.search(rest[:14])
    if m:
        group = re.sub(r"[\s–]", "", m.group(1)).upper().replace("-", "")
        group = f"{group[0]}-{group[1:]}" if len(group) > 1 else group
        rest = rest[:m.start()] + rest[m.end():]
    return load, group, rest.strip(" .:-")


def _classify(description: str, group: str) -> str:
    """Transient, residential or other.

    The occupancy group is read first where there is one: it is the
    certificate's own answer, and survives OCR better than prose. J-1 is
    transient, J-2 permanent residence.
    """
    if group in ("J-1", "J1"):
        return "transient"
    if group in ("J-2", "J-3", "J2", "J3"):
        return "residential"
    upper = description.upper()
    if any(k in upper for k in ("J-1", "HOTEL", "TRANSIENT")):
        return "transient"
    if any(k in upper for k in ("J-2", "RESIDENTIAL", "APARTMENT", "DWELLING", "ROOMING")):
        return "residential"
    return "other"


def _rows_from_lines(lines: list):
    """Floor rows out of the PERMISSIBLE USE AND OCCUPANCY block."""
    started = any(TABLE_START.search(l) for l in lines)
    live = not started
    rows, groups, current = [], set(), None

    for line in lines:
        if not live:
            if TABLE_START.search(line):
                live = True
            continue
        if TABLE_END.search(line):
            live = False
            continue

        floors, rest = _floor_of(line)
        if floors:
            load, group, desc = _split_columns(rest)
            current = []
            for f in floors:
                row = {"floor": f, "load": load, "group": group, "desc": desc}
                rows.append(row)
                current.append(row)
            if group:
                groups.add(group)
            continue

        if current is None:
            continue
        # A continuation line. If it names its own count and its own use it is
        # a second entry on the same floor — "FOURTEEN (14) ROOMING UNITS"
        # under a floor already carrying six apartments — and folding it into
        # the line above would lose one of the two counts.
        load, group, desc = _split_columns(line)
        if not desc:
            continue
        if re.search(r"\(\s*\d{1,3}\s*\)", desc) and SLEEPS.search(desc):
            current = [dict(r, load=load, group=group or r["group"], desc=desc)
                       for r in current]
            rows.extend(current)
            if group:
                groups.add(group)
        else:
            for r in current:
                r["desc"] = f"{r['desc']} {desc}".strip()

    out = []
    for r in rows:
        desc, group = r["desc"], r["group"]
        if not desc:
            continue
        described = None
        if SLEEPS.search(desc):
            m = re.search(r"\(\s*(\d{1,3})\s*\)", desc)
            if m:
                described = int(m.group(1))
        out.append({
            "floor": r["floor"],
            "occupancy_load": r["load"],
            "units_described": described,
            "units_column_raw": None,
            "use_group": group,
            "kind": _classify(desc, group),
            "description": desc[:120],
        })
    return out, groups


def _header_from(lines: list, path: Path) -> dict:
    """Address and date off the form, BIN off the filename.

    The BIN is on the certificate only as a block and lot, and OCR mangles
    those. The filename carries it exactly — these were downloaded per BIN —
    so take it from there rather than guess at the scan.
    """
    header = {"bin": path.name.split("_")[0]}
    text = " ".join(lines[:14])
    m = re.search(r"\b(0?[1-9]|1[0-2])\s*[/\-]\s*(0?[1-9]|[12]\d|3[01])\s*[/\-]\s*((?:19|20)?\d\d)\b", text)
    if m:
        mm, dd, yy = m.groups()
        yy = yy if len(yy) == 4 else ("19" + yy if int(yy) > 40 else "20" + yy)
        header["effective_date"] = f"{int(mm):02d}/{int(dd):02d}/{yy}"
    m = re.search(r"located\s+at\s+(.{6,60}?)(?:\s+Block|\s+BLOCK|$)", " ".join(lines), re.I)
    if m:
        header["address"] = re.sub(r"\s+", " ", m.group(1)).strip()
    header["co_type"] = "Legacy (OCR)"
    return header


def parse_scan(path: Path) -> dict:
    """One scanned certificate, in the same shape as a text-layer one."""
    lines = _ocr_lines(path)
    rows, groups = _rows_from_lines(lines)
    if not rows:
        return {"file": path.name, "readable": False,
                "reason": "OCR found no PERMISSIBLE USE AND OCCUPANCY rows"}
    if not _trustworthy(rows):
        return {"file": path.name, "readable": False, "rows_seen": len(rows),
                "reason": "OCR output too thin or too noisy to publish"}
    record = summarise(rows, _header_from(lines, path), path.name, groups)
    # Said outright, because an OCR'd description is a reading of a 1960s
    # typescript and a reader deciding on a building should know that.
    record["source"] = "ocr"
    return record


CACHE = DATA_PROCESSED / "coo_ocr_cache.jsonl"


def _read_cache() -> dict:
    """Everything OCR'd so far, by filename."""
    if not CACHE.exists():
        return {}
    out = {}
    for line in CACHE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:     # a line torn in half by a kill
            continue
        out[row["file"]] = row
    return out


def _try(path: Path) -> dict:
    """One certificate, never raising. A bad scan is not a reason to stop."""
    try:
        return parse_scan(path)
    except Exception as exc:
        return {"file": path.name, "readable": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def _merge_into_parsed(got: int = 0) -> None:
    """Fold everything OCR'd so far into coo_parsed.json.

    Kept apart from the OCR pass because the pass is long and fragile. A
    hung worker stranded 231 cached results behind a merge that never ran —
    the cache was full and nothing downstream could see any of it. The cache
    is the record; this reads it and nothing else, so it is safe to run on
    its own after an interrupted pass rather than OCR'ing the archive again.
    """
    src_path = provenance.resolve("coo_parsed", DATA_PROCESSED)
    dest = DATA_PROCESSED / f"coo_parsed_{date.today():%Y%m%d}.json"
    existing = json.loads(src_path.read_text()) if src_path.exists() else []
    # Everything ever read, this pass and any earlier one.
    fresh = list(_read_cache().values())

    annotate_currency(fresh, _feed_latest())
    # Replace each file's old unreadable stub, keep everything else as it was.
    # Shared with parse_coo_pdf so the two readers cannot disagree about which
    # record survives — they write the same file from opposite ends.
    records = merge_parsed(existing, fresh)
    dest.write_text(json.dumps(records, indent=2))
    bins = {r.get("bin") for r in records if r.get("floors")}
    print(f"\n{got} newly readable by OCR -> {dest}")
    print(f"  buildings with a floor table now: {len(bins)}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)

    # An interrupted pass leaves its results in the cache and coo_parsed.json
    # untouched. This picks them up without re-reading a single certificate.
    if args[0] == "--merge-only":
        _merge_into_parsed()
        return

    dest = provenance.resolve("coo_parsed", DATA_PROCESSED)
    if args[0] == "--all":
        existing = json.loads(dest.read_text()) if dest.exists() else []
        done = {r["file"] for r in existing if r.get("floors")}
        # Anything already OCR'd in an earlier pass, however it ended. A full
        # pass is about half an hour of solid CPU and two of them have now been
        # lost to an interruption partway, so each result is written as it
        # lands and a restart picks up from there.
        cached = _read_cache()
        done |= set(cached)
        paths = [p for p in sorted(PDF_DIR.glob("*.PDF")) if p.name not in done]
        print(f"{len(paths)} certificates to try"
              + (f" ({len(cached)} already read in an earlier pass)" if cached else ""))
    else:
        existing, paths = None, [Path(a) for a in args]

    if existing is None:                              # named files, read here
        for p in paths:
            r = _try(p)
            print(json.dumps({k: v for k, v in r.items() if k != "floors"}, indent=2))
            for f in r.get("floors", []):
                print(f"    {f['floor']:>4}  UG {f['use_group'] or '-':<5} "
                      f"{f['kind']:<11} {f['description'][:62]}")
        return

    # OCR is one core bound solid for about a minute a certificate, and the
    # certificates are independent, so the run is only as long as the archive
    # divided by the cores. Serial, the first full pass projected to five
    # hours. Two are left for the machine to stay usable on.
    import multiprocessing as mp
    workers = max(1, min(6, (os.cpu_count() or 4) - 2))
    print(f"  {workers} workers")
    got = 0
    with mp.Pool(workers) as pool, CACHE.open("a") as cache:
        for i, r in enumerate(pool.imap_unordered(_try, paths, chunksize=1), 1):
            cache.write(json.dumps(r) + "\n")
            cache.flush()
            if r.get("floors"):
                got += 1
            if i % 10 == 0 or i == len(paths):
                print(f"  {i}/{len(paths)} read, {got} with a floor table", flush=True)

    _merge_into_parsed(got)


if __name__ == "__main__":
    main()
