// Facts about a building, derived from its geojson properties and nothing else.
//
// This module exists because the detail panel and the PDF export answer the
// same questions about the same building, and they were about to answer them
// from two copies of the logic. Everything here is pure: no React, no DOM, no
// maplibre — so both the panel and a jsPDF page can call it.

export const SEGMENTS = [
  {
    key: "transient", label: "Class B, no operator", color: "#8b5cf6", defaultOn: true,
    info: "HPD has registered Class B (transient) rooms here, and no hotel operator turned up in DCWP licences or Google Places. Most still have a managing agent on file, which is not the same thing. A couple qualify on DOB R-1 occupancy rather than Class B. The primary sourcing targets.",
  },
  {
    key: "active_hotel", label: "Active hotel", color: "#16a34a", defaultOn: false,
    info: "Operating hotels with a known operator (brand, license, or listing). Already has a hotel operator in place.",
  },
  {
    key: "partial", label: "Partial signal", color: "#f59e0b", defaultOn: false,
    info: "Building class suggests mixed use (RM, RC, etc.) but HPD didn't confirm Class B rooms. May have transient capacity — needs manual verification.",
  },
];

export const SEGMENT_COLORS = Object.fromEntries(SEGMENTS.map((s) => [s.key, s.color]));

export function segmentColor(segment) {
  return SEGMENT_COLORS[segment] || "#94a3b8";
}

export function segmentLabel(segment) {
  return (SEGMENTS.find((s) => s.key === segment) || {}).label
    || String(segment || "").replace(/_/g, " ")
    || "Unclassified";
}

export const CRM_STATUSES = [
  { value: "", label: "—", color: "" },
  { value: "not_contacted", label: "Not contacted", color: "bg-gray-100 text-gray-600" },
  { value: "reached_out", label: "Reached out", color: "bg-blue-100 text-blue-700" },
  { value: "meeting_set", label: "Meeting set", color: "bg-emerald-100 text-emerald-700" },
  { value: "passed", label: "Passed", color: "bg-red-100 text-red-600" },
];

// buildings.geojson features are MultiPolygon footprints, but the clustered dot
// layer and search results hand back Points. Callers get a [lng, lat] either way.
// Vertex mean of the outer ring — an approximation, but consistent with the dots.
export function featureCentroid(f) {
  const coords = f?.geometry?.coordinates;
  if (!coords) return null;
  if (f.geometry.type === "Point") return coords;
  const ring = f.geometry.type === "MultiPolygon" ? coords[0][0] : coords[0];
  if (!ring || !ring.length) return null;
  let cx = 0, cy = 0;
  for (const [x, y] of ring) { cx += x; cy += y; }
  return [cx / ring.length, cy / ring.length];
}

export function estRooms(p) {
  const classB = p.hpd_class_b || 0;
  const cooUnits = p.coo_dwelling_units ? parseInt(p.coo_dwelling_units, 10) || 0 : 0;
  const isHotel = (p.bldgclass || "").startsWith("H");
  if (classB > 0) return { value: classB, source: "HPD Class B" };
  if (cooUnits > 0) return { value: cooUnits, source: "C of O" };
  const floors = p.numfloors || 0;
  if (isHotel && floors >= 3) return { value: Math.round(floors * 15), source: "Floor est." };
  const plutoUnits = (p.unitsres || 0) > 0 ? p.unitsres : (p.unitstotal || 0);
  if (plutoUnits > 0) return { value: plutoUnits, source: "PLUTO" };
  if (floors >= 3) return { value: Math.round(floors * 15), source: "Floor est." };
  return { value: 0, source: "Unknown" };
}

export const ROOM_SOURCE_EXPLAIN = {
  "HPD Class B": "Transient (Class B) rooms registered with HPD under the Multiple Dwelling Law. Renewed annually by building owners — the most current signal of active transient capacity.",
  "C of O": "From DOB Certificate of Occupancy — the approved dwelling unit count. Reliable but may include residential units.",
  "Floor est.": "Estimated at ~15 rooms/floor. No HPD registration or C of O on file for this hotel.",
  "PLUTO": "From Dept. of Finance tax lot data. Counts residential dwelling units, not hotel rooms — accurate for residential buildings but undercounts hotels.",
  "Unknown": "No room count data available from any source.",
};

export function parseJsonProp(val) {
  if (typeof val === "string") {
    try { return JSON.parse(val); } catch { return val; }
  }
  return val;
}

