import { useEffect, useRef, useState, useCallback, useMemo, Fragment } from "react";
import maplibregl from "maplibre-gl";

const SEGMENTS = [
  {
    key: "reversion", label: "Reversion window", color: "#e11d48", defaultOn: true,
    info: "Hotel-class buildings converted to residential. Can revert to transient use without a special permit before Dec 9, 2027.",
  },
  {
    key: "split_use", label: "Split-use", color: "#8b5cf6", defaultOn: true,
    info: "Buildings with both HPD Class A (residential) and Class B (transient) units. Existing transient capacity alongside residential.",
  },
  {
    key: "pure_hotel", label: "Pure hotel", color: "#16a34a", defaultOn: false,
    info: "Fully operating hotels — HPD Class B confirmed, no residential apartments. Already transient, likely has an existing operator.",
  },
  {
    key: "partial", label: "Partial signal", color: "#f59e0b", defaultOn: false,
    info: "Building class suggests mixed use (RM, RC, etc.) but HPD didn't confirm Class B rooms. May have transient capacity — needs manual verification.",
  },
];

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


function computeScore(p, weights) {
  const total = weights.legal + weights.avail + weights.quality;
  if (total === 0) return 0;
  const wL = weights.legal / total;
  const wA = weights.avail / total;
  const wQ = weights.quality / total;
  return Math.round((p.score_legal || 0) * wL + (p.score_avail || 0) * wA + (p.score_quality || 0) * wQ);
}

