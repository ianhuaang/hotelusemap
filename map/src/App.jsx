import { useEffect, useRef, useState, useCallback, useMemo, Fragment } from "react";
import maplibregl from "maplibre-gl";

// Browser-exposed by design (Vite inlines it). Restrict by HTTP referrer + API in
// the Google Cloud console — that, not secrecy, is the control for a client-side key.
const GOOGLE_MAPS_API_KEY = import.meta.env.VITE_GOOGLE_MAPS_KEY || "";

const SEGMENTS = [
  {
    key: "transient", label: "Transient capacity", color: "#8b5cf6", defaultOn: true,
    info: "Buildings with HPD Class B (transient) rooms but no known active hotel operator. The primary sourcing targets.",
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

// buildings.geojson features are MultiPolygon footprints, but the clustered dot
// layer and search results hand back Points. Callers get a [lng, lat] either way.
// Vertex mean of the outer ring — an approximation, but consistent with the dots.
function featureCentroid(f) {
  const coords = f?.geometry?.coordinates;
  if (!coords) return null;
  if (f.geometry.type === "Point") return coords;
  const ring = f.geometry.type === "MultiPolygon" ? coords[0][0] : coords[0];
  if (!ring || !ring.length) return null;
  let cx = 0, cy = 0;
  for (const [x, y] of ring) { cx += x; cy += y; }
  return [cx / ring.length, cy / ring.length];
}

function estRooms(p) {
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

const SEGMENT_COLORS = Object.fromEntries(SEGMENTS.map((s) => [s.key, s.color]));

function segmentColor(segment) {
  return SEGMENT_COLORS[segment] || "#94a3b8";
}

function buildColorExpr() {
  const stops = [];
  for (const [seg, color] of Object.entries(SEGMENT_COLORS)) {
    stops.push(seg, color);
  }
  return ["match", ["get", "segment"], ...stops, "#94a3b8"];
}

function buildOpacityExpr() {
  return [
    "match",
    ["get", "confidence"],
    "high", 0.85,
    "medium", 0.65,
    "low", 0.5,
    0.5,
  ];
}

const CLUSTER_ZOOM_THRESHOLD = 14; // below this: clusters; above: footprints


function computeScore(p) {
  let score = 0;
  const hasClassB = (p.hpd_class_b || 0) > 0;
  const hasH = (p.bldgclass || "").startsWith("H");
  if (hasClassB) score += 35;
  if (hasH) score += 25;
  if (p.dob_has_r1) score += 15;
  // A final C of O is the strongest evidence transient use is approved, so it
  // scores highest. coo_has_temporary only means "a temporary one appears
  // somewhere in the history" — 19 West 103 Street has 24 C of O records, one
  // temporary, and was collecting the full bonus while buildings with a clean
  // final C of O collected nothing. Judge the latest record instead.
  const cooType = p.coo_latest_type || "";
  if (cooType === "Final" || cooType.startsWith("Renewal")) score += 10;
  else if (cooType === "Temporary" || cooType === "Initial") score += 6;
  if ((p.permit_transient_strong || 0) >= 1) score += 8;
  // dob_r1_filing_count is NOT scored: it counts the same DOB filings that set
  // dob_has_r1, so scoring both awarded 22 of 100 points for one signal.
  // Filing volume is surfaced in ScoreExplainer as confidence, not points.
  // Zoning penalty only for buildings without existing transient rights
  if (p.zoning_hotel_permitted === "not_permitted" && !hasClassB && !hasH) score = Math.max(0, score - 15);
  return Math.min(score, 100);
}

function buildFilter(activeSegments, showPriorOps, showReversion, minUnits, minClassB, filters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted) {
  const allowedSegs = Object.entries(activeSegments).filter(([, v]) => v).map(([k]) => k);
  const segFilter = ["in", ["get", "segment"], ["literal", allowedSegs]];
  const priorOpFilter = ["==", ["get", "has_prior_op"], true];
  const reversionFilter = ["==", ["get", "has_reversion"], true];

  const overlayParts = [];
  if (showPriorOps) overlayParts.push(priorOpFilter);
  if (showReversion) overlayParts.push(reversionFilter);

  const visibilityFilter = overlayParts.length > 0
    ? ["any", segFilter, ...overlayParts]
    : segFilter;

  const alwaysShowFilter = overlayParts.length > 0
    ? ["any", ...overlayParts]
    : ["literal", false];

  const conditions = [
    visibilityFilter,
    ["any",
      [">=",
        ["case",
          [">", ["to-number", ["get", "hpd_class_b"], 0], 0], ["to-number", ["get", "hpd_class_b"], 0],
          [">", ["to-number", ["get", "coo_dwelling_units"], 0], 0], ["to-number", ["get", "coo_dwelling_units"], 0],
          ["all", ["==", ["slice", ["get", "bldgclass"], 0, 1], "H"], [">=", ["to-number", ["get", "numfloors"], 0], 3]],
            ["*", ["to-number", ["get", "numfloors"], 0], 15],
          [">", ["to-number", ["get", "unitsres"], 0], 0], ["to-number", ["get", "unitsres"], 0],
          ["to-number", ["get", "unitstotal"], 0],
        ],
        minUnits,
      ],
      alwaysShowFilter,
    ],
  ];

  if (minClassB > 0) {
    conditions.push(["any",
      [">=", ["to-number", ["get", "hpd_class_b"], 0], minClassB],
      ["==", ["get", "segment"], "partial"],
      ["==", ["get", "segment"], "active_hotel"],
      alwaysShowFilter,
    ]);
  }

  const refinements = [];
  if (filters.filterTempCoo) {
    refinements.push(["==", ["get", "coo_has_temporary"], true]);
  }
  if (filters.filterHasClassB) {
    refinements.push([">", ["get", "hpd_class_b"], 0]);
  }
  if (filters.filterMultiOwner) {
    refinements.push([">", ["get", "owner_portfolio_size"], 1]);
  }
  if (filters.filterRecentSale) {
    refinements.push([">=", ["get", "last_sale_date"], filters._recentSaleCutoff]);
  }
  if (filters.filterCommercialZone) {
    refinements.push(["any",
      ["==", ["slice", ["get", "zonedist1"], 0, 1], "C"],
      ["==", ["slice", ["get", "zonedist1"], 0, 1], "M"],
    ]);
  }
  if (distressOnly) {
    refinements.push(["any",
      ["==", ["get", "has_tax_lien"], true],
      ["==", ["get", "has_lis_pendens"], true],
      [">", ["get", "hpd_open_violations"], 0],
      [">", ["get", "ecb_open_violations"], 0],
    ]);
  }
  if (noOperatorOnly) {
    refinements.push(["!", ["has", "hotel_name"]]);
  }
  if (hideBrandTypes.size > 0) {
    const hiddenTypes = [...hideBrandTypes];
    for (const bt of hiddenTypes) {
      refinements.push(["!=", ["get", "brand_type"], bt]);
    }
  }
  if (hideCondos) {
    refinements.push(["!=", ["get", "is_condo"], true]);
  }
  if (hideRestricted) {
    refinements.push(["!=", ["get", "restricted_class"], true]);
  }

  for (const ref of refinements) {
    conditions.push(["any", ref, alwaysShowFilter]);
  }

  return ["all", ...conditions];
}

// --- CSV export ---
const CSV_COLUMNS = [
  { key: "address", label: "Address" },
  { key: "neighborhood", label: "Neighborhood" },
  { key: "est_rooms", label: "Est. Rooms" },
  { key: "room_source", label: "Room Source" },
  { key: "has_prior_op", label: "Prior Operator" },
  { key: "coo_has_temporary", label: "Temp C of O" },
  { key: "bbl", label: "BBL" },
  { key: "tier", label: "Tier" },
  { key: "confidence", label: "Confidence" },
  { key: "bldgclass", label: "Building Class" },
  { key: "unitsres", label: "Residential Units" },
  { key: "unitstotal", label: "Total Units" },
  { key: "numfloors", label: "Floors" },
  { key: "hpd_class_a", label: "HPD Class A" },
  { key: "hpd_class_b", label: "HPD Class B" },
  { key: "zonedist1", label: "Zoning" },
  { key: "ownername", label: "Owner" },
  { key: "owner_portfolio_size", label: "Owner Portfolio Size" },
  { key: "acris_deed_owner", label: "ACRIS Deed Owner" },
  { key: "acris_deed_date", label: "Deed Date" },
  { key: "acris_deed_address", label: "Deed Owner Address" },
  { key: "acris_borrower", label: "ACRIS Mortgage Borrower" },
  { key: "acris_lender", label: "ACRIS Lender" },
  { key: "operator_name", label: "Operator" },
  { key: "operator_source", label: "Operator Source" },
  { key: "hpd_managing_agent_corp", label: "HPD Managing Agent" },
  { key: "hpd_head_officer", label: "HPD Head Officer" },
  { key: "hotel_name", label: "Hotel Name" },
  { key: "hotel_phone", label: "Hotel Phone" },
  { key: "hotel_website", label: "Hotel Website" },
  { key: "prior_operator_name", label: "Prior Operator" },
  { key: "prior_operator_notes", label: "Prior Operator Notes" },
  { key: "has_tax_lien", label: "Tax Lien" },
  { key: "has_lis_pendens", label: "Lis Pendens" },
  { key: "hpd_open_violations", label: "HPD Open Violations" },
  { key: "hpd_class_c_violations", label: "HPD Class C Violations" },
  { key: "ecb_open_violations", label: "ECB Violations" },
  { key: "ecb_total_balance", label: "ECB Balance ($)" },
  { key: "last_sale_date", label: "Last Sale Date" },
  { key: "last_sale_price", label: "Last Sale Price" },
  { key: "permit_count", label: "DOB Permits" },
  { key: "coo_count", label: "C of O Records" },
  { key: "coo_has_temporary", label: "Has Temp C of O" },
  { key: "coo_dwelling_units", label: "C of O Dwelling Units" },
  { key: "is_landmark", label: "LPC Landmark" },
  { key: "historic_district", label: "Historic District" },
  { key: "height_roof", label: "Roof Height (ft)" },
  { key: "bin", label: "BIN" },
  { key: "mortgage_age_years", label: "Mortgage Age (yrs)" },
  { key: "mortgage_approaching_maturity", label: "Mortgage Maturing" },
  { key: "reason_codes", label: "Reason Codes" },
];

function parseJsonProp(val) {
  if (typeof val === "string") {
    try { return JSON.parse(val); } catch { return val; }
  }
  return val;
}

function parseBbl(bbl) {
  const s = String(bbl || "").padStart(10, "0");
  return { borough: s[0], block: s.slice(1, 6), lot: s.slice(6, 10) };
}

function buildRecordLinks(bbl, bin) {
  const { borough, block, lot } = parseBbl(bbl);
  return {
    hpd: `https://hpdonline.nyc.gov/hpdonline/building/search-results?boroId=${borough}&block=${block}&lot=${lot}`,
    dob: bin
      ? `https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?bin=${bin}&requestid=1`
      : `https://a810-bisweb.nyc.gov/bisweb/PropertyBrowseByBBLServlet?allborough=${borough}&allblock=${block}&alllot=${lot}&go5=+GO+&requestid=0`,
    acris: `https://a836-acris.nyc.gov/bblsearch/bblsearch.asp?borough=${borough}&block=${block}&lot=${lot}`,
  };
}

function exportToCsv(features) {
  const escCsv = (v) => {
    const s = String(v ?? "");
    return s.includes(",") || s.includes('"') || s.includes("\n")
      ? `"${s.replace(/"/g, '""')}"`
      : s;
  };

  const header = CSV_COLUMNS.map((c) => c.label).join(",");
  const rows = features.map((f) => {
    const p = f.properties;
    const priorOp = parseJsonProp(p.prior_operator);
    const reasons = parseJsonProp(p.reason_codes) || [];

    const rooms = estRooms(p);
    const row = {
      ...p,
      est_rooms: rooms.value,
      room_source: rooms.source,
      has_prior_op: p.has_prior_op ? "Yes" : "",
      numfloors: p.numfloors ? Math.round(p.numfloors) : "",
      height_roof: p.height_roof ? Math.round(p.height_roof) : "",
      prior_operator_name: priorOp?.name || "",
      prior_operator_notes: priorOp?.notes || "",
      has_tax_lien: p.has_tax_lien ? "Yes" : "",
      has_lis_pendens: p.has_lis_pendens ? "Yes" : "",
      coo_has_temporary: p.coo_has_temporary ? "Yes" : "",
      mortgage_approaching_maturity: p.mortgage_approaching_maturity ? "Yes" : "",
      reason_codes: Array.isArray(reasons) ? reasons.join("; ") : reasons,
    };
    return CSV_COLUMNS.map((c) => escCsv(row[c.key])).join(",");
  });

  const csv = [header, ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `nyc_transient_export_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --- Components ---



function DistressRow({ signal }) {
  const [open, setOpen] = useState(false);
  const bg = signal.severity === "high" ? "bg-red-50" : "bg-amber-50";
  const dot = signal.severity === "high" ? "bg-red-500" : "bg-amber-400";
  const text = signal.severity === "high" ? "text-red-800" : "text-amber-900";
  return (
    <div className={`${bg} rounded-lg overflow-hidden`}>
      <button
        onClick={() => setOpen(!open)}
        className={`w-full flex items-center gap-2 px-3 py-2 cursor-pointer text-left`}
      >
        <span className={`w-2 h-2 rounded-full ${dot} shrink-0`} />
        <span className={`text-xs ${text} font-medium flex-1`}>{signal.label}</span>
        <span className={`text-[10px] ${text} opacity-60 transition-transform ${open ? "rotate-90" : ""}`}>&#9654;</span>
      </button>
      {open && (
        <div className="px-3 pb-2 -mt-0.5">
          <p className="text-[11px] text-gray-600 leading-relaxed">{signal.detail}</p>
        </div>
      )}
    </div>
  );
}

function useNotes() {
  const STORAGE_KEY = "nyc-transient-notes";
  const load = () => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch { return {}; }
  };
  const [notes, setNotes] = useState(load);
  const save = (bbl, text) => {
    setNotes((prev) => {
      const next = { ...prev };
      if (text.trim()) next[bbl] = text.trim();
      else delete next[bbl];
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  };
  return { notes, save };
}

// --- CRM Status ---

const CRM_STATUSES = [
  { value: "", label: "—", color: "" },
  { value: "not_contacted", label: "Not contacted", color: "bg-gray-100 text-gray-600" },
  { value: "reached_out", label: "Reached out", color: "bg-blue-100 text-blue-700" },
  { value: "meeting_set", label: "Meeting set", color: "bg-emerald-100 text-emerald-700" },
  { value: "passed", label: "Passed", color: "bg-red-100 text-red-600" },
];

function useCrmStatuses() {
  const STORAGE_KEY = "nyc-transient-crm-statuses";
  const load = () => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch { return {}; }
  };
  const [statuses, setStatuses] = useState(load);
  const setStatus = (key, value) => {
    setStatuses((prev) => {
      const next = { ...prev };
      if (value) next[key] = value;
      else delete next[key];
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  };
  return { statuses, setStatus };
}

function NoteEditor({ bbl, notes, onSave }) {
  const existing = notes[bbl] || "";
  const [text, setText] = useState(existing);
  const [editing, setEditing] = useState(false);
  const changed = text.trim() !== existing;

  useEffect(() => { setText(notes[bbl] || ""); setEditing(false); }, [bbl, notes]);

  if (!editing && !existing) {
    return (
      <button
        onClick={() => setEditing(true)}
        className="w-full text-left px-3 py-2 text-xs text-gray-400 bg-gray-50 rounded-lg hover:bg-gray-100 cursor-pointer transition-colors"
      >
        + Add a note...
      </button>
    );
  }

  if (!editing) {
    return (
      <div
        onClick={() => setEditing(true)}
        className="px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg cursor-pointer hover:bg-amber-100 transition-colors"
      >
        <div className="text-[10px] text-amber-600 font-semibold uppercase tracking-wide mb-0.5">Note</div>
        <div className="text-xs text-amber-900 whitespace-pre-wrap">{existing}</div>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Add notes about this building..."
        rows={3}
        autoFocus
        className="w-full px-3 py-2 text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded-lg outline-none focus:ring-2 focus:ring-gray-300 resize-none"
      />
      <div className="flex gap-1.5 justify-end">
        <button
          onClick={() => { setText(existing); setEditing(false); }}
          className="px-2.5 py-1 text-[11px] text-gray-500 hover:text-gray-700 cursor-pointer"
        >
          Cancel
        </button>
        <button
          onClick={() => { onSave(bbl, text); setEditing(false); }}
          disabled={!changed}
          className={`px-2.5 py-1 text-[11px] rounded-md cursor-pointer transition-colors ${
            changed ? "bg-gray-800 text-white hover:bg-gray-700" : "bg-gray-200 text-gray-400 cursor-not-allowed"
          }`}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function ScoreExplainer({ p }) {
  const [open, setOpen] = useState(false);

  const signals = [
    { label: "HPD Class B rooms registered", pts: 35, hit: (p.hpd_class_b || 0) > 0 },
    { label: "Hotel building class (H-series)", pts: 25, hit: (p.bldgclass || "").startsWith("H") },
    { label: "DOB R-1 transient occupancy", pts: 15, hit: !!p.dob_has_r1 },
    { label: "Final C of O on file", pts: 10, hit: (p.coo_latest_type || "") === "Final" || (p.coo_latest_type || "").startsWith("Renewal") },
    { label: "Temporary C of O only", pts: 6, hit: (p.coo_latest_type || "") === "Temporary" || (p.coo_latest_type || "") === "Initial" },
    { label: "DOB transient permit activity", pts: 8, hit: (p.permit_transient_strong || 0) >= 1 },
  ];
  const hasGrandfathered = (p.hpd_class_b || 0) > 0 || (p.bldgclass || "").startsWith("H");
  if (p.zoning_hotel_permitted === "not_permitted" && !hasGrandfathered) signals.push({ label: "Residential zoning (penalty)", pts: -15, hit: true, penalty: true });
  if (p.zoning_hotel_permitted === "not_permitted" && hasGrandfathered) signals.push({ label: "Residential zoning (grandfathered)", pts: 0, hit: true });
  // Same filings that earned the R-1 points above — shown as corroboration, worth no points.
  const r1Filings = p.dob_r1_filing_count || 0;
  if (r1Filings >= 3) signals.push({ label: `Corroborated by ${r1Filings} R-1 filings`, pts: 0, hit: true });

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 cursor-pointer text-left"
      >
        <span className={`text-[10px] text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}>&#9654;</span>
        <span className="text-[10px] text-gray-400 hover:text-gray-600">Why this score</span>
      </button>
      {open && (
        <div className="mt-2 space-y-0.5">
          {signals.map((sig) => (
            <div key={sig.label} className="flex items-center gap-2">
              <span className={`text-[11px] ${sig.penalty && sig.hit ? "text-red-600" : sig.hit ? "text-gray-800" : "text-gray-300"}`}>
                {sig.hit ? (sig.pts < 0 ? "" : "+") : "\u00A0\u00A0"}{sig.hit ? sig.pts : 0}
              </span>
              <span className={`text-[11px] ${sig.penalty && sig.hit ? "text-red-600" : sig.hit ? "text-gray-700" : "text-gray-300"}`}>
                {sig.label}
              </span>
              {sig.hit && !sig.penalty && <span className="text-[10px] text-emerald-500">&#10003;</span>}
              {sig.hit && sig.penalty && <span className="text-[10px] text-red-500">&#9888;</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const M_PER_DEG_LAT = 111320;
const mPerDegLng = (lat) => M_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180);

// Probe radii in metres out from the footprint centroid. Querying the centroid
// itself returns whatever panorama is nearest to a point *inside* the building —
// in practice a shop, lobby or hotel-room interior about two thirds of the time.
// The street-level capture we want sits 20-50m away out on the roadway.
const PROBE_RINGS = [30, 48];
const panoCache = new Map();

async function findStreetPano(lng, lat) {
  const pts = [];
  for (const r of PROBE_RINGS) {
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4;
      pts.push([Math.cos(a) * r, Math.sin(a) * r]);
    }
  }
  const mLng = mPerDegLng(lat);
  const found = new Map();
  let denied = false;
  // Metadata is the unlimited-free SKU, so 16 parallel probes cost nothing but
  // one round trip. Only the single chosen image below is billable.
  await Promise.all(pts.map(async ([dx, dy]) => {
    const qlat = lat + dy / M_PER_DEG_LAT;
    const qlng = lng + dx / mLng;
    try {
      const r = await fetch(`https://maps.googleapis.com/maps/api/streetview/metadata?location=${qlat},${qlng}&source=outdoor&key=${GOOGLE_MAPS_API_KEY}`);
      const d = await r.json();
      if (d.status === "REQUEST_DENIED") { denied = true; return; }
      if (d.status !== "OK" || !d.pano_id || !d.location) return;
      const dist = Math.hypot((d.location.lat - lat) * M_PER_DEG_LAT, (d.location.lng - lng) * mLng);
      found.set(d.pano_id, { id: d.pano_id, loc: d.location, dist, official: /Google/.test(d.copyright || "") });
    } catch { /* a single failed probe is not fatal */ }
  }));
  if (denied && !found.size) return { denied: true, pano: null };
  if (!found.size) return { denied: false, pano: null };
  // Official Google capture beats a user photo sphere (spheres are often indoors);
  // ~36m frames a whole facade without a neighbouring building intruding; anything
  // closer than 15m is probably still inside the building.
  const score = (c) => (c.official ? 0 : 100) + (c.dist < 15 ? 300 : 0) + Math.abs(c.dist - 36);
  return { denied: false, pano: [...found.values()].sort((a, b) => score(a) - score(b))[0] };
}

function StreetViewThumb({ lng, lat, label }) {
  const coordKey = lat == null || lng == null ? null : `${lat.toFixed(6)},${lng.toFixed(6)}`;
  // State is keyed to the coordinates so switching buildings reads as "loading" in
  // the same render, rather than briefly showing the previous building's photo.
  const [res, setRes] = useState({ key: null });

  useEffect(() => {
    if (!GOOGLE_MAPS_API_KEY || !coordKey) return;
    if (panoCache.has(coordKey)) { setRes({ key: coordKey, ...panoCache.get(coordKey) }); return; }
    let cancelled = false;
    findStreetPano(lng, lat).then((r) => {
      panoCache.set(coordKey, r);
      if (!cancelled) setRes({ key: coordKey, ...r });
    });
    return () => { cancelled = true; };
  }, [coordKey, lng, lat]);

  if (!GOOGLE_MAPS_API_KEY || !coordKey) return null;

  const ready = res.key === coordKey;
  if (!ready || !res.pano) {
    const msg = !ready ? "Loading Street View\u2026"
      : res.denied ? "Street View unavailable \u2014 key not authorized for this domain"
      : "No Street View coverage at this address";
    return <div className="px-4 py-2 border-b border-gray-100 text-[11px] text-gray-400">{msg}</div>;
  }

  // Aim the camera from the chosen street panorama back at the building centroid.
  const { id, loc } = res.pano;
  const heading = ((Math.atan2((lng - loc.lng) * mPerDegLng(lat), (lat - loc.lat) * M_PER_DEG_LAT) * 180) / Math.PI + 360) % 360;
  const h = heading.toFixed(1);
  // Requested at 640w and displayed at 384w so it stays crisp on retina.
  const img = `https://maps.googleapis.com/maps/api/streetview?size=640x280&pano=${id}&heading=${h}&fov=95&pitch=8&key=${GOOGLE_MAPS_API_KEY}`;
  const link = `https://www.google.com/maps/@?api=1&map_action=pano&pano=${id}&heading=${h}&pitch=8`;

  return (
    <a
      href={link}
      target="_blank"
      rel="noopener noreferrer"
      className="group relative block border-b border-gray-100 overflow-hidden"
    >
      <img
        src={img}
        alt={`Street View of ${label || "this building"}`}
        loading="lazy"
        className="w-full h-40 object-cover"
      />
      <div className="absolute inset-0 group-hover:bg-black/25 transition-colors" />
      <span className="absolute bottom-2 right-2 px-2 py-0.5 rounded-md bg-white/90 text-[10px] font-medium text-gray-700 opacity-0 group-hover:opacity-100 transition-opacity">
        Open in Street View &rarr;
      </span>
    </a>
  );
}

function DetailPanel({ feature, onClose, onAddToList, isInList, notes, onSaveNote, crmStatuses, setCrmStatus, allFeatures }) {
  if (!feature) return null;
  const p = feature.properties;
  const reasonCodes = parseJsonProp(p.reason_codes) || [];
  const blockers = parseJsonProp(p.blockers) || [];
  const priorOp = parseJsonProp(p.prior_operator);
  const [lng, lat] = featureCentroid(feature) || [];

  return (
    <div className="fixed top-4 right-4 w-96 max-h-[calc(100vh-2rem)] overflow-y-auto bg-white rounded-xl shadow-2xl border border-gray-200 z-20">
      <div className="flex items-center justify-between p-4 border-b border-gray-100">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-gray-900 truncate pr-2">{p.hotel_name || p.address}</h2>
          {p.hotel_name && <div className="text-xs text-gray-500 mt-0.5">{p.address}</div>}
          {p.neighborhood && <div className="text-xs text-gray-400 mt-0.5">{p.neighborhood}</div>}
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none cursor-pointer shrink-0">&times;</button>
      </div>

      <StreetViewThumb lng={lng} lat={lat} label={p.hotel_name || p.address} />

      <div className="p-4 space-y-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className="inline-block px-2.5 py-1 rounded-full text-xs font-semibold text-white"
            style={{ backgroundColor: segmentColor(p.segment) }}
          >
            {(p.segment || p.tier || "").replace(/_/g, " ")}
          </span>
          {p.has_reversion && (
            <span className="bg-red-500 text-white text-[10px] font-semibold px-1.5 py-0.5 rounded-md">
              Reversion
            </span>
          )}
          {p.has_prior_op && (
            <span className="bg-purple-500 text-white text-[10px] font-semibold px-1.5 py-0.5 rounded-md">
              Prior operator
            </span>
          )}
          <button
            onClick={() => onAddToList(feature)}
            className={`ml-auto px-2.5 py-1 text-xs rounded-md cursor-pointer transition-colors ${
              isInList
                ? "bg-gray-100 text-gray-500 hover:bg-red-50 hover:text-red-600"
                : "bg-gray-800 text-white hover:bg-gray-700"
            }`}
          >
            {isInList ? "Remove from list" : "+ Add to list"}
          </button>
        </div>

        {/* Composite deal score */}
        {(() => {
          const s = computeScore(p);
          const bg = s >= 60 ? "bg-emerald-600" : s >= 35 ? "bg-amber-500" : "bg-gray-400";
          return (
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3">
              <div className="flex items-center justify-between mb-1">
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Legal Score</div>
                <span className={`${bg} text-white text-sm font-bold px-2 py-0.5 rounded tabular-nums`}>{s}</span>
              </div>
              <div className="mb-2">
                <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
                  <div className="h-full rounded-full" style={{ width: `${s}%`, backgroundColor: "#16a34a" }} />
                </div>
              </div>
              <ScoreExplainer p={p} />
            </div>
          );
        })()}

        {priorOp && (
          <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
            <div className="text-xs font-semibold text-purple-700 uppercase tracking-wide mb-1">Prior flex operator</div>
            <div className="text-sm text-purple-900">{priorOp.name}</div>
            <div className="text-xs text-purple-600 mt-0.5">{priorOp.notes}</div>
            <div className="text-[10px] text-purple-400 mt-1">Legality unverified</div>
          </div>
        )}

        {p.has_reversion && (() => {
          const rev = parseJsonProp(p.reversion);
          return rev && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3">
              <div className="text-xs font-semibold text-red-700 uppercase tracking-wide mb-1">Post-2021 Reversion Opportunity</div>
              <div className="text-sm text-red-900 font-medium">{rev.former_hotel}</div>
              <div className="text-xs text-red-600 mt-0.5">Closed {rev.closure_year}</div>
              <div className="text-xs text-red-500 mt-1">{rev.note}</div>
              <div className="text-[10px] text-red-400 mt-1">Hotel class preserved — can revert without CPC special permit</div>
            </div>
          );
        })()}

        {/* Legal Feasibility */}
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3">
          <div className="text-xs font-semibold text-emerald-700 uppercase tracking-wide mb-2">Legal Feasibility</div>
          <div className="space-y-1.5">
            {(() => {
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
                items.push({ icon: "check", text: "Active DCWP hotel license" });
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
              return items.map((item, i) => (
                <div key={i} className="flex items-start gap-1.5">
                  <span className={`mt-0.5 shrink-0 text-[10px] ${
                    item.icon === "check" ? "text-emerald-600" : item.icon === "warn" ? "text-amber-600" : "text-gray-400"
                  }`}>
                    {item.icon === "check" ? "✓" : item.icon === "warn" ? "⚠" : "•"}
                  </span>
                  <span className="text-[11px] text-gray-700">{item.text}</span>
                </div>
              ));
            })()}
          </div>
          {reasonCodes.length > 0 && (
            <div className="mt-2 pt-2 border-t border-emerald-100">
              <div className="flex flex-wrap gap-1">
                {reasonCodes.map((code) => (
                  <span key={code} className="bg-emerald-100 text-emerald-700 text-[10px] px-1.5 py-0.5 rounded">{code}</span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Stats */}
        {(() => {
          const rooms = estRooms(p);
          const sourceExplain = {
            "HPD Class B": "Transient (Class B) rooms registered with HPD under the Multiple Dwelling Law. Renewed annually by building owners — the most current signal of active transient capacity.",
            "C of O": "From DOB Certificate of Occupancy — the approved dwelling unit count. Reliable but may include residential units.",
            "Floor est.": `Estimated at ~15 rooms/floor × ${Math.round(p.numfloors || 0)} floors. No HPD registration or C of O on file for this hotel.`,
            "PLUTO": "From Dept. of Finance tax lot data. Counts residential dwelling units, not hotel rooms — accurate for residential buildings but undercounts hotels.",
            "Unknown": "No room count data available from any source.",
          };
          return (
            <div className="grid grid-cols-3 gap-3">
              <div>
                <div className="text-[10px] text-gray-400 uppercase tracking-wide flex items-center gap-1">
                  Est. Rooms
                  <span className="relative group cursor-help">
                    <span className="inline-flex items-center justify-center w-3 h-3 rounded-full bg-gray-200 text-gray-500 text-[8px] font-bold">i</span>
                    <span className="absolute bottom-full left-0 mb-1 w-56 bg-gray-900 text-white text-[10px] leading-snug rounded-lg px-3 py-2 opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto transition-opacity z-50 shadow-lg">
                      <span className="font-semibold text-blue-300">Source: {rooms.source}</span><br/>
                      {sourceExplain[rooms.source]}
                    </span>
                  </span>
                </div>
                <div className="text-sm font-medium text-gray-900">{rooms.value}</div>
              </div>
              <Stat label="Floors" value={p.numfloors ? Math.round(p.numfloors) : "—"} />
              <Stat label="Zoning" value={p.zonedist1 || "—"} />
            </div>
          );
        })()}

        {/* Policy considerations */}
        {(() => {
          const considerations = [];
          if (p.is_landmark) {
            considerations.push({
              text: `LPC Individual Landmark${p.landmark_name ? ` — ${p.landmark_name}` : ""}`,
              severity: "medium",
            });
          }
          if (p.historic_district) {
            considerations.push({
              text: `Historic District — ${p.historic_district}`,
              severity: "medium",
            });
          }
          if (p.is_condo) {
            considerations.push({
              text: "Condominium — requires board approval or commercial condo owner negotiation",
              severity: "medium",
            });
          }
          if (p.zoning_hotel_permitted === "not_permitted") {
            considerations.push({
              text: `Residential zoning (${p.zonedist1 || "unknown"}) — hotel use not permitted for new operators, but existing Class B rooms are grandfathered`,
              severity: "medium",
            });
          }
          if (blockers.length > 0) {
            blockers.forEach(b => {
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
              });
            });
          }
          if (considerations.length === 0) return null;
          return (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">Policy Considerations</div>
              <div className="space-y-1">
                {considerations.map((c, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <span className={`mt-0.5 shrink-0 w-1.5 h-1.5 rounded-full ${
                      c.severity === "high" ? "bg-red-500" : c.severity === "medium" ? "bg-amber-500" : "bg-gray-400"
                    }`} />
                    <span className={`text-[11px] ${c.severity === "high" ? "text-red-700" : "text-gray-600"}`}>{c.text}</span>
                  </div>
                ))}
              </div>
            </div>
          );
        })()}

        {/* Operator identification */}
        {(p.operator_name || p.hotel_name || p.hpd_managing_agent_corp) && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Operator</div>
            {p.operator_name && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-800 font-medium">{p.operator_name}</span>
                <span className="text-[9px] bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded">
                  {p.operator_source === "dcwp_license" ? "DCWP license" : p.operator_source === "hpd_managing_agent" ? "HPD agent" : p.operator_source === "google_places" ? "Google" : p.operator_source === "ground_truth" ? "Known" : ""}
                </span>
              </div>
            )}
            {p.hpd_managing_agent_corp && p.hpd_managing_agent_corp !== p.operator_name && (
              <div className="mt-1">
                <span className="text-[10px] text-gray-400">HPD managing agent: </span>
                <span className="text-[11px] text-gray-600">{p.hpd_managing_agent_corp}</span>
                {p.hpd_managing_agent && <span className="text-[10px] text-gray-400 ml-1">({p.hpd_managing_agent})</span>}
              </div>
            )}
            {p.hotel_name && p.hotel_name !== p.operator_name && (
              <div className="mt-1">
                <span className="text-[10px] text-gray-400">Hotel name: </span>
                <span className="text-[11px] text-gray-600">{p.hotel_name}</span>
              </div>
            )}
            <div className="flex gap-3 mt-1">
              {p.hotel_phone && (
                <a href={`tel:${p.hotel_phone}`} className="text-[11px] text-blue-600 hover:underline">{p.hotel_phone}</a>
              )}
              {p.hotel_website && (
                <a href={p.hotel_website.startsWith("http") ? p.hotel_website : `https://${p.hotel_website}`} target="_blank" rel="noopener noreferrer" className="text-[11px] text-blue-600 hover:underline">Website</a>
              )}
            </div>
          </div>
        )}

        {/* Owner + portfolio + ACRIS */}
        {(p.ownername || p.acris_deed_owner) && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Owner</div>
            <div className="text-sm text-gray-700">{p.ownername || "—"}</div>
            {p.owner_portfolio_size > 1 && (
              <div className="mt-1 text-[10px] text-blue-600 font-medium">
                Owns {p.owner_portfolio_size} buildings in pipeline
              </div>
            )}
            {(p.acris_deed_owner || p.acris_borrower) && (
              <div className="mt-2 bg-gray-50 rounded-lg p-2.5 space-y-1.5">
                <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">ACRIS records</div>
                {p.acris_deed_owner && (
                  <div>
                    <span className="text-[10px] text-gray-400">Deed owner: </span>
                    <span className="text-[11px] text-gray-700 font-medium">{p.acris_deed_owner}</span>
                    {p.acris_deed_date && <span className="text-[10px] text-gray-400 ml-1">({p.acris_deed_date})</span>}
                    {p.acris_deed_address && <div className="text-[10px] text-gray-400 ml-0">{p.acris_deed_address}</div>}
                  </div>
                )}
                {p.acris_borrower && p.acris_borrower !== p.acris_deed_owner && (
                  <div>
                    <span className="text-[10px] text-gray-400">Mortgage borrower: </span>
                    <span className="text-[11px] text-gray-700 font-medium">{p.acris_borrower}</span>
                  </div>
                )}
                {p.acris_lender && (
                  <div>
                    <span className="text-[10px] text-gray-400">Lender: </span>
                    <span className="text-[10px] text-gray-500">{p.acris_lender}</span>
                  </div>
                )}
                {p.acris_mtge_date && (
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-gray-400">Mortgage: </span>
                    <span className="text-[10px] text-gray-500">{p.acris_mtge_date}</span>
                    {p.mortgage_amount && Number(p.mortgage_amount) > 0 && (
                      <span className="text-[10px] text-gray-500">(${Number(p.mortgage_amount).toLocaleString()})</span>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Last sale */}
        {p.last_sale_date && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Last sale</div>
            <div className="text-sm text-gray-700">
              {p.last_sale_price > 0 ? `$${Number(p.last_sale_price).toLocaleString()}` : "Undisclosed"}{" "}
              <span className="text-gray-400">on {p.last_sale_date}</span>
            </div>
            {p.sale_count > 1 && (
              <div className="text-[10px] text-gray-400 mt-0.5">{p.sale_count} sales in last 10 years</div>
            )}
          </div>
        )}

        {/* DOB permits */}
        {(() => {
          const permits = parseJsonProp(p.permits) || [];
          if (permits.length === 0) return null;
          return (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                DOB permits ({p.permit_count})
              </div>
              <div className="space-y-1.5">
                {permits.slice(0, 3).map((permit, i) => (
                  <div key={i} className="bg-gray-50 rounded-lg p-2">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-semibold text-gray-500 bg-gray-200 px-1.5 py-0.5 rounded">
                        {permit.job_type_label || permit.job_type}
                      </span>
                      <span className="text-[10px] text-gray-400">{permit.action_date}</span>
                      {permit.cost > 0 && (
                        <span className="text-[10px] text-gray-400 ml-auto">${Number(permit.cost).toLocaleString()}</span>
                      )}
                    </div>
                    {permit.description && (
                      <div className="text-[11px] text-gray-600 mt-0.5 line-clamp-2">{permit.description}</div>
                    )}
                    <div className="text-[10px] text-gray-400 mt-0.5">{permit.status}</div>
                  </div>
                ))}
                {permits.length > 3 && (
                  <div className="text-[10px] text-gray-400">+ {permits.length - 3} more</div>
                )}
              </div>
            </div>
          );
        })()}

        {/* C of O history */}
        {(() => {
          const coos = parseJsonProp(p.coo_records) || [];
          const hasTmp = p.coo_has_temporary;
          if (p.coo_count === 0 && !hasTmp) return null;
          return (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
                Certificate of Occupancy ({p.coo_count})
              </div>
              {hasTmp && (
                <div className="mb-1.5 flex items-center gap-1.5">
                  <span className="inline-block w-2 h-2 rounded-full bg-amber-400" />
                  <span className="text-[11px] text-amber-700 font-medium">Has Temporary C of Os — active construction</span>
                </div>
              )}
              {p.coo_dwelling_units != null && (
                <div className="text-[11px] text-gray-600 mb-1.5">
                  C of O dwelling units: <span className="font-semibold">{p.coo_dwelling_units}</span>
                  {p.unitsres > 0 && p.coo_dwelling_units !== p.unitsres && (
                    <span className="text-amber-600 ml-1">(PLUTO says {p.unitsres})</span>
                  )}
                </div>
              )}
              <div className="space-y-1.5">
                {coos.slice(0, 3).map((coo, i) => (
                  <div key={i} className="bg-gray-50 rounded-lg p-2">
                    <div className="flex items-center gap-1.5">
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                        coo.co_type === "Temporary" ? "text-amber-700 bg-amber-100" : "text-gray-500 bg-gray-200"
                      }`}>
                        {coo.co_type || "—"}
                      </span>
                      <span className="text-[10px] text-gray-400">{coo.issue_date}</span>
                      <span className="text-[10px] text-gray-500 ml-auto">{coo.job_type}</span>
                    </div>
                    {coo.dwelling_units && (
                      <div className="text-[10px] text-gray-500 mt-0.5">{coo.dwelling_units} dwelling units</div>
                    )}
                  </div>
                ))}
                {p.coo_count > 3 && (
                  <div className="text-[10px] text-gray-400">+ {p.coo_count - 3} more</div>
                )}
              </div>
            </div>
          );
        })()}

        {/* Due diligence links */}
        {(() => {
          const links = buildRecordLinks(p.bbl, p.bin);
          return (
            <div className="pt-2 border-t border-gray-100 space-y-2">
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Public records</div>
              <div className="flex gap-2">
                <a href={links.hpd} target="_blank" rel="noopener noreferrer"
                  className="flex-1 text-center px-2 py-1.5 bg-gray-50 hover:bg-gray-100 rounded-md text-[11px] font-medium text-blue-600 transition-colors">
                  HPD
                </a>
                <a href={links.dob} target="_blank" rel="noopener noreferrer"
                  className="flex-1 text-center px-2 py-1.5 bg-gray-50 hover:bg-gray-100 rounded-md text-[11px] font-medium text-blue-600 transition-colors">
                  DOB
                </a>
                <a href={links.acris} target="_blank" rel="noopener noreferrer"
                  className="flex-1 text-center px-2 py-1.5 bg-gray-50 hover:bg-gray-100 rounded-md text-[11px] font-medium text-blue-600 transition-colors">
                  ACRIS
                </a>
              </div>
              <div className="text-[10px] text-gray-400">
                BBL: {p.bbl} · BIN: {p.bin} · Pulled: {p.source_pulled_on}
              </div>
            </div>
          );
        })()}

        {/* Submarket context */}
        {p.neighborhood && allFeatures && (() => {
          const peers = allFeatures.filter(f => f.properties.neighborhood === p.neighborhood && f.properties.bbl !== p.bbl);
          if (peers.length < 3) return null;
          const legal = peers.filter(f => f.properties.tier === "legal_transient").length;
          const withLien = peers.filter(f => f.properties.has_tax_lien).length;
          const noOp = peers.filter(f => !f.properties.hotel_name).length;
          const landmark = peers.filter(f => f.properties.is_landmark || f.properties.historic_district).length;
          return (
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Submarket — {p.neighborhood}</div>
              <div className="flex gap-3 flex-wrap text-[11px]">
                <span className="text-gray-700 font-medium">{peers.length} buildings</span>
                {legal > 0 && <span className="text-emerald-600">{legal} legal transient</span>}
                {noOp > 0 && <span className="text-gray-500">{noOp} no known operator</span>}
                {withLien > 0 && <span className="text-red-500">{withLien} with liens</span>}
                {landmark > 0 && <span className="text-amber-500">{landmark} landmark/historic</span>}
              </div>
            </div>
          );
        })()}

        {/* Property condition */}
        {(() => {
          const hasLien = p.has_tax_lien;
          const hasLp = p.has_lis_pendens;
          const hpdV = p.hpd_open_violations || 0;
          const hpdC = p.hpd_class_c_violations || 0;
          const ecbV = p.ecb_open_violations || 0;
          const ecbBal = p.ecb_total_balance || 0;
          if (!hasLien && !hasLp && hpdV === 0 && ecbV === 0) return null;

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

          return (
            <div>
              <div className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Property Condition</div>
              <div className="space-y-1">
                {signals.map((sig, i) => (
                  <DistressRow key={i} signal={sig} />
                ))}
              </div>
            </div>
          );
        })()}

        {/* CRM Status + Notes */}
        <div className="flex items-center gap-2">
          <select
            value={crmStatuses[p.bbl] || ""}
            onChange={(e) => setCrmStatus(p.bbl, e.target.value)}
            className={`px-2 py-1 text-[11px] rounded-md border border-gray-200 outline-none cursor-pointer ${
              (CRM_STATUSES.find(s => s.value === (crmStatuses[p.bbl] || "")) || {}).color || "bg-gray-50 text-gray-400"
            }`}
          >
            {CRM_STATUSES.map(s => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
          <div className="flex-1">
            <NoteEditor bbl={p.bbl} notes={notes} onSave={onSaveNote} />
          </div>
        </div>

      </div>
    </div>
  );
}

function ListTray({ list, onRemove, onClear, onExpand, expanded }) {
  const items = Array.from(list.values());
  if (items.length === 0) return null;

  if (!expanded) {
    return (
      <div className="absolute bottom-6 right-4 z-20">
        <button
          onClick={onExpand}
          className="flex items-center gap-2.5 bg-gray-800 text-white pl-4 pr-3 py-2.5 rounded-lg shadow-xl cursor-pointer hover:bg-gray-700 transition-colors"
        >
          <span className="text-sm font-medium">Export list</span>
          <span className="bg-white/20 text-white text-xs font-bold px-2 py-0.5 rounded-full">{items.length}</span>
        </button>
      </div>
    );
  }

  return (
    <div className="absolute bottom-6 right-4 w-96 max-h-[60vh] bg-white rounded-xl shadow-2xl border border-gray-200 z-20 flex flex-col">
      <div className="flex items-center justify-between p-3 border-b border-gray-100 shrink-0">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-gray-900">Export list</h3>
          <span className="bg-gray-100 text-gray-600 text-xs font-bold px-2 py-0.5 rounded-full">{items.length}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => exportToCsv(items)}
            className="px-3 py-1.5 bg-gray-800 text-white text-xs rounded-md hover:bg-gray-700 cursor-pointer transition-colors font-medium"
          >
            Download CSV
          </button>
          <button
            onClick={onClear}
            className="px-2 py-1.5 text-xs text-gray-400 hover:text-red-500 cursor-pointer transition-colors"
          >
            Clear
          </button>
          <button onClick={onExpand} className="text-gray-400 hover:text-gray-600 text-lg leading-none cursor-pointer ml-1">&times;</button>
        </div>
      </div>
      <div className="overflow-y-auto divide-y divide-gray-50 flex-1">
        {items.map((f) => {
          const p = f.properties;
          const priorOp = parseJsonProp(p.prior_operator);
          return (
            <div key={p.bbl} className="flex items-center gap-3 px-4 py-2 hover:bg-gray-50 group">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: segmentColor(p.segment) }} />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-gray-900 truncate">{p.hotel_name || p.address}</div>
                <div className="text-[10px] text-gray-400">
                  {p.hotel_name ? `${p.address} · ` : ""}{estRooms(p).value} rooms &middot; {p.bldgclass || "—"}
                  {priorOp ? ` · ${priorOp.name}` : ""}
                </div>
              </div>
              <button
                onClick={() => onRemove(p.bbl)}
                className="text-gray-300 group-hover:text-red-400 text-sm cursor-pointer"
              >
                &times;
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-[10px] text-gray-400 uppercase tracking-wide">{label}</div>
      <div className="text-sm font-medium text-gray-900">{value}</div>
    </div>
  );
}

function InfoTip({ text }) {
  const [show, setShow] = useState(false);
  const tipRef = useRef(null);

  return (
    <span className="relative inline-flex ml-1">
      <button
        type="button"
        className="w-3.5 h-3.5 rounded-full bg-gray-200 hover:bg-gray-300 text-[9px] font-bold text-gray-500 hover:text-gray-700 inline-flex items-center justify-center cursor-pointer transition-colors leading-none"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        onClick={(e) => { e.stopPropagation(); setShow(!show); }}
      >
        i
      </button>
      {show && (
        <div
          ref={tipRef}
          className="absolute left-5 top-1/2 -translate-y-1/2 w-56 bg-gray-900 text-white text-[11px] leading-relaxed rounded-lg px-3 py-2 shadow-xl z-50 pointer-events-none"
        >
          {text}
          <div className="absolute left-0 top-1/2 -translate-x-1 -translate-y-1/2 w-2 h-2 bg-gray-900 rotate-45" />
        </div>
      )}
    </span>
  );
}

const BLDG_CLASS_LABELS = {
  H1: "Luxury hotel", H2: "Hotel, 100+ rooms", H3: "Hotel, 20-99 rooms", H4: "Motel",
  H5: "Private club/hotel", H6: "Apartment hotel", H7: "Apartment hotel, condo",
  H8: "Dormitory", H9: "Miscellaneous hotel", HB: "Boutique hotel",
  HH: "Hostel", HR: "SRO", HS: "Hotel, 10-19 rooms", RH: "Condo hotel",
  RM: "Residential, multi-story walk-up", RR: "Condo, walk-up",
  RC: "Residential/commercial mixed", RD: "Residential, elevator",
  RK: "Condo, two-three story", RI: "Condo, elevator",
  RW: "Condo, residential/commercial", RS: "Single room occupancy (SRO)",
  RX: "Condo, multi-story", R1: "Condo, detached",
  R2: "Condo, semi-detached", R3: "Condo, walk-up",
  R4: "Condo, elevator", R5: "Miscellaneous condo",
  R6: "Condo, loft", R7: "Condo, two-family",
  R8: "Condo, three-family", R9: "Condo, co-op conversion",
  D1: "Elevator co-op, 8-14 stories", D2: "Elevator co-op, fireproof",
  D3: "Elevator co-op, 8-14 stories alt.", D4: "Elevator co-op, luxury",
  D5: "Elevator co-op, converted", D6: "Elevator co-op, loft",
  D7: "Elevator co-op, semi-fireproof", D8: "Elevator co-op, misc.",
  D9: "Elevator co-op, misc. alt.",
  C1: "Walk-up, 3+ units, old law", C2: "Walk-up, 3+ units, new law",
  C3: "Walk-up, 3+ units, fireproof", C4: "Walk-up, condo conversion",
  C5: "Walk-up, converted dwelling", C6: "Walk-up, co-op",
  C7: "Walk-up, over 6 stories", C8: "Walk-up, over 6 units",
  C9: "Walk-up, garden complex",
  S1: "Primarily residential, mixed use", S2: "Primarily commercial, mixed use",
  S3: "Mixed use, 3-6 stories", S4: "Mixed use, factory conversion",
  S5: "Mixed use, semi-fireproof", S9: "Mixed use, misc.",
  O1: "Office, loft", O2: "Office, 10-25 stories", O3: "Office, 25-50 stories",
  O4: "Office, tower", O5: "Office, converted", O6: "Office, 6-10 stories",
  O7: "Office, professional building", O8: "Office, misc.", O9: "Office, misc. alt.",
};

function StatBldgClass({ code }) {
  const c = (code || "").trim().toUpperCase();
  const label = BLDG_CLASS_LABELS[c];
  return (
    <div>
      <div className="text-[10px] text-gray-400 uppercase tracking-wide">Building class</div>
      <div className="text-sm font-medium text-gray-900 flex items-center gap-1">
        {c || "—"}
        {label && (
          <span className="relative group cursor-help">
            <span className="inline-flex items-center justify-center w-3 h-3 rounded-full bg-gray-200 text-gray-500 text-[8px] font-bold">i</span>
            <span className="absolute bottom-full left-0 mb-1 w-48 bg-gray-900 text-white text-[10px] leading-snug rounded-lg px-3 py-2 opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto transition-opacity z-50 shadow-lg">
              {label}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

function FilterPanel({
  activeSegments, setActiveSegments,
  showPriorOps, setShowPriorOps,
  showReversion, setShowReversion,
  distressOnly, setDistressOnly,
  noOperatorOnly, setNoOperatorOnly,
  hideBrandTypes, toggleBrandType,
  hideCondos, setHideCondos,
  hideRestricted, setHideRestricted,
  minUnits, setMinUnits,
  minClassB, setMinClassB,
  featureCount, overlayCounts,
  onAddAllVisible, onAddCategory,
  dataDate,
}) {
  const toggleSegment = (key) => setActiveSegments((prev) => ({ ...prev, [key]: !prev[key] }));
  return (
    <div className="absolute top-4 left-4 w-72 bg-white/95 backdrop-blur rounded-xl shadow-xl border border-gray-200 z-20">
      <div className="p-4 border-b border-gray-100">
        <h1 className="text-sm font-bold text-gray-900 tracking-tight">NYC Transient Capacity</h1>
        <p className="text-[11px] text-gray-500 mt-0.5">Manhattan &middot; Downtown BK &middot; Williamsburg &middot; LIC</p>
        {dataDate && (() => {
          const days = dataDate.daysAgo;
          const color = days <= 7 ? "text-emerald-600" : days <= 30 ? "text-amber-600" : "text-red-600";
          const bg = days <= 7 ? "bg-emerald-50" : days <= 30 ? "bg-amber-50" : "bg-red-50";
          const label = days === 0 ? "Today" : days === 1 ? "1 day ago" : `${days} days ago`;
          return (
            <div className={`${bg} rounded px-1.5 py-0.5 mt-1 inline-flex items-center gap-1.5`}>
              <span className={`w-1.5 h-1.5 rounded-full ${days <= 7 ? "bg-emerald-500" : days <= 30 ? "bg-amber-500" : "bg-red-500"}`} />
              <span className={`text-[10px] ${color} font-medium`}>Data: {dataDate.formatted} ({label})</span>
            </div>
          );
        })()}
      </div>

      <div className="p-4 space-y-4">
        <div>
          <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-2">Building segments</div>
          <div className="space-y-1">
            {SEGMENTS.map((seg) => {
              const active = activeSegments[seg.key];
              return (
                <button
                  key={seg.key}
                  onClick={() => toggleSegment(seg.key)}
                  className="w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg transition-colors cursor-pointer text-left"
                  style={{
                    backgroundColor: active ? `${seg.color}10` : "transparent",
                    borderLeft: `3px solid ${active ? seg.color : "transparent"}`,
                  }}
                >
                  <span
                    className="w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors shrink-0"
                    style={{
                      borderColor: seg.color,
                      backgroundColor: active ? seg.color : "transparent",
                    }}
                  >
                    {active && (
                      <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </span>
                  <span className={`text-xs ${active ? "text-gray-900 font-medium" : "text-gray-400"}`}>
                    {seg.label}
                  </span>
                  {overlayCounts.segmentCounts[seg.key] > 0 && (
                    <span className={`text-[10px] ml-auto ${active ? "text-gray-500" : "text-gray-300"}`}>
                      {overlayCounts.segmentCounts[seg.key].toLocaleString()}
                    </span>
                  )}
                  <InfoTip text={seg.info} />
                </button>
              );
            })}
          </div>
        </div>

        {/* Overlays */}
        <div>
          <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-2">Overlays</div>
          <div className="space-y-1.5">
            <label className="flex items-center gap-2.5 cursor-pointer px-2.5">
              <input
                type="checkbox"
                checked={showPriorOps}
                onChange={(e) => setShowPriorOps(e.target.checked)}
                className="sr-only"
              />
              <span
                className="w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors"
                style={{
                  borderColor: "#a855f7",
                  backgroundColor: showPriorOps ? "#a855f7" : "transparent",
                }}
              >
                {showPriorOps && (
                  <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                )}
              </span>
              <div className="flex items-center">
                <span className="text-xs text-gray-700">Prior operators</span>
                <span className="text-[10px] text-gray-400 ml-1">({overlayCounts.priorOps})</span>
                <InfoTip text="Highlights buildings previously operated by flex-stay companies (Sonder, Placemakr, Kasa, Mint House, etc.). Adds any not already visible through active segments. Purple outline on map." />
              </div>
            </label>
            <label className="flex items-center gap-2.5 cursor-pointer px-2.5">
              <input
                type="checkbox"
                checked={showReversion}
                onChange={(e) => setShowReversion(e.target.checked)}
                className="sr-only"
              />
              <span
                className="w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors"
                style={{
                  borderColor: "#dc2626",
                  backgroundColor: showReversion ? "#dc2626" : "transparent",
                }}
              >
                {showReversion && (
                  <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                )}
              </span>
              <div className="flex items-center">
                <span className="text-xs text-gray-700">Reversion window</span>
                <span className="text-[10px] text-gray-400 ml-1">({overlayCounts.reversions})</span>
                <InfoTip text="Hotels that converted to residential post-2021. Can revert to hotel use without CPC special permit before Dec 2027. Red outline on map." />
              </div>
            </label>
          </div>
        </div>

        {/* Refinements */}
        <div>
          <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-2">Refinements</div>
          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={distressOnly}
              onChange={(e) => setDistressOnly(e.target.checked)}
              className="sr-only"
            />
            <span
              className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                distressOnly ? "bg-gray-800 border-gray-800" : "border-gray-300"
              }`}
            >
              {distressOnly && (
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <span className="text-xs text-gray-700">Distress signals only</span>
            <InfoTip text="Show only buildings with financial distress indicators: tax liens, lis pendens/judgments, high ECB fines, or significant HPD violations. Combined with legal transient status, these are the strongest signals of a motivated owner." />
          </label>

          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={noOperatorOnly}
              onChange={(e) => setNoOperatorOnly(e.target.checked)}
              className="sr-only"
            />
            <span
              className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                noOperatorOnly ? "bg-gray-800 border-gray-800" : "border-gray-300"
              }`}
            >
              {noOperatorOnly && (
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <span className="text-xs text-gray-700">No known operator</span>
            <InfoTip text="Show only buildings where we couldn't find an active hotel operation via Google Places. These have legal transient capacity but no identifiable operator — a potential management opportunity. Based on Google Places coverage, not a verified fact." />
          </label>

          <div className="px-2.5 mt-1.5 space-y-1.5">
            <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Hide by brand type</div>
            {[
              { bt: "chain", label: "Chains", tip: "Major flag systems (Marriott, Hilton, Hyatt, IHG, etc.). Not management targets." },
              { bt: "independent", label: "Branded independents", tip: "Recognizable independent brands (Arlo, Dream, Gansevoort). May be open to management changes." },
              { bt: "club", label: "Private clubs", tip: "Members-only clubs (Soho House, NY Athletic Club). Not hotel targets." },
            ].map(({ bt, label, tip }) => (
              <label key={bt} className="flex items-center gap-2.5 cursor-pointer">
                <input type="checkbox" checked={hideBrandTypes.has(bt)} onChange={() => toggleBrandType(bt)} className="sr-only" />
                <span className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                  hideBrandTypes.has(bt) ? "bg-gray-800 border-gray-800" : "border-gray-300"
                }`}>
                  {hideBrandTypes.has(bt) && (
                    <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                  )}
                </span>
                <span className="text-xs text-gray-700">{label}</span>
                <InfoTip text={tip} />
              </label>
            ))}
          </div>

          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={hideCondos}
              onChange={(e) => setHideCondos(e.target.checked)}
              className="sr-only"
            />
            <span
              className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                hideCondos ? "bg-gray-800 border-gray-800" : "border-gray-300"
              }`}
            >
              {hideCondos && (
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <span className="text-xs text-gray-700">Hide condos</span>
            <InfoTip text="Exclude condominium buildings. Condos require board approval or commercial condo owner negotiation — a different deal structure than single-owner rentals." />
          </label>

          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={hideRestricted}
              onChange={(e) => setHideRestricted(e.target.checked)}
              className="sr-only"
            />
            <span
              className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                hideRestricted ? "bg-gray-800 border-gray-800" : "border-gray-300"
              }`}
            >
              {hideRestricted && (
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <span className="text-xs text-gray-700">Hide SRO / dorm / hostel</span>
            <InfoTip text="Exclude SRO (HR, RS), dormitory (H8) and hostel (HH) building classes. These register Class B rooms and so score well, but SRO stock is rent-regulated and dorms and hostels are a different operating model. On by default." />
          </label>

          <div className="flex items-center justify-between px-2.5 mt-2">
            <span className="text-xs text-gray-700">Min units</span>
            <input
              type="number"
              min={0}
              value={minUnits}
              onChange={(e) => setMinUnits(Math.max(0, Number(e.target.value) || 0))}
              className="w-16 px-2 py-1 text-xs font-mono text-gray-700 bg-gray-50 border border-gray-200 rounded-md text-right outline-none focus:ring-2 focus:ring-gray-300"
            />
          </div>
          <div className="flex items-center justify-between px-2.5 mt-1.5">
            <span className="text-xs text-gray-700">Min Class B</span>
            <input
              type="number"
              min={0}
              value={minClassB}
              onChange={(e) => setMinClassB(Math.max(0, Number(e.target.value) || 0))}
              className="w-16 px-2 py-1 text-xs font-mono text-gray-700 bg-gray-50 border border-gray-200 rounded-md text-right outline-none focus:ring-2 focus:ring-gray-300"
            />
          </div>
        </div>

        {/* Score is now purely legal — no weight config needed */}

        {/* Add to list actions */}
        <div className="space-y-2 pt-2 border-t border-gray-100">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Add to export list</div>
          <button
            onClick={onAddAllVisible}
            className="w-full px-3 py-2 text-xs font-medium rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 cursor-pointer transition-colors text-left"
          >
            + All visible buildings
            <span className="text-gray-400 ml-1">({featureCount})</span>
          </button>
          <div className="flex gap-2">
            <button
              onClick={() => onAddCategory("has_prior_op")}
              className="flex-1 px-2 py-1.5 text-[11px] rounded-lg border border-purple-200 text-purple-700 hover:bg-purple-50 cursor-pointer transition-colors"
            >
              + Prior operators
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SearchBar({ mapRef, panelOpen, onSelectFeature }) {
  const HISTORY_KEY = "transient-search-history";
  const [query, setQuery] = useState("");
  const [geoResults, setGeoResults] = useState([]);
  const [placesResults, setPlacesResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(false);
  const [showAllHistory, setShowAllHistory] = useState(false);
  const debounceRef = useRef(null);

  const getHistory = () => {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; }
  };
  const addHistory = (label, coords) => {
    const hist = getHistory().filter(h => h.label !== label);
    hist.unshift({ label, coords });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(hist.slice(0, 20)));
  };

  const findNearbyBuilding = (map, center) => {
    // After fly animation, query rendered building features and select the nearest one
    const point = map.project(center);
    // Search in a 50px radius around the target point
    const bbox = [
      [point.x - 50, point.y - 50],
      [point.x + 50, point.y + 50],
    ];
    const features = map.queryRenderedFeatures(bbox, { layers: ["buildings-fill"] });
    if (features.length > 0 && onSelectFeature) {
      // Find the closest feature by distance to center
      let closest = features[0];
      let minDist = Infinity;
      for (const f of features) {
        const c = featureCentroid(f);
        if (!c) continue;
        const dist = Math.hypot(c[0] - center[0], c[1] - center[1]);
        if (dist < minDist) { minDist = dist; closest = f; }
      }
      onSelectFeature(closest);
    }
  };

  const search = useCallback(async (text) => {
    if (text.length < 3) {
      setGeoResults([]);
      setPlacesResults([]);
      return;
    }
    setLoading(true);
    try {
      const [geoSettled, placesSettled] = await Promise.allSettled([
        // GeoSearch (existing)
        fetch(`https://geosearch.planninglabs.nyc/v2/search?text=${encodeURIComponent(text)}`)
          .then(r => r.json()),
        // Google Places Text Search (New) — skipped when no key is configured
        !GOOGLE_MAPS_API_KEY ? Promise.resolve({}) : fetch("https://places.googleapis.com/v1/places:searchText", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
          },
          body: JSON.stringify({
            textQuery: text,
            locationBias: {
              circle: {
                center: { latitude: 40.7580, longitude: -73.9855 },
                radius: 15000.0,
              },
            },
          }),
        }).then(r => r.json()),
      ]);

      if (geoSettled.status === "fulfilled") {
        setGeoResults((geoSettled.value.features || []).slice(0, 5));
      } else {
        setGeoResults([]);
      }

      if (placesSettled.status === "fulfilled" && placesSettled.value.places) {
        setPlacesResults(placesSettled.value.places.slice(0, 5));
      } else {
        setPlacesResults([]);
      }
    } catch {
      setGeoResults([]);
      setPlacesResults([]);
    }
    setLoading(false);
  }, []);

  const handleInput = (e) => {
    const val = e.target.value;
    setQuery(val);
    setShowAllHistory(false);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(val), 300);
  };

  const flyTo = (feature) => {
    const map = mapRef.current;
    const [lng, lat] = feature.geometry.coordinates;
    map?.flyTo({ center: [lng, lat], zoom: 17, duration: 1200 });
    setQuery(feature.properties.label);
    setGeoResults([]);
    setPlacesResults([]);
    setFocused(false);
    addHistory(feature.properties.label, [lng, lat]);
    // After fly completes, try to find and select a nearby building
    if (map) {
      map.once("moveend", () => findNearbyBuilding(map, [lng, lat]));
    }
  };

  const flyToPlace = (place) => {
    const map = mapRef.current;
    const lat = place.location?.latitude;
    const lng = place.location?.longitude;
    if (!map || lat == null || lng == null) return;
    const label = place.displayName?.text || place.formattedAddress || "Place";
    map.flyTo({ center: [lng, lat], zoom: 17, duration: 1200 });
    setQuery(label);
    setGeoResults([]);
    setPlacesResults([]);
    setFocused(false);
    addHistory(label, [lng, lat]);
    // After fly completes, try to find and select a nearby building
    map.once("moveend", () => findNearbyBuilding(map, [lng, lat]));
  };

  const flyToHistory = (item) => {
    const map = mapRef.current;
    map?.flyTo({ center: item.coords, zoom: 17, duration: 1200 });
    setQuery(item.label);
    setFocused(false);
  };

  const hasResults = geoResults.length > 0 || placesResults.length > 0;
  const history = getHistory();
  const showHistory = focused && query.length < 3 && !hasResults && history.length > 0;
  const visibleHistory = showAllHistory ? history : history.slice(0, 5);

  return (
    <div className="absolute top-4 left-[calc(50%-10rem)] w-80 z-20">
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={handleInput}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 200)}
          placeholder="Search address or place name..."
          className="w-full px-4 py-2.5 bg-white/95 backdrop-blur rounded-lg shadow-lg border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:ring-2 focus:ring-gray-300"
        />
        {loading && (
          <div className="absolute right-3 top-3 w-4 h-4 border-2 border-gray-300 border-t-gray-600 rounded-full animate-spin" />
        )}
      </div>
      {hasResults && (
        <div className="mt-1 bg-white rounded-lg shadow-xl border border-gray-200 overflow-hidden max-h-80 overflow-y-auto">
          {geoResults.length > 0 && (
            <>
              {placesResults.length > 0 && (
                <div className="px-4 py-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50">Addresses</div>
              )}
              {geoResults.map((r, i) => (
                <button
                  key={`geo-${i}`}
                  onClick={() => flyTo(r)}
                  className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 border-b border-gray-50 last:border-0 cursor-pointer"
                >
                  {r.properties.label}
                </button>
              ))}
            </>
          )}
          {placesResults.length > 0 && (
            <>
              <div className="px-4 py-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wider bg-gray-50">Places</div>
              {placesResults.map((p, i) => (
                <button
                  key={`place-${i}`}
                  onClick={() => flyToPlace(p)}
                  className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 border-b border-gray-50 last:border-0 cursor-pointer"
                >
                  <div className="font-medium">{p.displayName?.text}</div>
                  <div className="text-xs text-gray-400 truncate">{p.formattedAddress}</div>
                </button>
              ))}
            </>
          )}
        </div>
      )}
      {showHistory && (
        <div className="mt-1 bg-white rounded-lg shadow-xl border border-gray-200 overflow-hidden">
          <div className="px-4 py-1.5 text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Recent</div>
          {visibleHistory.map((h, i) => (
            <button
              key={i}
              onClick={() => flyToHistory(h)}
              className="w-full text-left px-4 py-2 text-sm text-gray-600 hover:bg-gray-50 border-b border-gray-50 last:border-0 cursor-pointer flex items-center gap-2"
            >
              <span className="text-gray-300 text-xs">↩</span>
              <span className="truncate">{h.label}</span>
            </button>
          ))}
          {history.length > 5 && !showAllHistory && (
            <button
              onClick={() => setShowAllHistory(true)}
              className="w-full text-center py-2 text-[11px] text-blue-500 hover:bg-gray-50 cursor-pointer"
            >
              Show more ({history.length - 5} more)
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="absolute bottom-6 left-[19.5rem] bg-white/90 backdrop-blur rounded-lg shadow-lg border border-gray-200 px-3 py-2 z-10">
      <div className="flex gap-4">
        {ALL_TIERS.map((tier) => (
          <div key={tier.key} className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: tier.color }} />
            <span className="text-[10px] text-gray-600">{tier.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Table columns ---
const TABLE_COLS = [
  { key: "address", label: "Address", sortable: true, width: "min-w-[200px]" },
  { key: "score", label: "Score", sortable: true, numeric: true, width: "min-w-[70px]" },
  { key: "feasibility", label: "Legal", sortable: true, numeric: true, width: "min-w-[110px]", tooltip: "H = Hotel building class · B = HPD Class B rooms · R = DOB R-1 transient occupancy" },
  { key: "neighborhood", label: "Neighborhood", sortable: true, width: "min-w-[160px]" },
  { key: "tier", label: "Tier", sortable: true, width: "min-w-[120px]" },
  { key: "est_rooms", label: "Est. Rooms", sortable: true, numeric: true, width: "min-w-[85px]" },
  { key: "numfloors", label: "Floors", sortable: true, numeric: true, width: "min-w-[70px]" },
  { key: "bldgclass", label: "Class", sortable: true, width: "min-w-[65px]" },
  { key: "hpd_class_b", label: "Class B", sortable: true, numeric: true, width: "min-w-[75px]" },
  { key: "ownername", label: "Owner", sortable: true, width: "min-w-[180px]" },
  { key: "owner_portfolio_size", label: "Portfolio", sortable: true, numeric: true, width: "min-w-[80px]" },
  { key: "last_sale_price", label: "Last Sale", sortable: true, numeric: true, width: "min-w-[110px]" },
  { key: "last_sale_date", label: "Sale Date", sortable: true, width: "min-w-[95px]" },
  { key: "operator_name", label: "Operator", sortable: true, width: "min-w-[160px]" },
  { key: "zonedist1", label: "Zoning", sortable: true, width: "min-w-[85px]" },
];

function applyFilters(features, activeSegments, showPriorOps, showReversion, minUnits, minClassB, filters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted) {
  return features.filter((f) => {
    const p = f.properties;
    const segOk = activeSegments[p.segment];
    const overlayOk = (showPriorOps && p.has_prior_op) || (showReversion && p.has_reversion);
    if (!segOk && !overlayOk) return false;

    if (!overlayOk && estRooms(p).value < minUnits) return false;
    if (!overlayOk && minClassB > 0 && (p.hpd_class_b || 0) < minClassB && p.segment !== "partial" && p.segment !== "active_hotel") return false;

    if (!overlayOk) {
      if (filters.filterTempCoo && !p.coo_has_temporary) return false;
      if (filters.filterHasClassB && !(p.hpd_class_b > 0)) return false;
      if (filters.filterMultiOwner && !(p.owner_portfolio_size > 1)) return false;
      if (filters.filterRecentSale && (!p.last_sale_date || p.last_sale_date < filters._recentSaleCutoff)) return false;
      if (filters.filterCommercialZone) {
        const z = (p.zonedist1 || "")[0];
        if (z !== "C" && z !== "M") return false;
      }
      if (distressOnly) {
        const hasDistress = p.has_tax_lien || p.has_lis_pendens || (p.hpd_open_violations || 0) > 0 || (p.ecb_open_violations || 0) > 0;
        if (!hasDistress) return false;
      }
      if (noOperatorOnly && p.hotel_name) return false;
      if (hideBrandTypes.size > 0 && p.brand_type && hideBrandTypes.has(p.brand_type)) return false;
      if (hideCondos && p.is_condo) return false;
      if (hideRestricted && p.restricted_class) return false;
    }

    return true;
  });
}

function dedupeFeatures(features) {
  // Group by address + owner to collapse condo lots / multi-BBL complexes
  const groups = new Map();
  for (const f of features) {
    const p = f.properties;
    const key = `${(p.address || "").trim()}|${(p.ownername || "").trim()}`;
    if (!groups.has(key)) {
      groups.set(key, f);
    } else {
      // Keep the one with the highest-priority tier
      const existing = groups.get(key).properties;
      const SEG_RANK = { transient: 0, active_hotel: 1, partial: 2, unknown: 3 };
      if ((SEG_RANK[p.segment] ?? 99) < (SEG_RANK[existing.segment] ?? 99)) {
        groups.set(key, f);
      }
    }
  }
  return Array.from(groups.values());
}

function TableView({ features, onSelectFeature, exportList, onAddToList, extraFilters, setFilter,
  activeSegments, setActiveSegments, distressOnly, setDistressOnly, minUnits, setMinUnits, minClassB, setMinClassB,
  showPriorOps, setShowPriorOps, showReversion, setShowReversion, notes, matrixFilter, onClearMatrixFilter,
}) {
  const [sortKey, setSortKey] = useState("score");
  const [sortDir, setSortDir] = useState("desc");
  const [searchText, setSearchText] = useState("");

  // Deduplicate multi-BBL buildings (condo lots, complexes)
  const dedupedFeatures = useMemo(() => dedupeFeatures(features), [features]);

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const matrixFiltered = matrixFilter
    ? dedupedFeatures.filter((f) => matrixFilter.fn(f.properties))
    : dedupedFeatures;

  const filtered = searchText.length >= 2
    ? matrixFiltered.filter((f) => {
        const p = f.properties;
        const text = searchText.toLowerCase();
        return (p.address || "").toLowerCase().includes(text)
          || (p.ownername || "").toLowerCase().includes(text)
          || (p.bbl || "").includes(text);
      })
    : matrixFiltered;

  const sorted = [...filtered].sort((a, b) => {
    const col = TABLE_COLS.find((c) => c.key === sortKey);
    const feasScore = (p) => [
      (p.bldgclass || "").startsWith("H"),
      p.hpd_class_b > 0,
      !!p.dob_has_r1,
    ].filter(Boolean).length;
    let va = sortKey === "score" ? computeScore(a.properties) : sortKey === "feasibility" ? feasScore(a.properties) : sortKey === "est_rooms" ? estRooms(a.properties).value : a.properties[sortKey];
    let vb = sortKey === "score" ? computeScore(b.properties) : sortKey === "feasibility" ? feasScore(b.properties) : sortKey === "est_rooms" ? estRooms(b.properties).value : b.properties[sortKey];
    if (col?.numeric) {
      va = Number(va) || 0;
      vb = Number(vb) || 0;
    } else {
      va = String(va || "").toLowerCase();
      vb = String(vb || "").toLowerCase();
    }
    if (va < vb) return sortDir === "asc" ? -1 : 1;
    if (va > vb) return sortDir === "asc" ? 1 : -1;
    return 0;
  });

  const fmtPrice = (v) => {
    if (!v || v === 0) return "—";
    return `$${Number(v).toLocaleString()}`;
  };

  const cellValue = (col, p) => {
    if (col.key === "address") {
      const hasNote = notes && notes[p.bbl];
      return (
        <span className="flex items-center gap-1.5">
          <span className="truncate">{p.hotel_name || p.address || "—"}</span>
          {hasNote && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" title="Has note" />}
        </span>
      );
    }
    if (col.key === "score") {
      const s = computeScore(p);
      const bg = s >= 60 ? "bg-emerald-600" : s >= 35 ? "bg-amber-500" : "bg-gray-400";
      return <span className={`${bg} text-white text-[10px] font-bold px-1.5 py-0.5 rounded tabular-nums`}>{s}</span>;
    }
    if (col.key === "est_rooms") {
      const rooms = estRooms(p);
      if (rooms.value === 0) return "—";
      return <span title={rooms.source}>{rooms.value}</span>;
    }
    if (col.key === "feasibility") {
      const checks = [
        { key: "H", label: "Hotel class", on: (p.bldgclass || "").startsWith("H") },
        { key: "B", label: "HPD Class B rooms", on: p.hpd_class_b > 0 },
        { key: "R", label: "DOB R-1 transient", on: !!p.dob_has_r1 },
      ];
      return (
        <span className="flex gap-1">
          {checks.map((c) => (
            <span
              key={c.key}
              title={`${c.label}: ${c.on ? "Yes" : "No"}`}
              className={`w-5 h-5 rounded text-[9px] font-bold flex items-center justify-center ${
                c.on ? "bg-emerald-100 text-emerald-700" : "bg-gray-100 text-gray-300"
              }`}
            >
              {c.key}
            </span>
          ))}
        </span>
      );
    }
    const v = p[col.key];
    if (col.key === "tier") {
      return (
        <span
          className="inline-block px-2 py-0.5 rounded-full text-[10px] font-semibold text-white"
          style={{ backgroundColor: segmentColor(p.segment) }}
        >
          {(p.segment || p.tier || "").replace(/_/g, " ")}
        </span>
      );
    }
    if (col.key === "last_sale_price") return fmtPrice(v);
    if (col.key === "unitsres") {
      const res = p.unitsres || 0;
      const total = p.unitstotal || 0;
      if (res > 0) return res;
      if (total > 0) return <span className="text-gray-400" title="Total units (no residential)">{total}</span>;
      return "—";
    }
    if (col.key === "numfloors") return v ? Math.round(v) : "—";
    if (col.key === "owner_portfolio_size") return v > 1 ? v : "";
    if (col.key === "operator_name") {
      return <span className="truncate block max-w-[160px]" title={v}>{v || "—"}</span>;
    }
    if (col.key === "ownername") {
      return (
        <span className="truncate block max-w-[180px]" title={v}>{v || "—"}</span>
      );
    }
    if (v == null || v === "") return "—";
    return String(v);
  };

  return (
    <div className="h-full flex flex-col bg-gray-50">
      {/* Header + search */}
      <div className="px-8 pt-8 pb-4 shrink-0 space-y-3">
        <div>
          <h2 className="text-lg font-bold text-gray-900">Building pipeline</h2>
          <p className="text-sm text-gray-500 mt-1">
            All buildings ranked by deal score. Click a row to view details.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="Search address, owner, or BBL..."
            className="flex-1 px-3 py-2 bg-white rounded-lg border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:ring-2 focus:ring-gray-300"
          />
          <span className="text-xs text-gray-400 shrink-0">{sorted.length} buildings</span>
        </div>
        {matrixFilter && (
          <div className="flex items-center gap-2 px-3 py-2 bg-indigo-50 border border-indigo-200 rounded-lg">
            <span className="text-xs font-medium text-indigo-700">Filtered: {matrixFilter.label}</span>
            <span className="text-xs text-indigo-500">({matrixFiltered.length})</span>
            <button onClick={onClearMatrixFilter} className="ml-auto text-indigo-400 hover:text-indigo-600 cursor-pointer text-xs font-medium">
              Clear
            </button>
          </div>
        )}
        <div className="flex flex-wrap gap-1.5">
          {[
            { key: "filterHasClassB", label: "Class B rooms" },
            { key: "filterTempCoo", label: "Temp C of O" },
            { key: "filterMultiOwner", label: "Multi-building owner" },
            { key: "filterRecentSale", label: "Sold last 3yr" },
            { key: "filterCommercialZone", label: "C/M zoning" },
          ].map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setFilter(key, !extraFilters[key])}
              className={`px-2.5 py-1 text-[11px] rounded-md border transition-colors cursor-pointer ${
                extraFilters[key]
                  ? "bg-gray-800 text-white border-gray-800"
                  : "bg-white text-gray-600 border-gray-200 hover:border-gray-400"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Core filters */}
        <div className="flex items-center gap-4 flex-wrap">
          <div className="flex items-center gap-1.5">
            {SEGMENTS.map((seg) => (
              <button
                key={seg.key}
                onClick={() => setActiveSegments((prev) => ({ ...prev, [seg.key]: !prev[seg.key] }))}
                className={`px-2 py-0.5 text-[11px] rounded-md border transition-colors cursor-pointer ${
                  activeSegments[seg.key]
                    ? "text-white border-transparent"
                    : "bg-white text-gray-400 border-gray-200"
                }`}
                style={activeSegments[seg.key] ? { backgroundColor: seg.color, borderColor: seg.color } : {}}
              >
                {seg.label}
              </button>
            ))}
          </div>
          <button
            onClick={() => setShowPriorOps(!showPriorOps)}
            className={`px-2 py-0.5 text-[11px] rounded-md border transition-colors cursor-pointer ${
              showPriorOps ? "text-white border-transparent" : "bg-white text-gray-400 border-gray-200"
            }`}
            style={showPriorOps ? { backgroundColor: "#a855f7", borderColor: "#a855f7" } : {}}
          >
            Prior operators
          </button>
          <button
            onClick={() => setShowReversion(!showReversion)}
            className={`px-2 py-0.5 text-[11px] rounded-md border transition-colors cursor-pointer ${
              showReversion ? "text-white border-transparent" : "bg-white text-gray-400 border-gray-200"
            }`}
            style={showReversion ? { backgroundColor: "#dc2626", borderColor: "#dc2626" } : {}}
          >
            Reversion window
          </button>
          <button
            onClick={() => setDistressOnly(!distressOnly)}
            className={`px-2 py-0.5 text-[11px] rounded-md border transition-colors cursor-pointer ${
              distressOnly ? "bg-red-600 text-white border-red-600" : "bg-white text-gray-600 border-gray-200"
            }`}
          >
            Distress only
          </button>
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-gray-500">Min units</span>
            <input
              type="number"
              min={0}
              value={minUnits}
              onChange={(e) => setMinUnits(Math.max(0, Number(e.target.value) || 0))}
              className="w-14 px-1.5 py-0.5 text-[11px] font-mono text-gray-700 bg-white border border-gray-200 rounded-md text-right outline-none focus:ring-1 focus:ring-gray-300"
            />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-gray-500">Min Class B</span>
            <input
              type="number"
              min={0}
              value={minClassB}
              onChange={(e) => setMinClassB(Math.max(0, Number(e.target.value) || 0))}
              className="w-14 px-1.5 py-0.5 text-[11px] font-mono text-gray-700 bg-white border border-gray-200 rounded-md text-right outline-none focus:ring-1 focus:ring-gray-300"
            />
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto px-8 pb-8">
        <div className="bg-white rounded-xl border border-gray-200 overflow-x-auto">
        <table className="min-w-max text-left">
          <thead className="sticky top-0 bg-gray-50 z-10">
            <tr>
              <th className="px-3 py-2.5 text-[10px] font-semibold text-gray-500 uppercase tracking-wide w-10">
                <span className="sr-only">Select</span>
              </th>
              {TABLE_COLS.map((col) => (
                <th
                  key={col.key}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
                  title={col.tooltip || col.label}
                  className={`px-3 py-2.5 text-[10px] font-semibold text-gray-500 uppercase tracking-wide ${col.width} ${
                    col.sortable ? "cursor-pointer hover:text-gray-700 select-none" : ""
                  }`}
                >
                  <div className="flex items-center gap-1">
                    {col.label}
                    {sortKey === col.key && (
                      <span className="text-gray-700">{sortDir === "asc" ? "↑" : "↓"}</span>
                    )}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {sorted.map((f) => {
              const p = f.properties;
              const inList = exportList.has(p.bbl);
              return (
                <tr
                  key={p.bbl}
                  onClick={() => onSelectFeature(f)}
                  className={`hover:bg-gray-50 cursor-pointer transition-colors ${inList ? "bg-blue-50/50" : ""}`}
                >
                  <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => onAddToList(f)}
                      className={`w-4 h-4 rounded border-2 flex items-center justify-center transition-colors cursor-pointer ${
                        inList ? "bg-gray-800 border-gray-800" : "border-gray-300 hover:border-gray-500"
                      }`}
                    >
                      {inList && (
                        <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </svg>
                      )}
                    </button>
                  </td>
                  {TABLE_COLS.map((col) => (
                    <td key={col.key} className={`px-3 py-2 text-xs text-gray-700 ${col.width}`}>
                      {cellValue(col, p)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
        {sorted.length === 0 && (
          <div className="text-center text-sm text-gray-400 py-12">No buildings match the current filters</div>
        )}
        </div>
      </div>
    </div>
  );
}

function _removed() { /* buildOwnerGroups + OwnerView removed */
  const groups = new Map();
  for (const f of features) {
    const p = f.properties;
    const canon = p.owner_canonical || p.ownername || "";
    if (!canon || canon === "UNAVAILABLE OWNER") continue;
    if (!groups.has(canon)) {
      groups.set(canon, {
        name: canon,
        displayName: p.ownername || canon,
        buildings: [],
        totalUnits: 0,
        totalClassB: 0,
        tiers: new Set(),
        hasPriorOp: false,
        hasTempCoo: false,
        latestSaleDate: null,
      });
    }
    const g = groups.get(canon);
    // Deduplicate by address within owner group
    if (!g.buildings.some((b) => b.properties.address === p.address && b.properties.ownername === p.ownername)) {
      g.buildings.push(f);
    }
    g.totalUnits += estRooms(p).value;
    g.totalClassB += (p.hpd_class_b || 0);
    g.tiers.add(p.segment || p.tier);
    if (p.has_prior_op) g.hasPriorOp = true;
    if (p.coo_has_temporary) g.hasTempCoo = true;
    if (p.last_sale_date && (!g.latestSaleDate || p.last_sale_date > g.latestSaleDate)) {
      g.latestSaleDate = p.last_sale_date;
    }
  }
  return Array.from(groups.values());
}

const SCORE_SIGNALS = [
  { key: "hpd_classb", label: "HPD Class B rooms registered", pts: 35, max: 100, desc: "HPD registration shows Class B (transient) rooms. The legal foundation for short-stay operations." },
  { key: "hotel_class", label: "Hotel building class (H-series)", pts: 25, max: 100, desc: "DOB building classification is hotel. Transient use is inherent to the building." },
  { key: "dob_r1", label: "DOB R-1 transient occupancy", pts: 15, max: 100, desc: "DOB filings classify this building with R-1 (transient residential) occupancy." },
  { key: "coo_temp", label: "Temporary C of O issued", pts: 10, max: 100, desc: "A temporary Certificate of Occupancy has been issued, confirming transient use." },
  { key: "permit_transient", label: "DOB transient permit activity", pts: 8, max: 100, desc: "DOB permits reference transient-related work (hotel, SRO, transient keywords)." },
  { key: "r1_filings", label: "Multiple R-1 DOB filings", pts: 7, max: 100, desc: "3+ DOB filings with R-1 occupancy code. Sustained transient use history." },
  { key: "zoning_penalty", label: "Residential zoning (penalty)", pts: -15, max: 100, desc: "Zoning does not permit new hotel use. Only applies to buildings without existing Class B or H-class — those are grandfathered." },
];

function ScoreConfigPage({ features }) {
  const topBuildings = useMemo(() => {
    const scored = features.map((f) => ({
      feature: f,
      score: computeScore(f.properties),
    }));
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 10);
  }, [features]);

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="max-w-3xl mx-auto py-8 px-6 space-y-8">
        <div>
          <h2 className="text-lg font-bold text-gray-900">Legal score breakdown</h2>
          <p className="text-sm text-gray-500 mt-1">
            Buildings are scored purely on legal authority for transient use.
            Higher scores mean stronger evidence that the building is authorized for short-stay operations.
          </p>
        </div>

        {/* Signal breakdown */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: "#16a34a" }} />
            <h3 className="text-sm font-semibold text-gray-800">Legal signals</h3>
          </div>
          <div className="space-y-3">
            {SCORE_SIGNALS.map((sig) => (
              <div key={sig.key} className="flex items-start gap-3">
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <div className="w-14 shrink-0">
                    <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full" style={{ width: `${Math.abs(sig.pts / sig.max) * 100}%`, backgroundColor: sig.pts < 0 ? "#e11d48" : "#16a34a" }} />
                    </div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-xs text-gray-800 font-medium">{sig.label}</div>
                    <div className="text-[10px] text-gray-400 leading-snug">{sig.desc}</div>
                  </div>
                </div>
                <span className={`text-[11px] font-mono tabular-nums shrink-0 pt-0.5 ${sig.pts < 0 ? "text-red-500" : "text-gray-400"}`}>{sig.pts > 0 ? "+" : ""}{sig.pts}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Live preview: top 10 */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h3 className="text-sm font-semibold text-gray-800 mb-3">Top 10 by legal score</h3>
          <div className="space-y-2">
            {topBuildings.map(({ feature, score }, i) => {
              const p = feature.properties;
              const scoreColor = score >= 60 ? "bg-emerald-600" : score >= 35 ? "bg-amber-500" : "bg-gray-400";
              return (
                <div key={p.bbl + i} className="flex items-center gap-3 py-1.5 border-b border-gray-50 last:border-0">
                  <span className="text-[10px] text-gray-300 w-4 text-right tabular-nums">{i + 1}</span>
                  <span className={`${scoreColor} text-white text-[10px] font-bold px-1.5 py-0.5 rounded tabular-nums`}>{score}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs text-gray-800 font-medium truncate">{p.address}</div>
                    <div className="text-[10px] text-gray-400">{p.neighborhood || ""}</div>
                  </div>
                  <div className="w-16 h-1.5 bg-gray-100 rounded-full overflow-hidden shrink-0">
                    <div className="h-full rounded-full" style={{ width: `${score}%`, backgroundColor: "#16a34a" }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function OwnerView({ features, onSelectFeature, exportList, onAddToList }) {
  const [sortKey, setSortKey] = useState("totalUnits");
  const [sortDir, setSortDir] = useState("desc");
  const [searchText, setSearchText] = useState("");
  const [expandedOwner, setExpandedOwner] = useState(null);

  const owners = useMemo(() => buildOwnerGroups(features), [features]);

  const filtered = searchText.length >= 2
    ? owners.filter((o) => o.name.toLowerCase().includes(searchText.toLowerCase())
        || o.buildings.some((b) => (b.properties.address || "").toLowerCase().includes(searchText.toLowerCase())))
    : owners;

  const sorted = [...filtered].sort((a, b) => {
    let va, vb;
    if (sortKey === "name") {
      va = a.name.toLowerCase();
      vb = b.name.toLowerCase();
    } else if (sortKey === "buildings") {
      va = a.buildings.length;
      vb = b.buildings.length;
    } else if (sortKey === "totalUnits") {
      va = a.totalUnits;
      vb = b.totalUnits;
    } else if (sortKey === "totalClassB") {
      va = a.totalClassB;
      vb = b.totalClassB;
    } else if (sortKey === "bestTier") {
      const rank = { transient: 0, active_hotel: 1, partial: 2, unknown: 3 };
      va = Math.min(...[...a.tiers].map((t) => rank[t] ?? 99));
      vb = Math.min(...[...b.tiers].map((t) => rank[t] ?? 99));
    }
    if (va < vb) return sortDir === "asc" ? -1 : 1;
    if (va > vb) return sortDir === "asc" ? 1 : -1;
    return 0;
  });

  const handleSort = (key) => {
    if (sortKey === key) setSortDir((d) => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir("desc"); }
  };

  const OWNER_COLS = [
    { key: "name", label: "Owner", width: "min-w-[220px]" },
    { key: "buildings", label: "Buildings", width: "min-w-[85px]" },
    { key: "totalUnits", label: "Est. Rooms", width: "min-w-[95px]" },
    { key: "totalClassB", label: "Class B Rooms", width: "min-w-[100px]" },
    { key: "bestTier", label: "Best Tier", width: "min-w-[120px]" },
  ];

  const bestTier = (tiers) => {
    const rank = ["transient", "active_hotel", "partial", "unknown"];
    for (const t of rank) if (tiers.has(t)) return t;
    return "unknown";
  };

  return (
    <div className="h-full flex flex-col bg-white">
      <div className="px-4 py-3 pr-[22rem] border-b border-gray-200 shrink-0">
        <div className="flex items-center gap-3">
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="Search owner or address..."
            className="flex-1 px-3 py-2 bg-gray-50 rounded-lg border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:ring-2 focus:ring-gray-300"
          />
          <span className="text-xs text-gray-400 shrink-0">{sorted.length} owners</span>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-gray-50 z-10">
            <tr>
              {OWNER_COLS.map((col) => (
                <th
                  key={col.key}
                  onClick={() => handleSort(col.key)}
                  className={`px-4 py-2.5 text-[10px] font-semibold text-gray-500 uppercase tracking-wide cursor-pointer hover:text-gray-700 select-none ${col.width}`}
                >
                  <div className="flex items-center gap-1">
                    {col.label}
                    {sortKey === col.key && <span className="text-gray-700">{sortDir === "asc" ? "↑" : "↓"}</span>}
                  </div>
                </th>
              ))}
              <th className="px-4 py-2.5 text-[10px] font-semibold text-gray-500 uppercase tracking-wide min-w-[120px]">Signals</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {sorted.map((owner) => {
              const isExpanded = expandedOwner === owner.name;
              const bt = bestTier(owner.tiers);
              return (
                <Fragment key={owner.name}>
                  <tr
                    onClick={() => setExpandedOwner(isExpanded ? null : owner.name)}
                    className="hover:bg-gray-50 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className={`text-[10px] text-gray-400 transition-transform ${isExpanded ? "rotate-90" : ""}`}>▶</span>
                        <div>
                          <div className="text-xs font-medium text-gray-900 truncate max-w-[200px]" title={owner.displayName}>{owner.displayName}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-700">{owner.buildings.length}</td>
                    <td className="px-4 py-3 text-xs text-gray-700 font-medium">{owner.totalUnits.toLocaleString()}</td>
                    <td className="px-4 py-3 text-xs text-gray-700">{owner.totalClassB > 0 ? owner.totalClassB.toLocaleString() : "—"}</td>
                    <td className="px-4 py-3">
                      <span
                        className="inline-block px-2 py-0.5 rounded-full text-[10px] font-semibold text-white"
                        style={{ backgroundColor: segmentColor(bt) }}
                      >
                        {bt.replace(/_/g, " ")}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex gap-1.5">
                        {owner.hasPriorOp && <span className="px-1.5 py-0.5 text-[9px] font-semibold rounded bg-purple-100 text-purple-700">Prior op</span>}
                        {owner.hasTempCoo && <span className="px-1.5 py-0.5 text-[9px] font-semibold rounded bg-amber-100 text-amber-700">Temp CO</span>}
                        {owner.latestSaleDate && <span className="px-1.5 py-0.5 text-[9px] rounded bg-gray-100 text-gray-500">Sale {owner.latestSaleDate.slice(0,4)}</span>}
                      </div>
                    </td>
                  </tr>
                  {isExpanded && owner.buildings.map((f) => {
                    const p = f.properties;
                    const inList = exportList.has(p.bbl);
                    return (
                      <tr
                        key={p.bbl}
                        className={`bg-gray-50/50 hover:bg-gray-100/50 cursor-pointer ${inList ? "bg-blue-50/30" : ""}`}
                      >
                        <td className="pl-12 pr-4 py-2">
                          <div className="flex items-center gap-2">
                            <button
                              onClick={(e) => { e.stopPropagation(); onAddToList(f); }}
                              className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center transition-colors cursor-pointer shrink-0 ${
                                inList ? "bg-gray-800 border-gray-800" : "border-gray-300 hover:border-gray-500"
                              }`}
                            >
                              {inList && (
                                <svg className="w-2 h-2 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                                </svg>
                              )}
                            </button>
                            <span
                              className="text-[11px] text-blue-600 hover:underline"
                              onClick={(e) => { e.stopPropagation(); onSelectFeature(f); }}
                            >
                              {p.address}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-2">
                          <span
                            className="inline-block px-1.5 py-0.5 rounded-full text-[9px] font-semibold text-white"
                            style={{ backgroundColor: segmentColor(p.segment) }}
                          >
                            {(p.tier || "").replace(/_/g, " ")}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-[11px] text-gray-600">{estRooms(p).value}</td>
                        <td className="px-4 py-2 text-[11px] text-gray-600">{p.hpd_class_b > 0 ? p.hpd_class_b : "—"}</td>
                        <td className="px-4 py-2 text-[11px] text-gray-500">{p.bldgclass}</td>
                        <td className="px-4 py-2 text-[11px] text-gray-500">{p.zonedist1}</td>
                      </tr>
                    );
                  })}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {sorted.length === 0 && (
          <div className="text-center text-sm text-gray-400 py-12">No owners match the current filters</div>
        )}
      </div>
    </div>
  );
}

function MethodologyView({ features, onDrillDown, onSelectFeature }) {
  const counts = useMemo(() => {
    const paths = {};
    const add = (key) => {
      if (!paths[key]) paths[key] = { count: 0, rooms: 0 };
      return paths[key];
    };
    const seen = new Set();

    for (const f of features) {
      const p = f.properties;
      const bbl = p.bbl;
      if (seen.has(bbl)) continue;
      seen.add(bbl);

      const bldg = (p.bldgclass || "").toUpperCase();
      const isHotelClass = bldg.startsWith("H") && bldg !== "HR" && bldg !== "H8";
      const classB = p.hpd_class_b || 0;
      const classA = p.hpd_class_a || 0;
      const hasLicense = !!p.has_hotel_license;
      const rc = (() => { try { return JSON.parse(p.reason_codes || "[]"); } catch { return []; } })();
      const hasDob = rc.includes("dob_transient_occupancy");
      const hasPrior = !!p.has_prior_op;
      const rooms = classB > 0 ? classB : (p.unitsres || 0);

      const seg = p.segment || "unknown";
      let key;
      if (isHotelClass && classB > 0) {
        key = classA > 0 ? "hc_cb__split" : "hc_cb__full";
      } else if (isHotelClass) {
        if (hasLicense) key = "hc_nocb__license";
        else if (hasPrior) key = "hc_nocb__prior";
        else key = "hc_nocb__other";
      } else if (classB > 0) {
        key = classA > 0 ? "nohc_cb__split" : "nohc_cb__full";
      } else {
        if (hasLicense) key = "nohc_nocb__license";
        else if (hasDob) key = "nohc_nocb__dob";
        else if (hasPrior) key = "nohc_nocb__prior";
        else key = "nohc_nocb__partial";
      }

      const entry = add(key);
      entry.count++;
      entry.rooms += rooms;

      if (classB > 0) {
        const cell = isHotelClass ? "hc_cb" : "nohc_cb";
        const segKey = seg === "active_hotel" ? `${cell}__active` : `${cell}__transient`;
        const segEntry = add(segKey);
        segEntry.count++;
        segEntry.rooms += rooms;
      }
    }
    return paths;
  }, [features]);

  const g = (key) => counts[key] || { count: 0, rooms: 0 };
  const sum = (...keys) => keys.reduce((a, k) => ({ count: a.count + g(k).count, rooms: a.rooms + g(k).rooms }), { count: 0, rooms: 0 });

  const cellTotals = {
    hc_cb: sum("hc_cb__full", "hc_cb__split"),
    hc_nocb: sum("hc_nocb__license", "hc_nocb__prior", "hc_nocb__other"),
    nohc_cb: sum("nohc_cb__full", "nohc_cb__split"),
    nohc_nocb: sum("nohc_nocb__license", "nohc_nocb__dob", "nohc_nocb__prior", "nohc_nocb__partial"),
  };

  const isHC = (p) => { const b = (p.bldgclass || "").toUpperCase(); return b.startsWith("H") && b !== "HR" && b !== "H8"; };
  const hasCB = (p) => (p.hpd_class_b || 0) > 0;
  const hasCA = (p) => (p.hpd_class_a || 0) > 0;
  const hasLic = (p) => !!p.has_hotel_license;
  const hasPO = (p) => !!p.has_prior_op;
  const hasDOB = (p) => { try { return JSON.parse(p.reason_codes || "[]").includes("dob_transient_occupancy"); } catch { return false; } };

  const isSeg = (p, s) => (p.segment || "unknown") === s;
  const FILTERS = {
    hc_cb: (p) => isHC(p) && hasCB(p),
    hc_cb__full: (p) => isHC(p) && hasCB(p) && !hasCA(p),
    hc_cb__split: (p) => isHC(p) && hasCB(p) && hasCA(p),
    hc_cb__active: (p) => isHC(p) && hasCB(p) && isSeg(p, "active_hotel"),
    hc_cb__transient: (p) => isHC(p) && hasCB(p) && !isSeg(p, "active_hotel"),
    hc_nocb: (p) => isHC(p) && !hasCB(p),
    hc_nocb__license: (p) => isHC(p) && !hasCB(p) && hasLic(p),
    hc_nocb__prior: (p) => isHC(p) && !hasCB(p) && !hasLic(p) && hasPO(p),
    hc_nocb__other: (p) => isHC(p) && !hasCB(p) && !hasLic(p) && !hasPO(p),
    nohc_cb: (p) => !isHC(p) && hasCB(p),
    nohc_cb__full: (p) => !isHC(p) && hasCB(p) && !hasCA(p),
    nohc_cb__split: (p) => !isHC(p) && hasCB(p) && hasCA(p),
    nohc_cb__active: (p) => !isHC(p) && hasCB(p) && isSeg(p, "active_hotel"),
    nohc_cb__transient: (p) => !isHC(p) && hasCB(p) && !isSeg(p, "active_hotel"),
    nohc_nocb: (p) => !isHC(p) && !hasCB(p),
    nohc_nocb__license: (p) => !isHC(p) && !hasCB(p) && hasLic(p),
    nohc_nocb__dob: (p) => !isHC(p) && !hasCB(p) && !hasLic(p) && hasDOB(p),
    nohc_nocb__prior: (p) => !isHC(p) && !hasCB(p) && !hasLic(p) && !hasDOB(p) && hasPO(p),
    nohc_nocb__partial: (p) => !isHC(p) && !hasCB(p) && !hasLic(p) && !hasDOB(p) && !hasPO(p),
  };

  const drill = (filterKey, label) => {
    if (onDrillDown) onDrillDown(FILTERS[filterKey], label);
  };

  const EXAMPLES = {
    hc_cb: [
      { bbl: "1011640047", label: "240 W 73rd St — Split-use, 142B / 76A, no active business" },
      { bbl: "4003590021", label: "37-02 10th St — Full hotel, 381B rooms, no active business" },
    ],
    hc_nocb: [
      { bbl: "1004150067", label: "139 Orchard St — Prior operator (ex-Sonder)" },
      { bbl: "1001060017", label: "320 Pearl St — No license or prior op, reversion window" },
    ],
    nohc_cb: [
      { bbl: "1010097502", label: "111 W 56th St — Split-use, 587B / 99A, RM class" },
      { bbl: "1013037503", label: "525 Lexington Ave — Full transient, 506B, RH class" },
    ],
    nohc_nocb: [
      { bbl: "1000537502", label: "123 Washington St — 223 units, DOB R-1, prior op" },
    ],
  };
  const showExample = (bbl) => {
    const f = features.find((ft) => String(ft.properties.bbl) === bbl);
    if (f && onSelectFeature) onSelectFeature(f);
  };
  const ExampleLink = ({ quadrant }) => (
    <div className="mt-2 pt-1.5 border-t border-dashed border-gray-200">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-blue-500">Example Targets</span>
      {EXAMPLES[quadrant].map((ex) => (
        <div key={ex.bbl} className="text-[11px] text-gray-500 cursor-pointer hover:text-blue-600 transition-colors py-0.5" onClick={() => showExample(ex.bbl)}>
          {ex.label}
        </div>
      ))}
    </div>
  );

  const CellCount = ({ data, filterKey, label, className = "" }) => (
    <div
      className={`flex items-baseline gap-1.5 cursor-pointer hover:opacity-70 transition-opacity ${className}`}
      onClick={() => drill(filterKey, label)}
    >
      <span className="text-lg font-bold text-gray-900">{data.count.toLocaleString()}</span>
      <span className="text-[11px] text-gray-400">properties</span>
      {data.rooms > 0 && (
        <span className="text-[11px] text-gray-400 ml-1">{data.rooms.toLocaleString()} rooms</span>
      )}
    </div>
  );

  const SubRow = ({ label, data, color, filterKey }) => {
    if (data.count === 0) return null;
    return (
      <div className="flex items-center justify-between py-1 cursor-pointer hover:bg-black/5 rounded px-1 -mx-1 transition-colors" onClick={() => drill(filterKey, label)}>
        <div className="flex items-center gap-1.5">
          {color && <span className="w-2 h-2 rounded-sm shrink-0" style={{ background: color }} />}
          <span className="text-[11px] text-gray-600">{label}</span>
        </div>
        <div className="flex items-baseline gap-1 text-right">
          <span className="text-[12px] font-semibold text-gray-800">{data.count}</span>
          {data.rooms > 0 && <span className="text-[10px] text-gray-400">{data.rooms.toLocaleString()} rm</span>}
        </div>
      </div>
    );
  };

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="max-w-4xl mx-auto px-6 py-8 space-y-6">
        <div>
          <h1 className="text-xl font-bold text-gray-900 tracking-tight">Classification Matrix</h1>
          <p className="text-sm text-gray-500 mt-1 max-w-xl">
            Every building is classified along two axes: <strong>PLUTO building class</strong> (is it coded as a hotel?)
            and <strong>HPD Class B registration</strong> (are transient rooms confirmed?). {(cellTotals.hc_cb.count + cellTotals.hc_nocb.count + cellTotals.nohc_cb.count + cellTotals.nohc_nocb.count).toLocaleString()} buildings after pipeline filters.
          </p>
          <p className="text-[11px] text-gray-400 mt-1 max-w-xl">
            Already excluded: incompatible zoning, universities, shelters, HDFCs, hospitals, YMCAs, warehouses, garages, vacant land, SROs, dormitories, 1-4 family homes, apartment hotels (R5), and buildings with negligible Class B.
          </p>
        </div>

        {/* Matrix */}
        <div className="grid grid-cols-[auto_1fr_1fr] gap-0">
          {/* Header row */}
          <div />
          <div className="text-center pb-2 px-2">
            <div className="text-[11px] font-bold uppercase tracking-wide text-emerald-700">HPD Class B — Yes</div>
            <div className="text-[10px] text-gray-400">Transient rooms confirmed</div>
          </div>
          <div className="text-center pb-2 px-2">
            <div className="text-[11px] font-bold uppercase tracking-wide text-gray-500">HPD Class B — No</div>
            <div className="text-[10px] text-gray-400">No transient registration</div>
          </div>

          {/* Row 1: Hotel class YES */}
          <div className="flex items-start justify-end pr-3 pt-4">
            <div className="text-right">
              <div className="text-[11px] font-bold uppercase tracking-wide text-emerald-700">Hotel<br/>Class</div>
              <div className="text-[10px] text-gray-400 mt-0.5">Yes</div>
            </div>
          </div>

          {/* Cell: Hotel class YES + Class B YES */}
          <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-4 m-1">
            <CellCount data={cellTotals.hc_cb} filterKey="hc_cb" label="Hotel Class + Class B" />
            <div className="text-[10px] font-semibold uppercase tracking-wide text-emerald-600 mt-2 mb-1">Strongest signal</div>
            <div className="text-[11px] text-gray-600 mb-2">Both PLUTO and HPD confirm hotel/transient use.</div>
            <div className="border-t border-emerald-100 pt-1.5 space-y-0">
              <SubRow label="Full hotel (Class B only)" data={g("hc_cb__full")} color="#16a34a" filterKey="hc_cb__full" />
              <SubRow label="Split-use (Class A + B)" data={g("hc_cb__split")} color="#8b5cf6" filterKey="hc_cb__split" />
            </div>
            <div className="border-t border-emerald-100 mt-1.5 pt-1.5 space-y-0">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-0.5">By Operator Status</div>
              <SubRow label="Active hotel (operator found)" data={g("hc_cb__active")} color="#16a34a" filterKey="hc_cb__active" />
              <SubRow label="Transient capacity (no active business)" data={g("hc_cb__transient")} color="#8b5cf6" filterKey="hc_cb__transient" />
            </div>
            <ExampleLink quadrant="hc_cb" />
          </div>

          {/* Cell: Hotel class YES + Class B NO */}
          <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 m-1">
            <CellCount data={cellTotals.hc_nocb} filterKey="hc_nocb" label="Hotel Class, no Class B" />
            <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-600 mt-2 mb-1">Hotel class, unconfirmed</div>
            <div className="text-[11px] text-gray-600 mb-2">PLUTO says hotel but HPD has no transient rooms registered.</div>
            <div className="border-t border-amber-100 pt-1.5 space-y-0">
              <SubRow label="Active hotel license (DCWP)" data={g("hc_nocb__license")} color="#16a34a" filterKey="hc_nocb__license" />
              <SubRow label="Prior operator known" data={g("hc_nocb__prior")} color="#a855f7" filterKey="hc_nocb__prior" />
              <SubRow label="No license or prior operator" data={g("hc_nocb__other")} color="#94a3b8" filterKey="hc_nocb__other" />
            </div>
            <ExampleLink quadrant="hc_nocb" />
          </div>

          {/* Row 2: Hotel class NO */}
          <div className="flex items-start justify-end pr-3 pt-4">
            <div className="text-right">
              <div className="text-[11px] font-bold uppercase tracking-wide text-gray-500">Non-<br/>Hotel<br/>Class</div>
              <div className="text-[10px] text-gray-400 mt-0.5">No</div>
            </div>
          </div>

          {/* Cell: Hotel class NO + Class B YES */}
          <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 m-1">
            <CellCount data={cellTotals.nohc_cb} filterKey="nohc_cb" label="Non-hotel Class + Class B" />
            <div className="text-[10px] font-semibold uppercase tracking-wide text-blue-600 mt-2 mb-1">HPD-confirmed transient</div>
            <div className="text-[11px] text-gray-600 mb-2">Not classified as hotel in PLUTO but HPD confirms transient rooms exist.</div>
            <div className="border-t border-blue-100 pt-1.5 space-y-0">
              <SubRow label="Full transient (Class B only)" data={g("nohc_cb__full")} color="#16a34a" filterKey="nohc_cb__full" />
              <SubRow label="Split-use (Class A + B)" data={g("nohc_cb__split")} color="#8b5cf6" filterKey="nohc_cb__split" />
            </div>
            <div className="border-t border-blue-100 mt-1.5 pt-1.5 space-y-0">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-0.5">By Operator Status</div>
              <SubRow label="Active hotel (operator found)" data={g("nohc_cb__active")} color="#16a34a" filterKey="nohc_cb__active" />
              <SubRow label="Transient capacity (no active business)" data={g("nohc_cb__transient")} color="#8b5cf6" filterKey="nohc_cb__transient" />
            </div>
            <ExampleLink quadrant="nohc_cb" />
          </div>

          {/* Cell: Hotel class NO + Class B NO */}
          <div className="bg-white border border-gray-200 rounded-xl p-4 m-1">
            <CellCount data={cellTotals.nohc_nocb} filterKey="nohc_nocb" label="Non-hotel Class, no Class B" />
            <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mt-2 mb-1">Partial signals only</div>
            <div className="text-[11px] text-gray-600 mb-2">No hotel class, no Class B. Included based on other transient indicators.</div>
            <div className="border-t border-gray-100 pt-1.5 space-y-0">
              <SubRow label="Active hotel license (DCWP)" data={g("nohc_nocb__license")} color="#16a34a" filterKey="nohc_nocb__license" />
              <SubRow label="DOB transient occupancy (R-1/J-1)" data={g("nohc_nocb__dob")} color="#f59e0b" filterKey="nohc_nocb__dob" />
              <SubRow label="Prior operator known" data={g("nohc_nocb__prior")} color="#a855f7" filterKey="nohc_nocb__prior" />
              <SubRow label="Mixed-use building class" data={g("nohc_nocb__partial")} color="#94a3b8" filterKey="nohc_nocb__partial" />
            </div>
            <ExampleLink quadrant="nohc_nocb" />
          </div>
        </div>

        {/* Segments */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Segments</div>
          <div className="space-y-2 text-[11px] text-gray-600">
            <div className="flex items-start gap-2">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0 mt-0.5" style={{ background: "#8b5cf6" }} />
              <div><strong className="text-gray-800">Transient capacity</strong> — Buildings with legally established transient use (HPD Class B rooms, DOB R-1 occupancy, or hotel building class) but no known active hotel operator. The primary sourcing targets.</div>
            </div>
            <div className="flex items-start gap-2">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0 mt-0.5" style={{ background: "#16a34a" }} />
              <div><strong className="text-gray-800">Active hotel</strong> — Buildings with an identified hotel operator via DCWP license, Google Places, or operator name keywords. Already has a hotel operator in place.</div>
            </div>
            <div className="flex items-start gap-2">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0 mt-0.5" style={{ background: "#f59e0b" }} />
              <div><strong className="text-gray-800">Partial signal</strong> — Building class suggests mixed use (RM, RC, etc.) but no confirmed transient rooms from HPD or DOB. May have transient capacity — needs manual verification.</div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-gray-100 text-[11px] text-gray-500">
            <div className="flex items-start gap-1.5">
              <span className="bg-red-500 text-white text-[9px] font-semibold px-1.5 py-0.5 rounded-md shrink-0 mt-0.5">Reversion</span>
              <span>Overlay — hotels that closed or converted to residential post-2021 (6 tracked). Can revert to hotel use without CPC special permit because their hotel use predates the 2021 text amendment. Identified through manual research; data sources lag behind real-world closures.</span>
            </div>
          </div>
        </div>

        {/* Transient signals */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Transient Use Signals</div>
          <div className="text-[11px] text-gray-500 mb-2">A building qualifies as having legally established transient capacity through any of these signals:</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">HPD Class B rooms</strong> — Transient rooms registered with the city under the Multiple Dwelling Law. Renewed annually by building owners, making it the most current and reliable signal of active transient capacity.</div>
            <div><strong className="text-gray-600">Hotel building class (H*)</strong> — PLUTO classifies the building as a hotel. Updated annually by DCP, but can lag behind real-world changes. A building may retain its H-class even after ceasing hotel operations.</div>
            <div><strong className="text-gray-600">DOB R-1 occupancy</strong> — Department of Buildings filings for R-1 (transient residential) occupancy group. Direct evidence of legally established transient use.</div>
            <div><strong className="text-gray-600">DCWP hotel license</strong> — Active or lapsed city license to operate a hotel at the address. An operating signal, not a zoning signal.</div>
            <div><strong className="text-gray-600">Composite signals</strong> — Weaker signals (DOB J-1 filings, temporary certificates of occupancy, transient keywords in permit descriptions, prior operator records) that individually don't qualify but together indicate transient capacity when 3+ are present.</div>
          </div>
        </div>

        {/* Pipeline filters */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Pipeline Filters</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">Zoning</strong> — Incompatible zoning districts removed (unless building has HPD Class B rooms or is a post-2021 reversion)</div>
            <div><strong className="text-gray-600">Special permit</strong> — Non-operating hotel-class buildings removed (would require CPC special permit under 2021 text amendment)</div>
            <div><strong className="text-gray-600">Non-target</strong> — Universities, shelters, HDFCs/supportive housing, YMCAs, Salvation Army, hospitals, nursing homes, religious facilities excluded. Also excludes non-residential building classes: warehouses, factories, garages, vacant land, transportation, parks, apartment hotels (R5).</div>
            <div><strong className="text-gray-600">Empty non-hotel</strong> — Buildings with 0 residential units, 0 Class B rooms, and no hotel signals (non-hotel/non-residential building classes like offices and stores)</div>
            <div><strong className="text-gray-600">Negligible Class B</strong> — Non-hotel-class buildings with {"<"}=3 Class B rooms in 20+ unit residential buildings (likely super's unit or data noise)</div>
            <div><strong className="text-gray-600">Other</strong> — 1-4 family residential excluded. SRO (HR) and dormitory (H8) excluded from hotel tier.</div>
          </div>
        </div>

        {/* Operator detection */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Operator Detection</div>
          <div className="text-[11px] text-gray-500 mb-2">A building is tagged as "active hotel" if any of these identify an operator:</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">Google Places</strong> — API enrichment for all H-class buildings. Returns hotel name if Google recognizes a hotel at the address. Results filtered for false positives (same name matching 4+ BBLs = proximity artifact).</div>
            <div><strong className="text-gray-600">DCWP license</strong> — Active hotel license from the city.</div>
            <div><strong className="text-gray-600">Operator name keywords</strong> — HPD managing agent or operator name contains "hotel", "inn", "suites", "hostel", or "motel".</div>
          </div>
          <div className="text-[11px] text-gray-400 mt-2 italic">Caveat: Google Places and DCWP data can be stale. Some buildings flagged as active hotels have actually closed (e.g., migrant shelter hotels). Reversion overlay tracks known closures.</div>
        </div>

        {/* Regulatory context */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Regulatory Context</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">2021 Citywide Hotel Text Amendment</strong> — All new hotels in NYC require a CPC special permit. Existing hotels are grandfathered. Converting Class A (residential) to Class B (transient) = new hotel use = requires special permit.</div>
            <div><strong className="text-gray-600">Usable capacity</strong> — Only existing HPD Class B rooms represent immediately usable transient capacity. Class A units cannot be converted without triggering the special permit requirement.</div>
            <div><strong className="text-gray-600">Post-2021 reversions</strong> — Hotels that converted to residential after the amendment can revert without special permit because their hotel use predates the amendment. Currently tracked via manual research — DOB conversion filings and data sources lag behind real-world closures by months to years.</div>
          </div>
        </div>

        {/* Client-side filters */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Client-Side Filters</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">Hide condos</strong> — Condominiums identified by "CONDO" in owner name or building class R1/R2/R4. Condos require board approval or commercial condo owner negotiation — different deal structure than rentals.</div>
            <div><strong className="text-gray-600">Distress signals only</strong> — Tax liens, lis pendens/judgments, high ECB fines, or significant HPD violations.</div>
            <div><strong className="text-gray-600">No known operator</strong> — Buildings where Google Places, DCWP, and operator name keywords found no active hotel operation.</div>
            <div><strong className="text-gray-600">Hide by brand type</strong> — Three toggles: <em>Chains</em> (Marriott, Hilton, etc.), <em>Branded independents</em> (Arlo, Dream — potential targets), and <em>Private clubs</em> (Soho House, NY Athletic Club). Chains and clubs hidden by default; branded independents shown.</div>
            <div><strong className="text-gray-600">Min Class B</strong> — Minimum HPD Class B room count (default: 10). Exempts partial signal and active hotel segments to avoid filtering out buildings with zero Class B by definition.</div>
            <div><strong className="text-gray-600">Min rooms</strong> — Minimum room count threshold (estimated from Class B, C of O, PLUTO, or floor count).</div>
          </div>
        </div>

        {/* Scoring framework */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Scoring Framework</div>
          <div className="text-[11px] text-gray-500 mb-2">Each building receives a score (0-100) based on the strength of its transient capacity signals:</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">HPD Class B rooms (+35)</strong> — Strongest signal. Annual registration confirms active transient capacity.</div>
            <div><strong className="text-gray-600">Hotel building class (+25)</strong> — H-series PLUTO classification. Strong but can be stale.</div>
            <div><strong className="text-gray-600">DOB R-1 transient occupancy (+15)</strong> — Filings establishing R-1 occupancy group.</div>
            <div><strong className="text-gray-600">Temporary C of O (+10)</strong> — Temporary certificate of occupancy issued.</div>
            <div><strong className="text-gray-600">DOB transient permit activity (+8)</strong> — Permits with transient-related work descriptions.</div>
            <div><strong className="text-gray-600">Multiple R-1 filings (+7)</strong> — 3+ DOB filings for R-1 occupancy.</div>
            <div><strong className="text-gray-600">Residential zoning penalty (-15)</strong> — Applied only to buildings <em>without</em> existing transient rights (no Class B and no H-class). Buildings with Class B or H-class are grandfathered and exempt from this penalty.</div>
          </div>
        </div>

        {/* Brand classification */}
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Brand Classification</div>
          <div className="text-[11px] text-gray-500 mb-2">Buildings with recognized hotel brands are classified into three types:</div>
          <div className="space-y-1 text-[11px] text-gray-500">
            <div><strong className="text-gray-600">Chain</strong> — Major flag systems (Marriott, Hilton, Hyatt, IHG, Wyndham, etc.). Detected by operator name or Google Places hotel name matching. Hidden by default — not management targets.</div>
            <div><strong className="text-gray-600">Independent</strong> — Recognizable independent brands (Arlo, Dream, Gansevoort, Pod, etc.). May be open to management changes. Shown by default.</div>
            <div><strong className="text-gray-600">Club</strong> — Private members' clubs (Soho House, NY Athletic Club, Harvard Club, etc.). Not hotel targets. Hidden by default.</div>
          </div>
        </div>

        {/* Data sources */}
        <div className="text-[11px] text-gray-400 leading-relaxed border-t border-gray-200 pt-4">
          <strong className="text-gray-500">Data sources:</strong> PLUTO (building class, zoning), HPD registration (Class A/B unit counts, managing agents),
          DCWP hotel licenses, DOB occupancy filings (R-1/J-1 groups, conversion records), Google Places API (hotel name enrichment for H-class buildings),
          OSM hotel data, prior operator ground truth. Condo status derived from owner name and building class (R1/R2/R4).
          Reversion buildings tracked via manual research (news, deal records). Room counts use HPD Class B when available, otherwise PLUTO residential units.
        </div>

        {/* Data sources */}
        <div className="text-[11px] text-gray-400 leading-relaxed border-t border-gray-200 pt-4">
          <strong className="text-gray-500">Data sources:</strong> PLUTO (building class, zoning), HPD registration (Class A/B unit counts, managing agents),
          DCWP hotel licenses, DOB occupancy filings (R-1/J-1 groups, conversion records), Google Places API (hotel name enrichment for H-class buildings),
          OSM hotel data, prior operator ground truth. Condo status derived from owner name and building class (R1/R2/R4).
          Reversion buildings tracked via manual research (news, deal records). Room counts use HPD Class B when available, otherwise PLUTO residential units.
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const allFeaturesRef = useRef([]);
  // Bumped once the geojson lands. tableFeatures reads allFeaturesRef, which a
  // useMemo cannot depend on — without this the table computed against an empty
  // ref on first paint and stayed empty until some other dep changed.
  const [featuresVersion, setFeaturesVersion] = useState(0);
  const currentFilterRef = useRef(null);
  const [inspectedFeature, setInspectedFeature] = useState(null); // single click detail
  const { notes, save: saveNote } = useNotes();
  const { statuses: crmStatuses, setStatus: setCrmStatus } = useCrmStatuses();
  const [exportList, setExportList] = useState(new Map()); // bbl -> feature
  const [listExpanded, setListExpanded] = useState(false);
  const [activeSegments, setActiveSegments] = useState(
    Object.fromEntries(SEGMENTS.map((s) => [s.key, s.defaultOn]))
  );
  // Both overlays bypass every refinement filter (see applyFilters), so leaving
  // them on by default injected rows that ignored the user's own filters.
  const [showPriorOps, setShowPriorOps] = useState(false);
  const [showReversion, setShowReversion] = useState(false);
  const [distressOnly, setDistressOnly] = useState(false);
  const [noOperatorOnly, setNoOperatorOnly] = useState(false);
  const [hideBrandTypes, setHideBrandTypes] = useState(new Set(["chain", "club"]));
  const toggleBrandType = useCallback((bt) => setHideBrandTypes(prev => {
    const next = new Set(prev);
    next.has(bt) ? next.delete(bt) : next.add(bt);
    return next;
  }), []);
  const [hideCondos, setHideCondos] = useState(false);
  // SRO / dormitory / hostel stock is hidden by default: it scores well on Class B
  // rooms but sits in a different regulatory and operating world.
  const [hideRestricted, setHideRestricted] = useState(true);
  const [minUnits, setMinUnits] = useState(0);
  const [minClassB, setMinClassB] = useState(10);
  const [extraFilters, setExtraFilters] = useState({
    filterTempCoo: false,
    filterHasClassB: false,
    filterMultiOwner: false,
    filterRecentSale: false,
    filterCommercialZone: false,
    _recentSaleCutoff: new Date(Date.now() - 3 * 365 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10),
  });
  const setFilter = useCallback((key, val) => {
    setExtraFilters((prev) => ({ ...prev, [key]: val }));
  }, []);
  const [activeView, setActiveView] = useState("map"); // "map" | "table" | "methodology"
  const [matrixFilter, setMatrixFilter] = useState(null); // { fn, label } from methodology drill-down
  // Score is now purely legal — no configurable weights
  const [featureCount, setFeatureCount] = useState(0);
  const [overlayCounts, setOverlayCounts] = useState({ priorOps: 0, priorOpsExtra: 0, reversions: 0, reversionsExtra: 0, tempCoo: 0, segmentCounts: {} });
  const [dataDate, setDataDate] = useState(null);

  // Load GeoJSON for overlay counts + bulk select
  useEffect(() => {
    fetch("/buildings.geojson")
      .then((r) => r.json())
      .then((data) => {
        const feats = data.features || [];
        allFeaturesRef.current = feats;
        setFeaturesVersion((v) => v + 1);
        const segCounts = {};
        const seenBBLs = {};
        for (const f of feats) {
          const seg = f.properties.segment || "unknown";
          const bbl = f.properties.bbl;
          if (!seenBBLs[seg]) seenBBLs[seg] = new Set();
          if (!seenBBLs[seg].has(bbl)) {
            seenBBLs[seg].add(bbl);
            segCounts[seg] = (segCounts[seg] || 0) + 1;
          }
        }
        const priorOpsAll = feats.filter((f) => f.properties.has_prior_op);
        const priorOpsExtra = priorOpsAll.filter((f) => f.properties.segment !== "transient");
        const revAll = feats.filter((f) => f.properties.has_reversion);
        const revExtra = revAll.filter((f) => f.properties.segment !== "transient" && f.properties.segment !== "active_hotel");
        setOverlayCounts({
          priorOps: priorOpsAll.length,
          priorOpsExtra: priorOpsExtra.length,
          reversions: revAll.length,
          reversionsExtra: revExtra.length,
          tempCoo: feats.filter((f) => f.properties.coo_has_temporary).length,
          segmentCounts: segCounts,
        });
        // Extract data date from first feature's source_pulled_on (YYYYMMDD)
        const raw = feats[0]?.properties?.source_pulled_on || "";
        if (raw.length === 8) {
          const d = new Date(`${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}`);
          const daysAgo = Math.floor((Date.now() - d.getTime()) / 86400000);
          setDataDate({
            formatted: d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }),
            daysAgo,
          });
        }
      })
      .catch(() => {});
  }, []);

  const handleAddToList = useCallback((feature) => {
    setExportList((prev) => {
      const next = new Map(prev);
      const bbl = feature.properties.bbl;
      if (next.has(bbl)) {
        next.delete(bbl);
      } else {
        next.set(bbl, feature);
      }
      return next;
    });
  }, []);

  const handleRemoveFromList = useCallback((bbl) => {
    setExportList((prev) => {
      const next = new Map(prev);
      next.delete(bbl);
      return next;
    });
  }, []);

  const handleAddCategory = useCallback((propKey) => {
    setExportList((prev) => {
      const next = new Map(prev);
      for (const f of allFeaturesRef.current) {
        if (f.properties[propKey]) {
          next.set(f.properties.bbl, f);
        }
      }
      return next;
    });
    setListExpanded(true);
  }, []);

  const handleAddAllVisible = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    const features = map.queryRenderedFeatures({ layers: ["buildings-fill"] });
    setExportList((prev) => {
      const next = new Map(prev);
      const seen = new Set();
      for (const f of features) {
        const bbl = f.properties.bbl;
        if (!seen.has(bbl)) {
          seen.add(bbl);
          next.set(bbl, f);
        }
      }
      return next;
    });
    setListExpanded(true);
  }, []);

  const handleClearList = useCallback(() => {
    setExportList(new Map());
    setListExpanded(false);
  }, []);

  // Update selection highlight on map
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getSource("buildings")) return;
    map.removeFeatureState({ source: "buildings" });
    for (const bbl of exportList.keys()) {
      map.setFeatureState({ source: "buildings", id: bbl }, { selected: true });
    }
  }, [exportList]);

  // Map init
  useEffect(() => {
    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: "https://tiles.openfreemap.org/styles/positron",
      center: [-73.97, 40.75],
      zoom: 11.5,
      minZoom: 10,
      maxZoom: 19,
    });

    map.addControl(new maplibregl.NavigationControl(), "bottom-right");

    map.on("load", () => {
      // Polygon source for building footprints (zoomed in)
      map.addSource("buildings", {
        type: "geojson",
        data: "/buildings.geojson",
        promoteId: "bbl",
      });

      // Point source for clusters (zoomed out) — built from centroids
      fetch("/buildings.geojson")
        .then((r) => r.json())
        .then((data) => {
          // Build points from centroids, deduplicating by address (condo lots share an address)
          const SEG_RANK = { transient: 0, active_hotel: 1, partial: 2, unknown: 3 };
          const pointMap = new Map();
          for (const f of (data.features || [])) {
            const c = featureCentroid(f);
            if (!c) continue;
            const [cx, cy] = c;
            const addr = (f.properties.address || "").trim();
            const key = addr || `${cx.toFixed(4)},${cy.toFixed(4)}`;
            const existing = pointMap.get(key);
            if (!existing || (SEG_RANK[f.properties.segment] ?? 99) < (SEG_RANK[existing.properties.segment] ?? 99)) {
              pointMap.set(key, {
                type: "Feature",
                geometry: { type: "Point", coordinates: [cx, cy] },
                properties: f.properties,
              });
            }
          }
          const points = { type: "FeatureCollection", features: Array.from(pointMap.values()) };

          map.addSource("buildings-points", {
            type: "geojson",
            data: points,
          });

          // Clean circles — tier color only, slightly larger for overlay signals
          map.addLayer({
            id: "buildings-dots",
            type: "circle",
            source: "buildings-points",
            filter: currentFilterRef.current || initFilter,
            maxzoom: CLUSTER_ZOOM_THRESHOLD + 0.5,
            paint: {
              "circle-color": buildColorExpr(),
              "circle-radius": [
                "interpolate", ["linear"], ["zoom"],
                10, 2.5,
                12, 3.5,
                CLUSTER_ZOOM_THRESHOLD, 5,
              ],
              "circle-opacity": [
                "interpolate", ["linear"], ["zoom"],
                CLUSTER_ZOOM_THRESHOLD - 0.5, 0.8,
                CLUSTER_ZOOM_THRESHOLD + 0.5, 0,
              ],
              "circle-stroke-width": 0.5,
              "circle-stroke-color": "#fff",
              "circle-stroke-opacity": [
                "interpolate", ["linear"], ["zoom"],
                CLUSTER_ZOOM_THRESHOLD - 0.5, 0.6,
                CLUSTER_ZOOM_THRESHOLD + 0.5, 0,
              ],
            },
          }, "buildings-fill");

          // Click dot → open detail
          map.on("click", "buildings-dots", (e) => {
            if (e.features?.length) setInspectedFeature(e.features[0]);
          });
          map.on("mousemove", "buildings-dots", () => { map.getCanvas().style.cursor = "pointer"; });
          map.on("mouseleave", "buildings-dots", () => { map.getCanvas().style.cursor = ""; });

        });

      const initFilter = buildFilter(
        Object.fromEntries(SEGMENTS.map((s) => [s.key, s.defaultOn])),
        true, true, 0, 0, {
        filterTempCoo: false, filterHasClassB: false, filterMultiOwner: false,
        filterRecentSale: false, filterCommercialZone: false,
      }, false, false, true, false);

      // Building footprint layers — only visible when zoomed in
      map.addLayer({
        id: "buildings-fill",
        type: "fill",
        source: "buildings",
        filter: initFilter,
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "fill-color": buildColorExpr(),
          "fill-opacity": buildOpacityExpr(),
        },
      });

      map.addLayer({
        id: "buildings-outline",
        type: "line",
        source: "buildings",
        filter: initFilter,
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "line-color": buildColorExpr(),
          "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.5, 16, 1, 18, 2],
          "line-opacity": 0.6,
        },
      });

      // Selection highlight
      map.addLayer({
        id: "selection-outline",
        type: "line",
        source: "buildings",
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "line-color": "#000",
          "line-width": ["interpolate", ["linear"], ["zoom"], 14, 2, 16, 3.5, 18, 5],
          "line-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 1, 0],
        },
      });

      // Prior operator outline — dark cool tone
      map.addLayer({
        id: "prior-op-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_prior_op"], true],
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "line-color": "#6b21a8",
          "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.8, 16, 1.5, 18, 2],
          "line-opacity": 0.6,
        },
      });

      // Reversion outline — red
      map.addLayer({
        id: "reversion-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_reversion"], true],
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "line-color": "#dc2626",
          "line-width": ["interpolate", ["linear"], ["zoom"], 14, 1, 16, 2, 18, 2.5],
          "line-opacity": 0.7,
        },
      });


      map.on("mousemove", "buildings-fill", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "buildings-fill", () => {
        map.getCanvas().style.cursor = "";
      });

      map.on("click", "buildings-fill", (e) => {
        if (e.features?.length) {
          setInspectedFeature(e.features[0]);
        }
      });

      const updateCount = () => {
        const layers = [];
        if (map.getLayer("buildings-fill")) layers.push("buildings-fill");
        if (map.getLayer("buildings-dots")) layers.push("buildings-dots");
        if (layers.length === 0) return;
        const features = map.queryRenderedFeatures({ layers });
        const uniqueBBLs = new Set(features.map((f) => f.properties.bbl));
        setFeatureCount(uniqueBBLs.size);
      };
      map.on("moveend", updateCount);
      map.on("sourcedata", updateCount);

      mapRef.current = map;
    });

    return () => map.remove();
  }, []);

  // Filter update
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("buildings-fill")) return;

    const filter = buildFilter(activeSegments, showPriorOps, showReversion, minUnits, minClassB, extraFilters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted);
    currentFilterRef.current = filter;
    map.setFilter("buildings-fill", filter);
    map.setFilter("buildings-outline", filter);
    if (map.getLayer("buildings-dots")) map.setFilter("buildings-dots", filter);

    if (map.getLayer("prior-op-outline")) map.setLayoutProperty("prior-op-outline", "visibility", showPriorOps ? "visible" : "none");
    if (map.getLayer("reversion-outline")) map.setLayoutProperty("reversion-outline", "visibility", showReversion ? "visible" : "none");

    setTimeout(() => {
      const layers = [];
      if (map.getLayer("buildings-fill")) layers.push("buildings-fill");
      if (map.getLayer("buildings-dots")) layers.push("buildings-dots");
      if (layers.length === 0) return;
      const features = map.queryRenderedFeatures({ layers });
      const uniqueBBLs = new Set(features.map((f) => f.properties.bbl));
      setFeatureCount(uniqueBBLs.size);
    }, 100);
  }, [activeSegments, showPriorOps, showReversion, minUnits, minClassB, extraFilters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted]);

  const tableFeatures = useMemo(() => {
    return applyFilters(allFeaturesRef.current, activeSegments, showPriorOps, showReversion, minUnits, minClassB, extraFilters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted);
  }, [featuresVersion, activeSegments, showPriorOps, showReversion, minUnits, minClassB, extraFilters, distressOnly, noOperatorOnly, hideBrandTypes, hideCondos, hideRestricted]);

  return (
    <div className="relative w-full h-full flex flex-col">
      {/* View toggle */}
      {!inspectedFeature && <div className="absolute top-4 right-4 z-30 flex bg-white rounded-lg shadow-lg border border-gray-200 overflow-hidden">
        <button
          onClick={() => setActiveView("map")}
          className={`w-20 py-2 text-xs font-medium transition-colors cursor-pointer text-center ${
            activeView === "map" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Map
        </button>
        <button
          onClick={() => { setMatrixFilter(null); setActiveView("table"); }}
          className={`w-20 py-2 text-xs font-medium transition-colors cursor-pointer text-center ${
            activeView === "table" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Table
        </button>
        <button
          onClick={() => setActiveView("methodology")}
          className={`w-24 py-2 text-xs font-medium transition-colors cursor-pointer text-center ${
            activeView === "methodology" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Methodology
        </button>
      </div>}

      {/* Map view */}
      <div ref={mapContainer} className="w-full h-full" style={{ display: activeView === "map" ? "block" : "none" }} />
      {activeView === "map" && (
        <div className="fixed bottom-8 left-1/2 -translate-x-1/2 bg-white/90 backdrop-blur-sm px-3 py-1.5 rounded-full shadow-md border border-gray-200 text-xs font-medium text-gray-700 pointer-events-none z-10">
          {featureCount.toLocaleString()} {featureCount === 1 ? "property" : "properties"} in view
        </div>
      )}

      {/* Table view */}
      {activeView === "table" && (
        <div className="flex-1 flex">
          <div className={`flex-1 h-full overflow-hidden transition-all ${inspectedFeature ? "pr-[26rem]" : ""}`}>
            <TableView
              features={tableFeatures}
              onSelectFeature={(f) => setInspectedFeature(f)}
              exportList={exportList}
              onAddToList={handleAddToList}
              extraFilters={extraFilters}
              setFilter={setFilter}
              activeSegments={activeSegments}
              setActiveSegments={setActiveSegments}
              distressOnly={distressOnly}
              setDistressOnly={setDistressOnly}
              minUnits={minUnits}
              setMinUnits={setMinUnits}
              minClassB={minClassB}
              setMinClassB={setMinClassB}
              showPriorOps={showPriorOps}
              setShowPriorOps={setShowPriorOps}
              showReversion={showReversion}
              setShowReversion={setShowReversion}
              notes={notes}
              matrixFilter={matrixFilter}
              onClearMatrixFilter={() => setMatrixFilter(null)}
            />
          </div>
        </div>
      )}

      {activeView === "methodology" && (
        <MethodologyView features={allFeaturesRef.current} onDrillDown={(fn, label) => {
          setMatrixFilter({ fn, label });
          setActiveView("table");
        }} onSelectFeature={(f) => { setInspectedFeature(f); setActiveView("map"); }} />
      )}

      {activeView === "map" && <FilterPanel
        activeSegments={activeSegments}
        setActiveSegments={setActiveSegments}
        showPriorOps={showPriorOps}
        setShowPriorOps={setShowPriorOps}
        showReversion={showReversion}
        setShowReversion={setShowReversion}
        distressOnly={distressOnly}
        setDistressOnly={setDistressOnly}
        noOperatorOnly={noOperatorOnly}
        setNoOperatorOnly={setNoOperatorOnly}
        hideBrandTypes={hideBrandTypes}
        toggleBrandType={toggleBrandType}
        hideCondos={hideCondos}
        hideRestricted={hideRestricted}
        setHideRestricted={setHideRestricted}
        setHideCondos={setHideCondos}
        minUnits={minUnits}
        setMinUnits={setMinUnits}
        minClassB={minClassB}
        setMinClassB={setMinClassB}
        featureCount={activeView !== "map" ? tableFeatures.length : featureCount}
        overlayCounts={overlayCounts}
        onAddAllVisible={activeView !== "map"
          ? () => {
              setExportList((prev) => {
                const next = new Map(prev);
                for (const f of tableFeatures) next.set(f.properties.bbl, f);
                return next;
              });
              setListExpanded(true);
            }
          : handleAddAllVisible
        }
        onAddCategory={handleAddCategory}
        dataDate={dataDate}
      />}

      {activeView === "map" && <SearchBar mapRef={mapRef} panelOpen={!!inspectedFeature} onSelectFeature={setInspectedFeature} />}

      <DetailPanel
        feature={inspectedFeature}
        onClose={() => setInspectedFeature(null)}
        onAddToList={handleAddToList}
        isInList={inspectedFeature ? exportList.has(inspectedFeature.properties.bbl) : false}
        notes={notes}
        onSaveNote={saveNote}
        crmStatuses={crmStatuses}
        setCrmStatus={setCrmStatus}
        allFeatures={allFeaturesRef.current}
      />

      <ListTray
        list={exportList}
        onRemove={handleRemoveFromList}
        onClear={handleClearList}
        onExpand={() => setListExpanded(!listExpanded)}
        expanded={listExpanded}
      />

      {/* Legend removed — tier colors are in the filter panel */}
    </div>
  );
}