export function parseBbl(bbl) {
  const s = String(bbl || "").padStart(10, "0");
  return { borough: s[0], block: s.slice(1, 6), lot: s.slice(6, 10) };
}

export function buildRecordLinks(bbl, bin) {
  const { borough, block, lot } = parseBbl(bbl);
  return {
    hpd: `https://hpdonline.nyc.gov/hpdonline/building/search-results?boroId=${borough}&block=${block}&lot=${lot}`,
    dob: bin
      ? `https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?bin=${bin}&requestid=1`
      : `https://a810-bisweb.nyc.gov/bisweb/PropertyBrowseByBBLServlet?allborough=${borough}&allblock=${block}&alllot=${lot}&go5=+GO+&requestid=0`,
    acris: `https://a836-acris.nyc.gov/bblsearch/bblsearch.asp?borough=${borough}&block=${block}&lot=${lot}`,
  };
}

// Alt addresses arrive as spelling variants of the same place: 35-02 37
// Avenue lists "37-06 36 STREET", "37-06 36TH STREET", "37-6 36TH STREET" and
// "3706 36TH STREET", which is one address written four ways. Dedupe on a
// normalised key — ordinal suffixes dropped, punctuation and leading zeros
// removed — and show the first spelling of each.
export function distinctAddresses(list, exclude) {
  const key = (s) => String(s || "").toUpperCase()
    .replace(/(\d)(ST|ND|RD|TH)\b/g, "$1")
    .replace(/[^A-Z0-9]/g, "")
    .replace(/\b0+(\d)/g, "$1");
  const skip = new Set([key(exclude)]);
  const out = [];
  for (const a of list || []) {
    const k = key(a);
    if (!a || !k || skip.has(k)) continue;
    skip.add(k);
    out.push(a);
  }
  return out;
}

export function computeScore(p) {
  let score = 0;
  const hasClassB = (p.hpd_class_b || 0) > 0;
  const hasH = (p.bldgclass || "").startsWith("H");
  // Weights sum to 100 so a full house reads as 100 and the number is a real
  // percentage of available legal evidence. They previously topped out at 93,
  // which made "out of 100" unanswerable — 16 buildings sat at the unreachable
  // ceiling and nothing could ever be a 100.
  if (hasClassB) score += 40;
  if (hasH) score += 25;
  if (p.dob_has_r1) score += 15;
  // A final C of O is the strongest evidence transient use is approved, so it
  // scores highest. coo_has_temporary only means "a temporary one appears
  // somewhere in the history" — 19 West 103 Street has 24 C of O records, one
  // temporary, and was collecting the full bonus while buildings with a clean
  // final C of O collected nothing. Judge the latest record instead.
  const cooType = p.coo_latest_type || "";
  if (cooType === "Final" || cooType.startsWith("Renewal")) score += 12;
  else if (cooType === "Temporary" || cooType === "Initial") score += 7;
  if ((p.permit_transient_strong || 0) >= 1) score += 8;
  // dob_r1_filing_count is NOT scored: it counts the same DOB filings that set
  // dob_has_r1, so scoring both awarded 22 of 100 points for one signal.
  // Filing volume is surfaced in buildScoreSignals as confidence, not points.
  // Zoning penalty only for buildings without existing transient rights
  if (p.zoning_hotel_permitted === "not_permitted" && !hasClassB && !hasH) score = Math.max(0, score - 15);
  return Math.min(score, 100);
}

// The line items behind computeScore, in the order they are weighted.
export function buildScoreSignals(p) {
  const signals = [
    { label: "HPD Class B rooms registered", pts: 40, hit: (p.hpd_class_b || 0) > 0 },
    { label: "Hotel building class (H-series)", pts: 25, hit: (p.bldgclass || "").startsWith("H") },
    { label: "DOB R-1 transient occupancy", pts: 15, hit: !!p.dob_has_r1 },
    { label: "Final C of O on file", pts: 12, hit: (p.coo_latest_type || "") === "Final" || (p.coo_latest_type || "").startsWith("Renewal") },
    { label: "Temporary C of O only", pts: 7, hit: (p.coo_latest_type || "") === "Temporary" || (p.coo_latest_type || "") === "Initial" },
    { label: "DOB transient permit activity", pts: 8, hit: (p.permit_transient_strong || 0) >= 1 },
  ];
  const hasGrandfathered = (p.hpd_class_b || 0) > 0 || (p.bldgclass || "").startsWith("H");
  if (p.zoning_hotel_permitted === "not_permitted" && !hasGrandfathered) signals.push({ label: "Residential zoning (penalty)", pts: -15, hit: true, penalty: true });
  if (p.zoning_hotel_permitted === "not_permitted" && hasGrandfathered) signals.push({ label: "Residential zoning (grandfathered)", pts: 0, hit: true });
  // Same filings that earned the R-1 points above — shown as corroboration, worth no points.
  const r1Filings = p.dob_r1_filing_count || 0;
  if (r1Filings >= 3) signals.push({ label: `Corroborated by ${r1Filings} R-1 filings`, pts: 0, hit: true });
  return signals;
}

