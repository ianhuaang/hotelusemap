import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import maplibregl from "maplibre-gl";

// Ordered best → weakest. Prior operator is an overlay, not part of the hierarchy.
const SIGNAL_TIERS = [
  {
    key: "legal_transient", label: "Legal transient", color: "#16a34a", rank: 0,
    info: "Multiple sources confirm existing transient/hotel capacity. Hotel building class in PLUTO and HPD Class B rooms, or pure Class B with no residential apartments.",
  },
  {
    key: "class_b", label: "Class B (split-use)", color: "#2563eb", rank: 1,
    info: "HPD shows both Class A apartments and Class B transient rooms in the same building. Real transient capacity exists, but mixed with residential. C of O data (v2) would clarify which floors.",
  },
  {
    key: "partial", label: "Partial signal", color: "#f59e0b", rank: 2,
    info: "Building class suggests mixed use (RM, RC, etc.) but HPD didn't confirm Class B rooms. May have transient capacity, or may just be commercial/residential. Needs manual verification.",
  },
];

const PRIOR_OP = {
  key: "prior_operator", label: "Prior operator", color: "#a855f7",
  info: "Buildings previously operated by flex-stay companies (Sonder, Placemakr, Kasa, Mint House, etc.). Shows operational viability but does not confirm legal transient capacity.",
};

const REVERSION = {
  key: "reversion_window", label: "Reversion window", color: "#e11d48",
  info: "Hotel-class buildings that converted to residential. Under the Citywide Hotel Text Amendment (Dec 2021), they can revert to transient use without a special permit before December 9, 2027.",
};

const ALL_TIERS = [...SIGNAL_TIERS, PRIOR_OP, REVERSION];
const TIER_COLORS = Object.fromEntries(ALL_TIERS.map((t) => [t.key, t.color]));

function tierColor(tier) {
  return TIER_COLORS[tier] || "#94a3b8";
}

function buildColorExpr() {
  const stops = [];
  for (const [tier, color] of Object.entries(TIER_COLORS)) {
    stops.push(tier, color);
  }
  return ["match", ["get", "tier"], ...stops, "#94a3b8"];
}

function buildOpacityExpr() {
  return [
    "match",
    ["get", "confidence"],
    "high", 0.85,
    "medium", 0.55,
    "low", 0.3,
    0.3,
  ];
}

function buildFilter(tierThreshold, showPriorOps, showReversion, minUnits, filters) {
  const allowedTiers = SIGNAL_TIERS
    .filter((t) => t.rank <= tierThreshold)
    .map((t) => t.key);

  const tierFilter = ["in", ["get", "tier"], ["literal", allowedTiers]];
  const priorOpFilter = ["==", ["get", "has_prior_op"], true];
  const reversionFilter = ["==", ["get", "has_reversion"], true];

  const overlayConditions = [];
  if (showPriorOps) overlayConditions.push(priorOpFilter);
  if (showReversion) overlayConditions.push(reversionFilter);

  const visibilityFilter = overlayConditions.length > 0
    ? ["any", tierFilter, ...overlayConditions]
    : tierFilter;

  const alwaysShowFilter = overlayConditions.length > 0
    ? ["any", ...overlayConditions]
    : ["literal", false];

  const conditions = [
    visibilityFilter,
    ["any",
      [">=", ["get", "unitsres"], minUnits],
      alwaysShowFilter,
    ],
  ];

  if (filters.filterTempCoo) {
    conditions.push(["==", ["get", "coo_has_temporary"], true]);
  }
  if (filters.filterHasClassB) {
    conditions.push([">", ["get", "hpd_class_b"], 0]);
  }
  if (filters.filterMultiOwner) {
    conditions.push([">", ["get", "owner_portfolio_size"], 1]);
  }
  if (filters.filterRecentSale) {
    conditions.push([">=", ["get", "last_sale_date"], filters._recentSaleCutoff]);
  }
  if (filters.filterCommercialZone) {
    conditions.push(["any",
      ["==", ["slice", ["get", "zonedist1"], 0, 1], "C"],
      ["==", ["slice", ["get", "zonedist1"], 0, 1], "M"],
    ]);
  }

  return ["all", ...conditions];
}