function buildFilter(activeSegments, showPriorOps, minUnits, filters, distressOnly, noOperatorOnly, hideBranded) {
  const allowedSegs = Object.entries(activeSegments).filter(([, v]) => v).map(([k]) => k);
  const segFilter = ["in", ["get", "segment"], ["literal", allowedSegs]];
  const priorOpFilter = ["==", ["get", "has_prior_op"], true];

  const visibilityFilter = showPriorOps
    ? ["any", segFilter, priorOpFilter]
    : segFilter;

  const alwaysShowFilter = showPriorOps ? priorOpFilter : ["literal", false];

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
  if (hideBranded) {
    refinements.push(["!=", ["get", "is_branded"], true]);
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
  { key: "has_reversion", label: "Reversion Window" },
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
  { key: "reversion_deadline", label: "Reversion Deadline" },
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

function exportToCsv(features, scoreWeights) {
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
    const reversion = parseJsonProp(p.reversion_window);
    const reasons = parseJsonProp(p.reason_codes) || [];

    const rooms = estRooms(p);
    const row = {
      ...p,
      est_rooms: rooms.value,
      room_source: rooms.source,
      has_prior_op: p.has_prior_op ? "Yes" : "",
      has_reversion: p.has_reversion ? "Yes" : "",
      numfloors: p.numfloors ? Math.round(p.numfloors) : "",
      height_roof: p.height_roof ? Math.round(p.height_roof) : "",
      prior_operator_name: priorOp?.name || "",
      prior_operator_notes: priorOp?.notes || "",
      reversion_deadline: reversion?.deadline || "",
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

  const legal = [];
  if (p.tier === "legal_transient") legal.push({ label: "Legal transient tier", pts: 30, hit: true });
  else legal.push({ label: "Legal transient tier", pts: 30, hit: false });
  if (p.tier === "partial") legal.push({ label: "Partial signal tier", pts: 8, hit: true });
  else legal.push({ label: "Partial signal tier", pts: 8, hit: false });
  legal.push({ label: "Temporary C of O", pts: 10, hit: !!p.coo_has_temporary });
  legal.push({ label: "HPD Class B rooms > 0", pts: 10, hit: (p.hpd_class_b || 0) > 0 });

  const avail = [
    { label: "Prior operator departed", pts: 15, hit: !!p.has_prior_op },
    { label: "Tax lien on property", pts: 8, hit: !!p.has_tax_lien },
    { label: "Lis pendens / judgment", pts: 8, hit: !!p.has_lis_pendens },
    { label: "Recent sale (last 2 yrs)", pts: 5, hit: !!(p.last_sale_date && p.last_sale_date >= `${new Date().getFullYear() - 2}-01-01`) },
    { label: "Reversion window", pts: 5, hit: !!p.has_reversion },
    { label: "ECB balance > $10K", pts: 4, hit: (p.ecb_total_balance || 0) > 10000 },
  ];

  const quality = [
    { label: "Not an existing hotel", pts: 7, hit: !(p.bldgclass || "").startsWith("H") },
    { label: "Low Class C violations (<10)", pts: 5, hit: (p.hpd_class_c_violations || 0) < 10 },
    { label: "Multi-building owner", pts: 3, hit: (p.owner_portfolio_size || 0) > 1 },
  ];

  // Only show tier signals that are relevant (the one that hit, or all if none hit)
  const legalFiltered = legal.filter((s) => s.hit || !["Legal transient tier", "Class B tier", "Class B (split-use) tier", "Partial signal tier"].includes(s.label) || s.hit);
  // Simpler: show all, highlight which fired
  const sections = [
    { label: "Legal certainty", color: "#16a34a", signals: legal },
    { label: "Availability", color: "#2563eb", signals: avail },
    { label: "Building quality", color: "#8b5cf6", signals: quality },
  ];

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
        <div className="mt-2 space-y-3">
          {sections.map(({ label, color, signals }) => (
            <div key={label}>
              <div className="flex items-center gap-1.5 mb-1">
                <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: color }} />
                <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">{label}</span>
              </div>
              <div className="space-y-0.5 pl-3.5">
                {signals.map((sig) => (
                  <div key={sig.label} className="flex items-center gap-2">
                    <span className={`text-[11px] ${sig.hit ? "text-gray-800" : "text-gray-300"}`}>
                      {sig.hit ? "+" : "\u00A0\u00A0"}{sig.hit ? sig.pts : 0}
                    </span>
                    <span className={`text-[11px] ${sig.hit ? "text-gray-700" : "text-gray-300"}`}>
                      {sig.label}
                    </span>
                    {sig.hit && <span className="text-[10px] text-emerald-500">&#10003;</span>}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DetailPanel({ feature, onClose, onAddToList, isInList, scoreWeights, notes, onSaveNote, crmStatuses, setCrmStatus, allFeatures }) {
  if (!feature) return null;
  const p = feature.properties;
  const reasonCodes = parseJsonProp(p.reason_codes) || [];
  const blockers = parseJsonProp(p.blockers) || [];
  const priorOp = parseJsonProp(p.prior_operator);
  const reversion = parseJsonProp(p.reversion_window);

  return (
    <div className="absolute top-4 right-4 w-96 max-h-[calc(100vh-2rem)] overflow-y-auto bg-white rounded-xl shadow-2xl border border-gray-200 z-20">
      <div className="flex items-center justify-between p-4 border-b border-gray-100">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-gray-900 truncate pr-2">{p.address}</h2>
          {p.neighborhood && <div className="text-xs text-gray-400 mt-0.5">{p.neighborhood}</div>}
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none cursor-pointer shrink-0">&times;</button>
      </div>

      <div className="p-4 space-y-4">
        <div className="flex items-center gap-2">
          <span
            className="inline-block px-2.5 py-1 rounded-full text-xs font-semibold text-white"
            style={{ backgroundColor: segmentColor(p.segment) }}
          >
            {(p.segment || p.tier || "").replace(/_/g, " ")}
          </span>
          {[
            p.has_prior_op && { label: "Prior operator", color: "bg-purple-500" },
            p.has_tax_lien && { label: "Tax lien", color: "bg-red-600" },
            p.has_lis_pendens && { label: "Lis pendens", color: "bg-red-600" },
            p.coo_has_temporary && { label: "Temp C of O", color: "bg-amber-500" },
            p.mortgage_approaching_maturity && { label: "Mortgage maturing", color: "bg-orange-500" },
            p.is_landmark && { label: "Landmark", color: "bg-amber-600" },
            p.historic_district && { label: "Historic district", color: "bg-amber-500" },
          ].filter(Boolean).map((sig, i) => (
            <span key={i} className={`${sig.color} text-white text-[10px] font-semibold px-1.5 py-0.5 rounded-md`}>
              {sig.label}
            </span>
          ))}
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

        {priorOp && (
          <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
            <div className="text-xs font-semibold text-purple-700 uppercase tracking-wide mb-1">Prior flex operator</div>
            <div className="text-sm text-purple-900">{priorOp.name}</div>
            <div className="text-xs text-purple-600 mt-0.5">{priorOp.notes}</div>
            <div className="text-[10px] text-purple-400 mt-1">Legality unverified</div>
          </div>
        )}

        {reversion && (
          <div className="bg-rose-50 border border-rose-200 rounded-lg p-3">
            <div className="text-xs font-semibold text-rose-700 uppercase tracking-wide mb-1">Reversion window</div>
            <div className="text-sm text-rose-900">{reversion.class_a_units} Class A units, 0 Class B rooms</div>
            <div className="text-xs text-rose-600 mt-0.5">{reversion.note}</div>
            <div className="text-[10px] text-rose-500 font-semibold mt-1">Deadline: {reversion.deadline}</div>
          </div>
        )}

        {(() => {
          const rooms = estRooms(p);
          const sourceExplain = {
            "HPD Class B": "From HPD registration — the number of transient (Class B) rooms registered with the city. Most reliable source.",
            "C of O": "From DOB Certificate of Occupancy — the approved dwelling unit count. Reliable but may include residential units.",
            "Floor est.": `Estimated at ~15 rooms/floor × ${Math.round(p.numfloors || 0)} floors. No HPD registration or C of O on file for this hotel.`,
            "PLUTO": "From Dept. of Finance tax lot data. Counts residential dwelling units, not hotel rooms — accurate for residential buildings but undercounts hotels.",
            "Unknown": "No room count data available from any source.",
          };
          return (
            <div className="grid grid-cols-2 gap-3">
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
              <StatBldgClass code={p.bldgclass} />
              <Stat label="HPD Class B" value={p.hpd_class_b ?? "—"} />
              <Stat label="HPD Class A" value={p.hpd_class_a ?? "—"} />
              <Stat label="Zoning" value={p.zonedist1 || "—"} />
            </div>
          );
        })()}

        {reasonCodes.length > 0 && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Reason codes</div>
            <div className="flex flex-wrap gap-1">
              {reasonCodes.map((code) => (
                <span key={code} className="bg-gray-100 text-gray-700 text-xs px-2 py-0.5 rounded">{code}</span>
              ))}
            </div>
          </div>
        )}

        {blockers.length > 0 && (
          <div>
            <div className="text-xs font-semibold text-red-500 uppercase tracking-wide mb-1">Blockers</div>
            <div className="flex flex-wrap gap-1">
              {blockers.map((b) => (
                <span key={b} className="bg-red-50 text-red-700 text-xs px-2 py-0.5 rounded">{b}</span>
              ))}
            </div>
          </div>
        )}

        {p.hpd_dob_class && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">HPD DOB classification</div>
            <div className="text-sm text-gray-700">{p.hpd_dob_class}</div>
          </div>
        )}

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
                    {p.mortgage_approaching_maturity && (
                      <span className="text-[9px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-medium">
                        {p.mortgage_age_years}yr — may be maturing
                      </span>
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
                  <span className="text-[11px] text-amber-700 font-medium">Has Temporary C of Os — strong transient signal</span>
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

        {/* Distress signals */}
        {(() => {
          const hasLien = p.has_tax_lien;
          const hasLp = p.has_lis_pendens;
          const hpdV = p.hpd_open_violations || 0;
          const hpdC = p.hpd_class_c_violations || 0;
          const ecbV = p.ecb_open_violations || 0;
          const ecbBal = p.ecb_total_balance || 0;
          const hasLandmark = p.is_landmark || p.historic_district;
          if (!hasLien && !hasLp && hpdV === 0 && ecbV === 0 && !hasLandmark) return null;

          const signals = [];
          if (hasLien) signals.push({
            label: "Tax lien on property",
            detail: "The city has placed a lien on this property for unpaid taxes or charges. Indicates financial distress — the owner may be motivated to find revenue sources like a hotel management partner.",
            severity: "high",
          });
          if (hasLp) signals.push({
            label: `Lis pendens / judgment (${p.lis_pendens_count})`,
            detail: "A legal action (lawsuit or judgment) has been filed against this property in the last 5 years. Strong signal of financial or legal distress — owner may be under pressure to generate income or sell.",
            severity: "high",
          });
          if (hpdV > 0) signals.push({
            label: `${hpdV} open HPD violations` + (hpdC > 0 ? ` (${hpdC} Class C)` : ""),
            detail: "Open violations from NYC Housing Preservation & Development. Class C = immediately hazardous (structural, lead, fire safety). High counts may indicate deferred maintenance. Very high counts (20+) could mean the building needs significant capital — a risk factor, not just an opportunity signal.",
            severity: hpdC > 5 ? "high" : "medium",
          });
          if (ecbV > 0) signals.push({
            label: `${ecbV} ECB violations` + (ecbBal > 0 ? ` ($${ecbBal.toLocaleString()} balance)` : ""),
            detail: "Active violations from the Environmental Control Board (OATH). These carry financial penalties. A large unpaid balance signals the owner may be under financial pressure — relevant when combined with legal transient eligibility.",
            severity: ecbBal > 10000 ? "high" : "medium",
          });

          if (p.is_landmark) signals.push({
            label: `LPC Individual Landmark${p.landmark_name ? ` — ${p.landmark_name}` : ""}`,
            detail: "This building is individually designated by the Landmarks Preservation Commission. Any exterior alteration requires LPC approval. Renovations to improve the property will face additional cost and timeline.",
            severity: "medium",
          });
          if (p.historic_district) signals.push({
            label: `Historic District — ${p.historic_district}`,
            detail: "This building is within an LPC-designated historic district. Exterior changes (windows, facade, signage, rooftop) require LPC review. Interior changes are generally unaffected.",
            severity: "medium",
          });

          return (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">Signals</div>
              <div className="space-y-1">
                {signals.map((sig, i) => (
                  <DistressRow key={i} signal={sig} />
                ))}
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

function ListTray({ list, onRemove, onClear, onExpand, expanded, scoreWeights }) {
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
            onClick={() => exportToCsv(items, scoreWeights)}
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
                <div className="text-xs font-medium text-gray-900 truncate">{p.address}</div>
                <div className="text-[10px] text-gray-400">
                  {estRooms(p).value} rooms &middot; {p.bldgclass || "—"}
                  {priorOp ? ` · ${priorOp.name}` : ""}
                  {p.has_reversion ? " · reversion" : ""}
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
  distressOnly, setDistressOnly,
  noOperatorOnly, setNoOperatorOnly,
  hideBranded, setHideBranded,
  minUnits, setMinUnits,
  scoreWeights, setScoreWeights,
  featureCount, overlayCounts,
  onAddAllVisible, onAddCategory,
  dataDate,
}) {
  const [showScoreConfig, setShowScoreConfig] = useState(false);
  const toggleSegment = (key) => setActiveSegments((prev) => ({ ...prev, [key]: !prev[key] }));
  return (
    <div className="absolute top-4 left-4 w-72 bg-white/95 backdrop-blur rounded-xl shadow-xl border border-gray-200 z-20">
      <div className="p-4 border-b border-gray-100">
        <h1 className="text-sm font-bold text-gray-900 tracking-tight">NYC Transient Capacity</h1>
        <p className="text-[11px] text-gray-500 mt-0.5">Manhattan &middot; Downtown BK &middot; Williamsburg &middot; LIC</p>
        {dataDate && <p className="text-[10px] text-gray-400 mt-0.5">Data as of {dataDate}</p>}
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
                  <InfoTip text={seg.info} />
                </button>
              );
            })}
          </div>
        </div>

        {/* Prior operator overlay */}
        <div>
          <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-2">Overlay</div>
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
              <InfoTip text="Buildings previously operated by flex-stay companies (Sonder, Placemakr, Kasa, Mint House, etc.). Shows operational viability." />
            </div>
          </label>
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

          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={hideBranded}
              onChange={(e) => setHideBranded(e.target.checked)}
              className="sr-only"
            />
            <span
              className={`w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors ${
                hideBranded ? "bg-gray-800 border-gray-800" : "border-gray-300"
              }`}
            >
              {hideBranded && (
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </span>
            <span className="text-xs text-gray-700">Hide branded chains</span>
            <InfoTip text="Exclude major hotel chains (Marriott, Hilton, Hyatt, IHG, Wyndham, etc.). These are unlikely management targets. Independent hotels with operators still show." />
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
        </div>

        {/* Deal score weights */}
        <div>
          <button
            onClick={() => setShowScoreConfig(!showScoreConfig)}
            className="flex items-center gap-1.5 cursor-pointer text-left w-full"
          >
            <span className={`text-[10px] text-gray-400 transition-transform ${showScoreConfig ? "rotate-90" : ""}`}>&#9654;</span>
            <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Deal score weights</span>
          </button>
          {showScoreConfig && (
            <div className="mt-2 space-y-2.5 px-1">
              {[
                { key: "legal", label: "Legal certainty" },
                { key: "avail", label: "Availability" },
                { key: "quality", label: "Building quality" },
              ].map(({ key, label }) => (
                <div key={key}>
                  <div className="flex items-center justify-between mb-0.5">
                    <span className="text-[11px] text-gray-600">{label}</span>
                    <span className="text-[11px] font-mono text-gray-500 w-8 text-right">{scoreWeights[key]}%</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={scoreWeights[key]}
                    onChange={(e) => setScoreWeights((prev) => ({ ...prev, [key]: Number(e.target.value) }))}
                    className="w-full h-1 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-gray-700"
                  />
                </div>
              ))}
              <button
                onClick={() => setScoreWeights({ legal: 50, avail: 35, quality: 15 })}
                className="text-[10px] text-gray-400 hover:text-gray-600 cursor-pointer"
              >
                Reset to defaults
              </button>
            </div>
          )}
        </div>

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
            <button
              onClick={() => onAddCategory("has_reversion")}
              className="flex-1 px-2 py-1.5 text-[11px] rounded-lg border border-rose-200 text-rose-700 hover:bg-rose-50 cursor-pointer transition-colors"
            >
              + Reversion window
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const GOOGLE_PLACES_API_KEY = "AIzaSyCKgST4j-ZZ_SqXp09dciVbHTn_4gctzUM";

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
        const coords = f.geometry?.coordinates;
        if (!coords) continue;
        const ring = f.geometry.type === "MultiPolygon" ? coords[0][0] : coords[0];
        let cx = 0, cy = 0;
        for (const [x, y] of ring) { cx += x; cy += y; }
        cx /= ring.length; cy /= ring.length;
        const dist = Math.hypot(cx - center[0], cy - center[1]);
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
        // Google Places Text Search (New)
        fetch("https://places.googleapis.com/v1/places:searchText", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
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
  { key: "signals", label: "Signals", sortable: true, numeric: true, width: "min-w-[100px]" },
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
  { key: "permit_count", label: "Permits", sortable: true, numeric: true, width: "min-w-[75px]" },
  { key: "coo_count", label: "C of Os", sortable: true, numeric: true, width: "min-w-[75px]" },
  { key: "coo_has_temporary", label: "Temp CO", sortable: true, width: "min-w-[80px]" },
  { key: "zonedist1", label: "Zoning", sortable: true, width: "min-w-[85px]" },
];

function applyFilters(features, activeSegments, showPriorOps, minUnits, filters, distressOnly, noOperatorOnly, hideBranded) {
  return features.filter((f) => {
    const p = f.properties;
    const segOk = activeSegments[p.segment];
    const overlayOk = showPriorOps && p.has_prior_op;
    if (!segOk && !overlayOk) return false;

    if (!overlayOk && estRooms(p).value < minUnits) return false;

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
      if (hideBranded && p.is_branded) return false;
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
      const SEG_RANK = { reversion: 0, split_use: 1, pure_hotel: 2, partial: 3, unknown: 4 };
      if ((SEG_RANK[p.segment] ?? 99) < (SEG_RANK[existing.segment] ?? 99)) {
        groups.set(key, f);
      }
    }
  }
  return Array.from(groups.values());
}

function TableView({ features, onSelectFeature, exportList, onAddToList, extraFilters, setFilter, scoreWeights,
  activeSegments, setActiveSegments, distressOnly, setDistressOnly, minUnits, setMinUnits,
  showPriorOps, setShowPriorOps, notes,
}) {
  const [sortKey, setSortKey] = useState("signals");
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

  const filtered = searchText.length >= 2
    ? dedupedFeatures.filter((f) => {
        const p = f.properties;
        const text = searchText.toLowerCase();
        return (p.address || "").toLowerCase().includes(text)
          || (p.ownername || "").toLowerCase().includes(text)
          || (p.bbl || "").includes(text);
      })
    : dedupedFeatures;

  const sorted = [...filtered].sort((a, b) => {
    const col = TABLE_COLS.find((c) => c.key === sortKey);
    const sigCount = (p) => [p.has_prior_op, p.has_reversion, p.has_tax_lien, p.has_lis_pendens, p.coo_has_temporary].filter(Boolean).length;
    let va = sortKey === "signals" ? sigCount(a.properties) : sortKey === "est_rooms" ? estRooms(a.properties).value : a.properties[sortKey];
    let vb = sortKey === "signals" ? sigCount(b.properties) : sortKey === "est_rooms" ? estRooms(b.properties).value : b.properties[sortKey];
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
          <span className="truncate">{p.address || "—"}</span>
          {hasNote && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" title="Has note" />}
        </span>
      );
    }
    if (col.key === "signals") {
      const sigs = [
        p.has_prior_op && { label: "Prior op", color: "bg-purple-500" },
        p.has_reversion && { label: "Reversion", color: "bg-rose-500" },
        p.has_tax_lien && { label: "Lien", color: "bg-red-600" },
        p.has_lis_pendens && { label: "LP", color: "bg-red-600" },
        p.coo_has_temporary && { label: "Temp CO", color: "bg-amber-500" },
        p.is_landmark && { label: "LPC", color: "bg-amber-600" },
        p.historic_district && { label: "Hist. dist.", color: "bg-amber-500" },
      ].filter(Boolean);
      if (sigs.length === 0) return "";
      return <span className="flex gap-0.5 flex-wrap">{sigs.map((s, i) => <span key={i} className={`${s.color} text-white text-[8px] font-bold px-1 py-0.5 rounded`}>{s.label}</span>)}</span>;
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
    if (col.key === "coo_has_temporary") return v ? "Yes" : "";
    if (col.key === "owner_portfolio_size") return v > 1 ? v : "";
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
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto px-8 pb-8">
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-gray-50 z-10">
            <tr>
              <th className="px-3 py-2.5 text-[10px] font-semibold text-gray-500 uppercase tracking-wide w-10">
                <span className="sr-only">Select</span>
              </th>
              {TABLE_COLS.map((col) => (
                <th
                  key={col.key}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
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
        hasReversion: false,
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
    if (p.has_reversion) g.hasReversion = true;
    if (p.coo_has_temporary) g.hasTempCoo = true;
    if (p.last_sale_date && (!g.latestSaleDate || p.last_sale_date > g.latestSaleDate)) {
      g.latestSaleDate = p.last_sale_date;
    }
  }
  return Array.from(groups.values());
}

const SCORE_SIGNALS = {
  legal: [
    { key: "tier_legal", label: "Legal transient tier", pts: 30, max: 50, desc: "Multiple sources confirm existing transient/hotel capacity." },
    { key: "tier_classb", label: "Class B (split-use) tier", pts: 20, max: 50, desc: "HPD shows both Class A apartments and Class B transient rooms." },
    { key: "tier_partial", label: "Partial signal tier", pts: 8, max: 50, desc: "Building class suggests mixed use but HPD didn't confirm Class B." },
    { key: "coo_temp", label: "Temporary C of O", pts: 10, max: 50, desc: "Independent confirmation of transient use via Certificate of Occupancy." },
    { key: "hpd_classb", label: "HPD Class B rooms", pts: 10, max: 50, desc: "HPD records show Class B (transient) room count > 0." },
  ],
  avail: [
    { key: "prior_op", label: "Prior operator departed", pts: 15, max: 45, desc: "A flex-stay operator previously ran this building. Proven model, gap to fill." },
    { key: "tax_lien", label: "Tax lien", pts: 8, max: 45, desc: "City lien for unpaid taxes. Owner under financial pressure." },
    { key: "lis_pendens", label: "Lis pendens / judgment", pts: 8, max: 45, desc: "Legal action filed against the property in last 5 years." },
    { key: "recent_sale", label: "Recent sale (last 2 yrs)", pts: 5, max: 45, desc: "New owner may be more open to management partnerships." },
    { key: "reversion", label: "Reversion window", pts: 5, max: 45, desc: "Hotel-converted building can revert to transient before Dec 2027." },
    { key: "ecb_balance", label: "ECB balance > $10K", pts: 4, max: 45, desc: "Unpaid DOB fines indicate financial pressure on the owner." },
  ],
  quality: [
    { key: "not_hotel", label: "Not already a hotel", pts: 7, max: 15, desc: "Building isn't H-class, so it's not already operating as a branded hotel." },
    { key: "low_violations", label: "Low Class C violations", pts: 5, max: 15, desc: "Fewer than 10 immediately hazardous violations. Building in decent shape." },
    { key: "portfolio_owner", label: "Multi-building owner", pts: 3, max: 15, desc: "Owner has multiple buildings in pipeline. Portfolio deal potential." },
  ],
};

function ScoreConfigPage({ scoreWeights, setScoreWeights, features }) {
  const topBuildings = useMemo(() => {
    const scored = features.map((f) => ({
      feature: f,
      score: computeScore(f.properties, scoreWeights),
    }));
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 10);
  }, [features, scoreWeights]);

  const categoryMeta = [
    { key: "legal", label: "Legal certainty", color: "#16a34a", weight: scoreWeights.legal },
    { key: "avail", label: "Availability", color: "#2563eb", weight: scoreWeights.avail },
    { key: "quality", label: "Building quality", color: "#8b5cf6", weight: scoreWeights.quality },
  ];

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="max-w-3xl mx-auto py-8 px-6 space-y-8">
        <div>
          <h2 className="text-lg font-bold text-gray-900">Deal score configuration</h2>
          <p className="text-sm text-gray-500 mt-1">
            Adjust how buildings are ranked. Each category has a weight (percentage of total score)
            and individual signals that contribute to it.
          </p>
        </div>

        {/* Category weight sliders */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
          <h3 className="text-sm font-semibold text-gray-800">Category weights</h3>
          <p className="text-xs text-gray-400">These control how much each dimension matters in the final score. They don't need to add up to 100 — they're normalized automatically.</p>
          {categoryMeta.map(({ key, label, color, weight }) => (
            <div key={key}>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: color }} />
                  <span className="text-sm text-gray-700">{label}</span>
                </div>
                <span className="text-sm font-mono text-gray-500 tabular-nums">{weight}%</span>
              </div>
              <input
                type="range"
                min={0}
                max={100}
                value={weight}
                onChange={(e) => setScoreWeights((prev) => ({ ...prev, [key]: Number(e.target.value) }))}
                className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer"
                style={{ accentColor: color }}
              />
            </div>
          ))}
          <button
            onClick={() => setScoreWeights({ legal: 50, avail: 35, quality: 15 })}
            className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer"
          >
            Reset to defaults (50 / 35 / 15)
          </button>
        </div>

        {/* Signal breakdown by category */}
        {categoryMeta.map(({ key, label, color }) => (
          <div key={key} className="bg-white rounded-xl border border-gray-200 p-5">
            <div className="flex items-center gap-2 mb-3">
              <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: color }} />
              <h3 className="text-sm font-semibold text-gray-800">{label}</h3>
            </div>
            <div className="space-y-3">
              {SCORE_SIGNALS[key].map((sig) => (
                <div key={sig.key} className="flex items-start gap-3">
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <div className="w-14 shrink-0">
                      <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                        <div className="h-full rounded-full" style={{ width: `${(sig.pts / sig.max) * 100}%`, backgroundColor: color }} />
                      </div>
                    </div>
                    <div className="min-w-0">
                      <div className="text-xs text-gray-800 font-medium">{sig.label}</div>
                      <div className="text-[10px] text-gray-400 leading-snug">{sig.desc}</div>
                    </div>
                  </div>
                  <span className="text-[11px] font-mono text-gray-400 tabular-nums shrink-0 pt-0.5">+{sig.pts}</span>
                </div>
              ))}
            </div>
          </div>
        ))}

        {/* Live preview: top 10 */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h3 className="text-sm font-semibold text-gray-800 mb-3">Top 10 with current weights</h3>
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
                    <div className="text-[10px] text-gray-400">
                      {p.neighborhood || ""}
                      {p.has_prior_op && <span className="ml-1.5 text-purple-500">Prior op</span>}
                      {p.has_tax_lien && <span className="ml-1.5 text-red-500">Lien</span>}
                    </div>
                  </div>
                  <div className="flex gap-1 shrink-0">
                    {[
                      { val: p.score_legal, color: "#16a34a" },
                      { val: p.score_avail, color: "#2563eb" },
                      { val: p.score_quality, color: "#8b5cf6" },
                    ].map(({ val, color }, j) => (
                      <div key={j} className="w-8 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                        <div className="h-full rounded-full" style={{ width: `${val}%`, backgroundColor: color }} />
                      </div>
                    ))}
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
      const rank = { reversion: 0, split_use: 1, pure_hotel: 2, partial: 3, unknown: 4 };
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
    const rank = ["reversion", "split_use", "pure_hotel", "partial", "unknown"];
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
                        {owner.hasReversion && <span className="px-1.5 py-0.5 text-[9px] font-semibold rounded bg-rose-100 text-rose-700">Reversion</span>}
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

export default function App() {
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const allFeaturesRef = useRef([]);
  const [inspectedFeature, setInspectedFeature] = useState(null); // single click detail
  const { notes, save: saveNote } = useNotes();
  const { statuses: crmStatuses, setStatus: setCrmStatus } = useCrmStatuses();
  const [exportList, setExportList] = useState(new Map()); // bbl -> feature
  const [listExpanded, setListExpanded] = useState(false);
  const [activeSegments, setActiveSegments] = useState(
    Object.fromEntries(SEGMENTS.map((s) => [s.key, s.defaultOn]))
  );
  const [showPriorOps, setShowPriorOps] = useState(true);
  const [distressOnly, setDistressOnly] = useState(false);
  const [noOperatorOnly, setNoOperatorOnly] = useState(false);
  const [hideBranded, setHideBranded] = useState(true);
  const [minUnits, setMinUnits] = useState(0);
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
  const [activeView, setActiveView] = useState("map"); // "map" | "table"
  const [scoreWeights, setScoreWeights] = useState({ legal: 50, avail: 35, quality: 15 });
  const [featureCount, setFeatureCount] = useState(0);
  const [overlayCounts, setOverlayCounts] = useState({ priorOps: 0, reversion: 0, tempCoo: 0 });
  const [dataDate, setDataDate] = useState(null);

  // Load GeoJSON for overlay counts + bulk select
  useEffect(() => {
    fetch("/buildings.geojson")
      .then((r) => r.json())
      .then((data) => {
        const feats = data.features || [];
        allFeaturesRef.current = feats;
        setOverlayCounts({
          priorOps: feats.filter((f) => f.properties.has_prior_op).length,
          reversion: feats.filter((f) => f.properties.has_reversion).length,
          tempCoo: feats.filter((f) => f.properties.coo_has_temporary).length,
        });
        // Extract data date from first feature's source_pulled_on (YYYYMMDD)
        const raw = feats[0]?.properties?.source_pulled_on || "";
        if (raw.length === 8) {
          const d = new Date(`${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}`);
          setDataDate(d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" }));
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
      style: {
        version: 8,
        sources: {
          "carto-light": {
            type: "raster",
            tiles: [
              "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
              "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
            ],
            tileSize: 256,
            attribution: '&copy; <a href="https://carto.com">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
          },
        },
        layers: [{ id: "carto-light", type: "raster", source: "carto-light" }],
      },
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
          const SEG_RANK = { reversion: 0, split_use: 1, pure_hotel: 2, partial: 3, unknown: 4 };
          const pointMap = new Map();
          for (const f of (data.features || [])) {
            const coords = f.geometry?.coordinates;
            if (!coords) continue;
            const ring = f.geometry.type === "MultiPolygon" ? coords[0][0] : coords[0];
            let cx = 0, cy = 0;
            for (const [x, y] of ring) { cx += x; cy += y; }
            cx /= ring.length; cy /= ring.length;
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
            filter: initFilter,
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
        true, 0, {
        filterTempCoo: false, filterHasClassB: false, filterMultiOwner: false,
        filterRecentSale: false, filterCommercialZone: false,
      }, false, false, true);

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

      // Reversion window outline — dark warm tone that pairs with green/blue/amber fills
      map.addLayer({
        id: "reversion-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_reversion"], true],
        minzoom: CLUSTER_ZOOM_THRESHOLD,
        paint: {
          "line-color": "#b91c1c",
          "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.8, 16, 1.5, 18, 2],
          "line-opacity": 0.6,
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
        const features = map.queryRenderedFeatures({ layers: ["buildings-fill"] });
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

    const filter = buildFilter(activeSegments, showPriorOps, minUnits, extraFilters, distressOnly, noOperatorOnly, hideBranded);
    map.setFilter("buildings-fill", filter);
    map.setFilter("buildings-outline", filter);
    if (map.getLayer("buildings-dots")) map.setFilter("buildings-dots", filter);

    if (map.getLayer("reversion-outline")) map.setLayoutProperty("reversion-outline", "visibility", activeSegments.reversion ? "visible" : "none");
    if (map.getLayer("prior-op-outline")) map.setLayoutProperty("prior-op-outline", "visibility", showPriorOps ? "visible" : "none");

    setTimeout(() => {
      const features = map.queryRenderedFeatures({ layers: ["buildings-fill"] });
      const uniqueBBLs = new Set(features.map((f) => f.properties.bbl));
      setFeatureCount(uniqueBBLs.size);
    }, 100);
  }, [activeSegments, showPriorOps, minUnits, extraFilters, distressOnly, noOperatorOnly, hideBranded]);

  const tableFeatures = useMemo(() => {
    return applyFilters(allFeaturesRef.current, activeSegments, showPriorOps, minUnits, extraFilters, distressOnly, noOperatorOnly, hideBranded);
  }, [activeSegments, showPriorOps, minUnits, extraFilters, distressOnly, noOperatorOnly, hideBranded]);

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
          onClick={() => setActiveView("table")}
          className={`w-20 py-2 text-xs font-medium transition-colors cursor-pointer text-center ${
            activeView === "table" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Table
        </button>
      </div>}

      {/* Map view */}
      <div ref={mapContainer} className="w-full h-full" style={{ display: activeView === "map" ? "block" : "none" }} />

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
              scoreWeights={scoreWeights}
              activeSegments={activeSegments}
              setActiveSegments={setActiveSegments}
              distressOnly={distressOnly}
              setDistressOnly={setDistressOnly}
              minUnits={minUnits}
              setMinUnits={setMinUnits}
              showPriorOps={showPriorOps}
              setShowPriorOps={setShowPriorOps}
              notes={notes}
            />
          </div>
        </div>
      )}

      {activeView === "map" && <FilterPanel
        activeSegments={activeSegments}
        setActiveSegments={setActiveSegments}
        showPriorOps={showPriorOps}
        setShowPriorOps={setShowPriorOps}
        distressOnly={distressOnly}
        setDistressOnly={setDistressOnly}
        noOperatorOnly={noOperatorOnly}
        setNoOperatorOnly={setNoOperatorOnly}
        hideBranded={hideBranded}
        setHideBranded={setHideBranded}
        minUnits={minUnits}
        setMinUnits={setMinUnits}
        scoreWeights={scoreWeights}
        setScoreWeights={setScoreWeights}
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
        scoreWeights={scoreWeights}
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
        scoreWeights={scoreWeights}
      />

      {/* Legend removed — tier colors are in the filter panel */}
    </div>
  );
}