// What the record says about whether transient use is legal here. icon is
// "check" (evidence for), "warn" (evidence against) or "info" (neutral).
export function buildFeasibilityItems(p, reasonCodes = []) {
  const items = [];
  const bldg = (p.bldgclass || "").toUpperCase();
  const isHotelClass = bldg.startsWith("H") && bldg !== "HR" && bldg !== "H8";
  if (isHotelClass) {
    items.push({ icon: "check", text: `Hotel building class (${bldg})` });
  } else if (bldg) {
    items.push({ icon: "info", text: `Building class: ${bldg}` });
  }
  if ((p.hpd_class_b || 0) > 0) {
    const totalHpd = (p.hpd_class_a || 0) + (p.hpd_class_b || 0);
    const pct = totalHpd > 0 ? Math.round((p.hpd_class_b / totalHpd) * 100) : 0;
    items.push({ icon: "check", text: `${p.hpd_class_b} HPD Class B (transient) rooms — ${pct}% of units` });
  }
  if ((p.hpd_class_a || 0) > 0) {
    items.push({ icon: "info", text: `${p.hpd_class_a} HPD Class A (residential) units` });
  }
  if (p.hpd_dob_class) {
    items.push({ icon: "info", text: `HPD DOB: ${p.hpd_dob_class}` });
  }
  if (p.has_hotel_license) {
    // Licensure, stated as licensure. This used to read "Active DCWP hotel
    // license" regardless of the actual status, and was also what the map used
    // to decide a building had an operator. The licensee is the owner; it says
    // the building may lawfully trade as a hotel, not that anyone is trading
    // in it.
    const status = p.hotel_license_status || "unknown status";
    const licensed = p.safe_hotels_licensed;
    items.push({
      icon: licensed ? "check" : "info",
      text: `DCWP hotel license — ${status}${licensed ? ", licensed under the Safe Hotels Act" : ""}${p.hotel_license_name ? ` (${p.hotel_license_name})` : ""}`,
    });
  }
  if (reasonCodes.includes("dob_transient_occupancy")) {
    items.push({ icon: "check", text: "DOB transient occupancy (R-1/J-1)" });
  }
  if (p.zonedist1) {
    const permitted = p.zoning_hotel_permitted === "permitted";
    items.push({
      icon: permitted ? "check" : "warn",
      text: `Zoning: ${p.zonedist1}${permitted ? " — hotel use permitted" : p.zoning_hotel_detail ? ` — ${p.zoning_hotel_detail}` : ""}`,
    });
  }
  return items;
}

