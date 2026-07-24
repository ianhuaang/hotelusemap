"""Join pipeline output to building footprints -> final GeoJSON for the map.

Pure and re-runnable. Reads from data/raw/ and data/processed/, writes to data/processed/.
"""

import json
from datetime import date
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_RAW, DATA_PROCESSED

TODAY = date.today().strftime("%Y%m%d")

# Manhattan community districts + target BK/QN districts → neighborhood names
CD_TO_NEIGHBORHOOD = {
    "101": "Financial District / Tribeca",
    "102": "Greenwich Village / SoHo",
    "103": "Lower East Side / Chinatown",
    "104": "Chelsea / Hudson Yards",
    "105": "Midtown West",
    "106": "Midtown East / Murray Hill",
    "107": "Upper West Side",
    "108": "Upper East Side",
    "109": "Morningside Heights / Harlem",
    "110": "Central Harlem",
    "111": "East Harlem",
    "112": "Washington Heights / Inwood",
    "301": "Williamsburg / Greenpoint",
    "302": "Downtown Brooklyn / Fort Greene",
    "306": "Park Slope / Red Hook",
    "401": "Astoria / Long Island City",
}


def build_geojson(
    pipeline_path: Path = None,
    footprints_path: Path = None,
) -> Path:
    if pipeline_path is None:
        pipeline_path = DATA_PROCESSED / f"pipeline_{TODAY}.json"
    if footprints_path is None:
        footprints_path = DATA_RAW / f"footprints_{TODAY}.json"

    pipeline = json.loads(pipeline_path.read_text())
    footprints = json.loads(footprints_path.read_text())

    # Drop unknown-tier buildings — no transient signal, just noise
    # But keep any building with a prior_operator tag
    pipeline = [r for r in pipeline if r["tier"] != "unknown" or r.get("prior_operator")]

    # Index pipeline by BBL
    pipe_by_bbl = {r["bbl"]: r for r in pipeline}

    # Index footprints by both base_bbl and mappluto_bbl
    fp_by_bbl: dict[str, list[dict]] = {}
    for fp in footprints:
        for key in ("base_bbl", "mappluto_bbl"):
            bbl = str(fp.get(key, "")).strip()
            if bbl:
                fp_by_bbl.setdefault(bbl, []).append(fp)

    # Tier priority for deduplication — higher-signal tiers win
    TIER_PRIORITY = {
        "legal_transient": 0, "class_b": 1, "partial": 2,
        "prior_operator": 3, "unknown": 4, "excluded": 5,
    }

    # Deduplicate: one feature per footprint (doitt_id), highest-tier record wins
    # Multiple condo lots can map to the same footprint — pick the best signal
    best_by_doitt: dict[str, tuple[dict, dict]] = {}  # doitt_id -> (record, fp)
    matched = 0
    unmatched_pipeline = 0

    for bbl, record in pipe_by_bbl.items():
        fps = fp_by_bbl.get(bbl, [])
        if not fps:
            unmatched_pipeline += 1
            continue

        matched += 1
        for fp in fps:
            geom = fp.get("the_geom")
            if not geom:
                continue

            doitt_id = str(fp.get("doitt_id", ""))
            existing = best_by_doitt.get(doitt_id)
            rec_priority = TIER_PRIORITY.get(record["tier"], 99)

            if existing is None:
                best_by_doitt[doitt_id] = (record, fp)
            else:
                existing_priority = TIER_PRIORITY.get(existing[0]["tier"], 99)
                if rec_priority < existing_priority:
                    best_by_doitt[doitt_id] = (record, fp)
                elif rec_priority == existing_priority and record.get("prior_operator"):
                    best_by_doitt[doitt_id] = (record, fp)

    features = []
    for doitt_id, (record, fp) in best_by_doitt.items():
        properties = {
            "bbl": record["bbl"],
            "address": record["address"],
            "bldgclass": record["bldgclass"],
            "unitsres": record["unitsres"],
            "unitstotal": record["unitstotal"],
            "numfloors": record["numfloors"],
            "tier": record["tier"],
            "confidence": record["confidence"],
            "reason_codes": record["reason_codes"],
            "blockers": record["blockers"],
            "hpd_class_a": record["hpd_class_a"],
            "hpd_class_b": record["hpd_class_b"],
            "hpd_dob_class": record["hpd_dob_class"],
            "ownername": record["ownername"],
            "cd": record.get("cd", ""),
            "neighborhood": CD_TO_NEIGHBORHOOD.get(record.get("cd", ""), ""),
            "zonedist1": record["zonedist1"],
            "height_roof": fp.get("height_roof"),
            "construction_year": fp.get("construction_year"),
            "bin": fp.get("bin", ""),
            "source_pulled_on": record["source_pulled_on"],
            "last_sale_date": record.get("last_sale_date"),
            "last_sale_price": record.get("last_sale_price"),
            "sale_count": record.get("sale_count", 0),
            "permit_count": record.get("permit_count", 0),
            "owner_canonical": record.get("owner_canonical", record.get("ownername", "")),
            "owner_portfolio_size": record.get("owner_portfolio_size", 0),
            "coo_count": record.get("coo_count", 0),
            "coo_latest_date": record.get("coo_latest_date"),
            "coo_latest_type": record.get("coo_latest_type"),
            "coo_has_temporary": record.get("coo_has_temporary", False),
            "coo_dwelling_units": record.get("coo_dwelling_units"),
            # Distress signals
            "hpd_open_violations": record.get("hpd_open_violations", 0),
            "hpd_class_c_violations": record.get("hpd_class_c_violations", 0),
            "ecb_open_violations": record.get("ecb_open_violations", 0),
            "ecb_total_balance": record.get("ecb_total_balance", 0),
            "has_tax_lien": record.get("has_tax_lien", False),
            "has_lis_pendens": record.get("has_lis_pendens", False),
            "lis_pendens_count": record.get("lis_pendens_count", 0),
            # ACRIS owner identification
            "acris_deed_owner": record.get("acris_deed_owner", ""),
            "acris_deed_date": record.get("acris_deed_date", ""),
            "acris_deed_address": record.get("acris_deed_address", ""),
            "acris_borrower": record.get("acris_borrower", ""),
            "acris_lender": record.get("acris_lender", ""),
            # Hotel info
            "hotel_name": record.get("hotel_name", ""),
            "hotel_phone": record.get("hotel_phone", ""),
            "hotel_website": record.get("hotel_website", ""),
        }

        # Include top 3 permits (trimmed to save space)
        permits = record.get("permits", [])
        if permits:
            properties["permits"] = [
                {k: p[k] for k in ("job_type_label", "description", "action_date", "cost", "status") if p.get(k)}
                for p in permits[:3]
            ]

        # Include top 3 C of O records
        coos = record.get("coo_records", [])
        if coos:
            properties["coo_records"] = [
                {k: c[k] for k in ("issue_date", "job_type", "co_type", "dwelling_units") if c.get(k)}
                for c in coos[:3]
            ]

        if record.get("prior_operator"):
            properties["prior_operator"] = record["prior_operator"]
            properties["has_prior_op"] = True

        if record.get("reversion_window"):
            properties["reversion_window"] = record["reversion_window"]
            properties["has_reversion"] = True

        # Deal sub-scores (each 0-100, combined on frontend with adjustable weights)
        # Legal certainty (max 50 raw pts -> normalized to 0-100)
        legal = 0
        tier = record["tier"]
        if tier == "legal_transient":
            legal += 30
        elif tier == "class_b":
            legal += 20
        elif tier == "partial":
            legal += 8
        elif tier == "prior_operator":
            legal += 5
        if record.get("coo_has_temporary"):
            legal += 10
        if record.get("hpd_class_b", 0) > 0:
            legal += 10
        properties["score_legal"] = round(min(legal / 50, 1.0) * 100)

        # Availability (max 45 raw pts -> normalized to 0-100)
        avail = 0
        if record.get("prior_operator"):
            avail += 15
        if record.get("has_tax_lien"):
            avail += 8
        if record.get("has_lis_pendens"):
            avail += 8
        sale_date = record.get("last_sale_date") or ""
        if sale_date >= f"{date.today().year - 2}-01-01":
            avail += 5
        if record.get("reversion_window"):
            avail += 5
        if (record.get("ecb_total_balance") or 0) > 10000:
            avail += 4
        properties["score_avail"] = round(min(avail / 45, 1.0) * 100)

        # Building quality (max 15 raw pts -> normalized to 0-100)
        quality = 0
        bldgclass = record.get("bldgclass") or ""
        if not bldgclass.startswith("H"):
            quality += 7
        class_c = record.get("hpd_class_c_violations") or 0
        if class_c < 10:
            quality += 5
        elif class_c < 20:
            quality += 2
        if (record.get("owner_portfolio_size") or 0) > 1:
            quality += 3
        properties["score_quality"] = round(min(quality / 15, 1.0) * 100)

        feature = {
            "type": "Feature",
            "geometry": fp["the_geom"],
            "properties": properties,
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    outpath = DATA_PROCESSED / f"buildings_{TODAY}.geojson"
    outpath.write_text(json.dumps(geojson))
    print(f"Built GeoJSON: {len(features)} features ({matched} BBLs matched, {unmatched_pipeline} unmatched)")
    return outpath


if __name__ == "__main__":
    build_geojson()