// --- CSV export ---
const CSV_COLUMNS = [
  { key: "address", label: "Address" },
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
  { key: "height_roof", label: "Roof Height (ft)" },
  { key: "bin", label: "BIN" },
  { key: "prior_operator_name", label: "Prior Operator" },
  { key: "reversion_deadline", label: "Reversion Deadline" },
  { key: "last_sale_date", label: "Last Sale Date" },
  { key: "last_sale_price", label: "Last Sale Price" },
  { key: "permit_count", label: "DOB Permits (3yr)" },
  { key: "owner_portfolio_size", label: "Owner Portfolio Size" },
  { key: "coo_count", label: "C of O Records" },
  { key: "coo_latest_type", label: "Latest C of O Type" },
  { key: "coo_dwelling_units", label: "C of O Dwelling Units" },
  { key: "reason_codes", label: "Reason Codes" },
];

function parseJsonProp(val) {
  if (typeof val === "string") {
    try { return JSON.parse(val); } catch { return val; }
  }
  return val;
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
    const reversion = parseJsonProp(p.reversion_window);
    const reasons = parseJsonProp(p.reason_codes) || [];

    const row = {
      ...p,
      numfloors: p.numfloors ? Math.round(p.numfloors) : "",
      height_roof: p.height_roof ? Math.round(p.height_roof) : "",
      prior_operator_name: priorOp?.name || "",
      reversion_deadline: reversion?.deadline || "",
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

function LL18Modal({ onClose, address }) {
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl max-w-lg w-full max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between p-5 border-b border-gray-100">
          <h2 className="text-base font-bold text-gray-900">LL18 Prohibited Buildings Check</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none cursor-pointer">&times;</button>
        </div>
        <div className="p-5 space-y-4 text-sm text-gray-700">
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">What is Local Law 18?</div>
            <p>
              Local Law 18 (2022) requires all short-term rental hosts in NYC to register with the Mayor's Office of Special Enforcement (OSE).
              Buildings can opt onto the <strong>Prohibited Buildings List (PBL)</strong>, which blocks any short-term rental registrations for that address.
            </p>
          </div>

          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Why it matters for Kasa</div>
            <p>
              If a building is on the PBL, short-term stays under 30 days cannot be legally registered there, regardless of the building's transient capacity or zoning. Always check before outreach.
            </p>
          </div>

          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">How to check</div>
            <ol className="list-decimal list-inside space-y-1.5 text-[13px]">
              <li>Click the link below to open the OSE portal</li>
              <li>Enter the building's <strong>house number</strong> and <strong>street name</strong></li>
              <li>Select the <strong>borough</strong></li>
              <li>Click <strong>Search</strong></li>
            </ol>
          </div>

          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">What to look for</div>
            <div className="space-y-2">
              <div className="flex items-start gap-2">
                <span className="mt-0.5 w-4 h-4 rounded-full bg-red-100 text-red-600 text-[10px] font-bold flex items-center justify-center shrink-0">!</span>
                <span><strong>"Building is on the Prohibited Buildings List"</strong> — this building is a no-go for stays under 30 days</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="mt-0.5 w-4 h-4 rounded-full bg-green-100 text-green-600 text-[10px] font-bold flex items-center justify-center shrink-0">&#10003;</span>
                <span><strong>"Building is not on the Prohibited Buildings List"</strong> — short-term rental registration is possible</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="mt-0.5 w-4 h-4 rounded-full bg-gray-100 text-gray-500 text-[10px] font-bold flex items-center justify-center shrink-0">?</span>
                <span><strong>"No results found"</strong> — address format may not match. Try variations (e.g., "W 42 ST" vs "WEST 42ND STREET")</span>
              </div>
            </div>
          </div>

          <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-700">
            <strong>Note:</strong> Even if a building is not on the PBL, LL18 still requires individual host registration for stays under 30 days. Kasa's operator model may be structured differently — confirm with Legal.
          </div>

          <a
            href="https://strr-portal.ose.nyc.gov/s/searchbuildingsaddress"
            target="_blank"
            rel="noopener noreferrer"
            className="block w-full text-center px-4 py-2.5 bg-gray-800 text-white text-sm font-medium rounded-lg hover:bg-gray-700 transition-colors"
          >
            Open OSE Prohibited Buildings Portal &rarr;
          </a>
        </div>
      </div>
    </div>
  );
}

function DetailPanel({ feature, onClose, onAddToList, isInList }) {
  const [showLL18, setShowLL18] = useState(false);
  if (!feature) return null;
  const p = feature.properties;
  const reasonCodes = parseJsonProp(p.reason_codes) || [];
  const blockers = parseJsonProp(p.blockers) || [];
  const priorOp = parseJsonProp(p.prior_operator);
  const reversion = parseJsonProp(p.reversion_window);

  return (
    <div className="absolute top-4 right-4 w-96 max-h-[calc(100vh-2rem)] overflow-y-auto bg-white rounded-xl shadow-2xl border border-gray-200 z-20">
      <div className="flex items-center justify-between p-4 border-b border-gray-100">
        <h2 className="text-lg font-semibold text-gray-900 truncate pr-2">{p.address}</h2>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none cursor-pointer">&times;</button>
      </div>

      <div className="p-4 space-y-4">
        <div className="flex items-center gap-2">
          <span
            className="inline-block px-2.5 py-1 rounded-full text-xs font-semibold text-white"
            style={{ backgroundColor: tierColor(p.tier) }}
          >
            {p.tier?.replace(/_/g, " ")}
          </span>
          <span className="text-xs text-gray-500">{p.confidence} confidence</span>
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

        <div className="grid grid-cols-2 gap-3">
          <Stat label="Residential units" value={p.unitsres} />
          <Stat label="Total units" value={p.unitstotal} />
          <Stat label="Floors" value={p.numfloors ? Math.round(p.numfloors) : "—"} />
          <Stat label="Building class" value={p.bldgclass || "—"} />
          <Stat label="HPD Class A" value={p.hpd_class_a ?? "—"} />
          <Stat label="HPD Class B" value={p.hpd_class_b ?? "—"} />
          <Stat label="Zoning" value={p.zonedist1 || "—"} />
          <Stat label="Roof height" value={p.height_roof ? `${Math.round(p.height_roof)} ft` : "—"} />
        </div>

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

        {/* Owner + portfolio */}
        {p.ownername && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Owner</div>
            <div className="text-sm text-gray-700">{p.ownername}</div>
            {p.owner_portfolio_size > 1 && (
              <div className="mt-1 text-[10px] text-blue-600 font-medium">
                Owns {p.owner_portfolio_size} buildings in pipeline
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

        <div className="text-xs text-gray-400 space-y-0.5 pt-2 border-t border-gray-100">
          <div>BBL: {p.bbl}</div>
          <div>BIN: {p.bin}</div>
          <div>Pulled: {p.source_pulled_on}</div>
        </div>

        <button
          onClick={() => setShowLL18(true)}
          className="w-full bg-amber-50 border border-amber-200 rounded-lg p-2.5 text-xs text-amber-700 text-left hover:bg-amber-100 cursor-pointer transition-colors"
        >
          <span className="font-semibold">LL18 check required</span> — tap to learn how to verify this building &rarr;
        </button>

        {showLL18 && <LL18Modal onClose={() => setShowLL18(false)} address={p.address} />}
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
              <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: tierColor(p.tier) }} />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-gray-900 truncate">{p.address}</div>
                <div className="text-[10px] text-gray-400">
                  {p.unitsres} units &middot; {p.bldgclass || "—"}
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

function FilterPanel({
  tierThreshold, setTierThreshold,
  showPriorOps, setShowPriorOps,
  showReversion, setShowReversion,
  minUnits, setMinUnits,
  featureCount, overlayCounts,
  onAddAllVisible, onAddCategory,
}) {
  return (
    <div className="absolute top-4 left-4 w-72 bg-white/95 backdrop-blur rounded-xl shadow-xl border border-gray-200 z-20">
      <div className="p-4 border-b border-gray-100">
        <h1 className="text-sm font-bold text-gray-900 tracking-tight">NYC Transient Capacity</h1>
        <p className="text-[11px] text-gray-500 mt-0.5">Manhattan &middot; Downtown BK &middot; Williamsburg &middot; LIC &middot; v1</p>
      </div>

      <div className="p-4 space-y-4">
        {/* Signal threshold */}
        <div>
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Show buildings down to</div>
          <div className="space-y-1">
            {SIGNAL_TIERS.map((tier, idx) => {
              const active = idx <= tierThreshold;
              return (
                <button
                  key={tier.key}
                  onClick={() => setTierThreshold(idx)}
                  className="w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg transition-colors cursor-pointer text-left"
                  style={{
                    backgroundColor: active ? `${tier.color}10` : "transparent",
                    borderLeft: `3px solid ${active ? tier.color : "transparent"}`,
                  }}
                >
                  <span
                    className="w-3 h-3 rounded-sm shrink-0"
                    style={{ backgroundColor: active ? tier.color : "#d1d5db" }}
                  />
                  <span className={`text-xs ${active ? "text-gray-900 font-medium" : "text-gray-400"}`}>
                    {tier.label}
                  </span>
                  <InfoTip text={tier.info} />
                  {idx === tierThreshold && (
                    <span className="ml-auto text-[10px] text-gray-400">threshold</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {/* Overlay toggles */}
        <div>
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
                borderColor: PRIOR_OP.color,
                backgroundColor: showPriorOps ? PRIOR_OP.color : "transparent",
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
              <InfoTip text={PRIOR_OP.info} />
            </div>
          </label>

          <label className="flex items-center gap-2.5 cursor-pointer px-2.5 mt-1.5">
            <input
              type="checkbox"
              checked={showReversion}
              onChange={(e) => setShowReversion(e.target.checked)}
              className="sr-only"
            />
            <span
              className="w-3.5 h-3.5 rounded-sm border-2 flex items-center justify-center transition-colors"
              style={{
                borderColor: REVERSION.color,
                backgroundColor: showReversion ? REVERSION.color : "transparent",
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
              <span className="text-[10px] text-gray-400 ml-1">({overlayCounts.reversion})</span>
              <InfoTip text={REVERSION.info} />
            </div>
          </label>
        </div>

        {/* Min units */}
        <div className="flex items-center justify-between px-2.5">
          <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Min units</span>
          <input
            type="number"
            min={0}
            value={minUnits}
            onChange={(e) => setMinUnits(Math.max(0, Number(e.target.value) || 0))}
            className="w-16 px-2 py-1 text-xs font-mono text-gray-700 bg-gray-50 border border-gray-200 rounded-md text-right outline-none focus:ring-2 focus:ring-gray-300"
          />
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

function SearchBar({ mapRef }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef(null);

  const search = useCallback(async (text) => {
    if (text.length < 3) {
      setResults([]);
      return;
    }
    setLoading(true);
    try {
      const resp = await fetch(
        `https://geosearch.planninglabs.nyc/v2/search?text=${encodeURIComponent(text)}`
      );
      const data = await resp.json();
      setResults((data.features || []).slice(0, 5));
    } catch {
      setResults([]);
    }
    setLoading(false);
  }, []);

  const handleInput = (e) => {
    const val = e.target.value;
    setQuery(val);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(val), 300);
  };

  const flyTo = (feature) => {
    const [lng, lat] = feature.geometry.coordinates;
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 17, duration: 1200 });
    setQuery(feature.properties.label);
    setResults([]);
  };

  return (
    <div className="absolute top-4 left-[calc(50%-10rem)] w-80 z-20">
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={handleInput}
          placeholder="Search address..."
          className="w-full px-4 py-2.5 bg-white/95 backdrop-blur rounded-lg shadow-lg border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:ring-2 focus:ring-gray-300"
        />
        {loading && (
          <div className="absolute right-3 top-3 w-4 h-4 border-2 border-gray-300 border-t-gray-600 rounded-full animate-spin" />
        )}
      </div>
      {results.length > 0 && (
        <div className="mt-1 bg-white rounded-lg shadow-xl border border-gray-200 overflow-hidden">
          {results.map((r, i) => (
            <button
              key={i}
              onClick={() => flyTo(r)}
              className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 border-b border-gray-50 last:border-0 cursor-pointer"
            >
              {r.properties.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="absolute bottom-6 left-4 bg-white/90 backdrop-blur rounded-lg shadow-lg border border-gray-200 px-3 py-2 z-10">
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
  { key: "tier", label: "Tier", sortable: true, width: "min-w-[120px]" },
  { key: "unitsres", label: "Units", sortable: true, numeric: true, width: "min-w-[70px]" },
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

function applyFilters(features, tierThreshold, showPriorOps, showReversion, minUnits, filters) {
  const allowedTiers = SIGNAL_TIERS
    .filter((t) => t.rank <= tierThreshold)
    .map((t) => t.key);

  return features.filter((f) => {
    const p = f.properties;
    const tierOk = allowedTiers.includes(p.tier);
    const overlayOk = (showPriorOps && p.has_prior_op) || (showReversion && p.has_reversion);
    if (!tierOk && !overlayOk) return false;

    if (!overlayOk && (p.unitsres || 0) < minUnits) return false;

    if (filters.filterTempCoo && !p.coo_has_temporary) return false;
    if (filters.filterHasClassB && !(p.hpd_class_b > 0)) return false;
    if (filters.filterMultiOwner && !(p.owner_portfolio_size > 1)) return false;
    if (filters.filterRecentSale && (!p.last_sale_date || p.last_sale_date < filters._recentSaleCutoff)) return false;
    if (filters.filterCommercialZone) {
      const z = (p.zonedist1 || "")[0];
      if (z !== "C" && z !== "M") return false;
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
      const TIER_RANK = { legal_transient: 0, class_b: 1, partial: 2, prior_operator: 3, unknown: 4, excluded: 5 };
      if ((TIER_RANK[p.tier] ?? 99) < (TIER_RANK[existing.tier] ?? 99)) {
        groups.set(key, f);
      }
    }
  }
  return Array.from(groups.values());
}

function TableView({ features, onSelectFeature, exportList, onAddToList, extraFilters, setFilter }) {
  const [sortKey, setSortKey] = useState("unitsres");
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
    let va = a.properties[sortKey];
    let vb = b.properties[sortKey];
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
    const v = p[col.key];
    if (col.key === "tier") {
      return (
        <span
          className="inline-block px-2 py-0.5 rounded-full text-[10px] font-semibold text-white"
          style={{ backgroundColor: tierColor(p.tier) }}
        >
          {(p.tier || "").replace(/_/g, " ")}
        </span>
      );
    }
    if (col.key === "last_sale_price") return fmtPrice(v);
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
    <div className="h-full flex flex-col bg-white">
      {/* Search + filters bar */}
      <div className="px-4 py-3 border-b border-gray-200 shrink-0 space-y-2">
        <div className="flex items-center gap-3">
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="Search address, owner, or BBL..."
            className="flex-1 px-3 py-2 bg-gray-50 rounded-lg border border-gray-200 text-sm text-gray-900 placeholder-gray-400 outline-none focus:ring-2 focus:ring-gray-300"
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
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
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
  );
}

export default function App() {
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const allFeaturesRef = useRef([]);
  const [inspectedFeature, setInspectedFeature] = useState(null); // single click detail
  const [exportList, setExportList] = useState(new Map()); // bbl -> feature
  const [listExpanded, setListExpanded] = useState(false);
  const [tierThreshold, setTierThreshold] = useState(1);
  const [showPriorOps, setShowPriorOps] = useState(true);
  const [showReversion, setShowReversion] = useState(true);
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
  const [featureCount, setFeatureCount] = useState(0);
  const [overlayCounts, setOverlayCounts] = useState({ priorOps: 0, reversion: 0, tempCoo: 0 });

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
      center: [-73.97, 40.72],
      zoom: 12,
      minZoom: 11,
      maxZoom: 19,
    });

    map.addControl(new maplibregl.NavigationControl(), "bottom-right");

    map.on("load", () => {
      map.addSource("buildings", {
        type: "geojson",
        data: "/buildings.geojson",
        promoteId: "bbl",
      });

      const initFilter = buildFilter(1, true, true, 0, {
        filterTempCoo: false, filterHasClassB: false, filterMultiOwner: false,
        filterRecentSale: false, filterCommercialZone: false,
      });

      map.addLayer({
        id: "buildings-fill",
        type: "fill",
        source: "buildings",
        filter: initFilter,
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
        paint: {
          "line-color": buildColorExpr(),
          "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.3, 16, 1, 18, 2],
          "line-opacity": 0.6,
        },
      });

      // Selection highlight — black outline on buildings in export list
      map.addLayer({
        id: "selection-outline",
        type: "line",
        source: "buildings",
        paint: {
          "line-color": "#000",
          "line-width": ["interpolate", ["linear"], ["zoom"], 13, 2, 16, 3.5, 18, 5],
          "line-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 1, 0],
        },
      });

      // Reversion window highlight outline
      map.addLayer({
        id: "reversion-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_reversion"], true],
        paint: {
          "line-color": REVERSION.color,
          "line-width": ["interpolate", ["linear"], ["zoom"], 13, 2.5, 16, 4, 18, 6],
          "line-opacity": 0.9,
        },
      });

      // Prior operator highlight outline
      map.addLayer({
        id: "prior-op-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_prior_op"], true],
        paint: {
          "line-color": PRIOR_OP.color,
          "line-width": ["interpolate", ["linear"], ["zoom"], 13, 2.5, 16, 4, 18, 6],
          "line-opacity": 0.9,
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

    const filter = buildFilter(tierThreshold, showPriorOps, showReversion, minUnits, extraFilters);
    map.setFilter("buildings-fill", filter);
    map.setFilter("buildings-outline", filter);

    for (const id of ["reversion-outline"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", showReversion ? "visible" : "none");
    }
    for (const id of ["prior-op-outline"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", showPriorOps ? "visible" : "none");
    }

    setTimeout(() => {
      const features = map.queryRenderedFeatures({ layers: ["buildings-fill"] });
      const uniqueBBLs = new Set(features.map((f) => f.properties.bbl));
      setFeatureCount(uniqueBBLs.size);
    }, 100);
  }, [tierThreshold, showPriorOps, showReversion, minUnits, extraFilters]);

  // Compute filtered features for table view
  const tableFeatures = useMemo(() => {
    return applyFilters(allFeaturesRef.current, tierThreshold, showPriorOps, showReversion, minUnits, extraFilters);
  }, [tierThreshold, showPriorOps, showReversion, minUnits, extraFilters, overlayCounts]);

  return (
    <div className="relative w-full h-full flex flex-col">
      {/* View toggle */}
      <div className={`absolute top-4 z-30 flex bg-white rounded-lg shadow-lg border border-gray-200 overflow-hidden ${inspectedFeature ? "right-[26rem]" : "right-4"}`}>
        <button
          onClick={() => setActiveView("map")}
          className={`px-3.5 py-2 text-xs font-medium transition-colors cursor-pointer ${
            activeView === "map" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Map
        </button>
        <button
          onClick={() => setActiveView("table")}
          className={`px-3.5 py-2 text-xs font-medium transition-colors cursor-pointer ${
            activeView === "table" ? "bg-gray-800 text-white" : "text-gray-600 hover:bg-gray-50"
          }`}
        >
          Table
        </button>
      </div>

      {/* Map view */}
      <div ref={mapContainer} className="w-full h-full" style={{ display: activeView === "map" ? "block" : "none" }} />

      {/* Table view */}
      {activeView === "table" && (
        <div className="flex-1 flex">
          {/* Filter panel takes left side */}
          <div className="w-72 shrink-0" />
          {/* Table fills the rest */}
          <div className="flex-1 h-full overflow-hidden">
            <TableView
              features={tableFeatures}
              onSelectFeature={(f) => setInspectedFeature(f)}
              exportList={exportList}
              onAddToList={handleAddToList}
              extraFilters={extraFilters}
              setFilter={setFilter}
            />
          </div>
        </div>
      )}

      <FilterPanel
        tierThreshold={tierThreshold}
        setTierThreshold={setTierThreshold}
        showPriorOps={showPriorOps}
        setShowPriorOps={setShowPriorOps}
        showReversion={showReversion}
        setShowReversion={setShowReversion}
        minUnits={minUnits}
        setMinUnits={setMinUnits}
        featureCount={activeView === "table" ? tableFeatures.length : featureCount}
        overlayCounts={overlayCounts}
        onAddAllVisible={activeView === "table"
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
      />

      {activeView === "map" && <SearchBar mapRef={mapRef} />}

      <DetailPanel
        feature={inspectedFeature}
        onClose={() => setInspectedFeature(null)}
        onAddToList={handleAddToList}
        isInList={inspectedFeature ? exportList.has(inspectedFeature.properties.bbl) : false}
      />

      <ListTray
        list={exportList}
        onRemove={handleRemoveFromList}
        onClear={handleClearList}
        onExpand={() => setListExpanded(!listExpanded)}
        expanded={listExpanded}
      />

      {activeView === "map" && <Legend />}
    </div>
  );
}