// Two questions, not one: whether the law allows transient use here is
// separate from what it costs to run. kind is "legal" or "operational".
export function buildConsiderations(p) {
  const blockers = parseJsonProp(p.blockers) || [];
  const considerations = [];
  if (p.is_landmark) {
    considerations.push({
      text: `LPC Individual Landmark${p.landmark_name ? ` — ${p.landmark_name}` : ""}`,
      kind: "legal",
      severity: "medium",
    });
  }
  if (p.historic_district) {
    considerations.push({
      text: `Historic District — ${p.historic_district}`,
      kind: "legal",
      severity: "medium",
    });
  }
  if (p.is_condo) {
    considerations.push({
      text: "Condominium (condo billing lot)",
      detail: "Requires board approval, or a negotiation with a commercial condo owner.",
      kind: "operational",
      severity: "medium",
    });
  }
  if (p.ecb_illegal_transient > 0) {
    considerations.push({
      text: `${p.ecb_illegal_transient} open DOB violation${p.ecb_illegal_transient === 1 ? "" : "s"} under §28-210.3 — permanent dwelling offered or used for other than permanent residential purpose. This is the statute cited against illegal hotels, so somebody has already been running transient stays here without authority.`,
      kind: "legal",
      severity: "high",
    });
  }
  if (p.fisp_applicable) {
    considerations.push({
      text: "Over six storeys — subject to facade inspection (Local Law 11 / FISP)",
      detail: "Inspection and filing every five years, and an unsafe finding carries a repair deadline. A recurring cost, not a one-off.",
      kind: "operational",
      severity: "low",
    });
  }
  if (p.safe_hotels_large_hotel) {
    considerations.push({
      text: `${p.safe_hotels_guest_rooms} guest rooms — a "large hotel" under the Safe Hotels Act`,
      detail: "Over 400 guest rooms: core staff must be employed directly, and a security guard must be on duty continuously. Roughly 4-5 FTE of fixed cover before occupancy.",
      kind: "operational",
      severity: "high",
    });
  } else if (p.safe_hotels_direct_employment) {
    considerations.push({
      text: `${p.safe_hotels_guest_rooms} guest rooms — direct employment required by the Safe Hotels Act`,
      detail: "At 100 rooms or more, housekeeping, front desk and front service staff must be employed directly rather than subcontracted. Re-underwrite labor before pricing.",
      kind: "operational",
      severity: "high",
    });
  }
  if (p.reversion_unverified) {
    considerations.push({
      text: "Reversion window unverified — no record shows transient use here before the December 9, 2021 special-permit cutoff. Without that, re-establishing hotel use may require a special permit rather than being as-of-right.",
      kind: "legal",
      severity: "high",
    });
  }
  if (p.htc_converted_use) {
    considerations.push({
      text: `Union shop under a Hotel Trades Council contract, listed as ${p.htc_shop_type.toLowerCase()} rather than a hotel`,
      detail: "The agreement can survive a conversion or a change of operator, so labor obligations may attach before any deal is signed.",
      kind: "operational",
      severity: "high",
    });
  }
  if (p.zoning_hotel_permitted === "not_permitted") {
    considerations.push({
      text: `Residential zoning (${p.zonedist1 || "unknown"}) — hotel use not permitted for new operators, but existing Class B rooms are grandfathered`,
      kind: "legal",
      severity: "medium",
    });
  }
  if (blockers.length > 0) {
    blockers.forEach((b) => {
      const isRentStab = b.toLowerCase().includes("rent-stabilized");
      const is421a = b.includes("421");
      const isResidentialRestriction = isRentStab || is421a;
      considerations.push({
        text: isRentStab
          ? b.replace("conversion to transient use restricted", "applies to Class A units only, Class B rooms unaffected")
          : is421a
          ? b.replace("rent stabilization obligations restrict use changes", "applies to residential units, Class B transient rooms unaffected")
          : b,
        severity: isResidentialRestriction ? "low" : "high",
        kind: "legal",
      });
    });
  }
  return considerations;
}

// Money and maintenance trouble on the record. Empty when the building is clean.
export function buildDistressSignals(p) {
  const hasLien = p.has_tax_lien;
  const hasLp = p.has_lis_pendens;
  const hpdV = p.hpd_open_violations || 0;
  const hpdC = p.hpd_class_c_violations || 0;
  const ecbV = p.ecb_open_violations || 0;
  const ecbBal = p.ecb_total_balance || 0;
  if (!hasLien && !hasLp && hpdV === 0 && ecbV === 0) return [];

  const signals = [];
  if (hpdV > 0) signals.push({
    label: `${hpdV} open HPD violations` + (hpdC > 0 ? ` (${hpdC} Class C)` : ""),
    detail: "Open violations from NYC Housing Preservation & Development. Class C = immediately hazardous. High counts may indicate deferred maintenance.",
    severity: hpdC > 5 ? "high" : "medium",
  });
  if (ecbV > 0) signals.push({
    label: `${ecbV} ECB violations` + (ecbBal > 0 ? ` ($${ecbBal.toLocaleString()} balance)` : ""),
    detail: "Active violations from the Environmental Control Board (OATH). These carry financial penalties.",
    severity: ecbBal > 10000 ? "high" : "medium",
  });
  if (hasLien) signals.push({
    label: "Tax lien on property",
    detail: "The city has placed a lien on this property for unpaid taxes or charges.",
    severity: "high",
  });
  if (hasLp) signals.push({
    label: `Lis pendens / judgment (${p.lis_pendens_count})`,
    detail: "A legal action (lawsuit or judgment) has been filed against this property in the last 5 years.",
    severity: "high",
  });
  return signals;
}
