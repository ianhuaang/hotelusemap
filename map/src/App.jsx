import { useEffect, useRef, useState, useCallback } from "react";
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

const TIER_RANK = Object.fromEntries(SIGNAL_TIERS.map((t) => [t.key, t.rank]));

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

const CONFIDENCE_LEVELS = ["high", "medium", "low"];
const OPACITY_BY_CONFIDENCE = { high: 0.85, medium: 0.55, low: 0.3 };

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

function buildFilter(tierThreshold, showPriorOps, showReversion, minUnits, minConfidence) {
  const confIdx = CONFIDENCE_LEVELS.indexOf(minConfidence);
  const allowedConf = CONFIDENCE_LEVELS.slice(0, confIdx + 1);

  const allowedTiers = SIGNAL_TIERS
    .filter((t) => t.rank <= tierThreshold)
    .map((t) => t.key);

  const tierFilter = ["in", ["get", "tier"], ["literal", allowedTiers]];
  const priorOpFilter = ["==", ["get", "has_prior_op"], true];
  const reversionFilter = ["==", ["get", "has_reversion"], true];

  // Build overlay conditions
  const overlayConditions = [];
  if (showPriorOps) overlayConditions.push(priorOpFilter);
  if (showReversion) overlayConditions.push(reversionFilter);

  const visibilityFilter = overlayConditions.length > 0
    ? ["any", tierFilter, ...overlayConditions]
    : tierFilter;

  const alwaysShowFilter = overlayConditions.length > 0
    ? ["any", ...overlayConditions]
    : ["literal", false];

  return [
    "all",
    visibilityFilter,
    ["any",
      [">=", ["get", "unitsres"], minUnits],
      alwaysShowFilter,
    ],
    ["any",
      ["in", ["get", "confidence"], ["literal", allowedConf]],
      alwaysShowFilter,
    ],
  ];
}

function DetailPanel({ feature, onClose }) {
  if (!feature) return null;
  const p = feature.properties;
  const reasonCodes = typeof p.reason_codes === "string" ? JSON.parse(p.reason_codes) : p.reason_codes || [];
  const blockers = typeof p.blockers === "string" ? JSON.parse(p.blockers) : p.blockers || [];
  const priorOp = typeof p.prior_operator === "string" ? JSON.parse(p.prior_operator) : p.prior_operator;
  const reversion = typeof p.reversion_window === "string" ? JSON.parse(p.reversion_window) : p.reversion_window;

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
          <span className="text-xs text-gray-500">
            {p.confidence} confidence
          </span>
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
                <span key={code} className="bg-gray-100 text-gray-700 text-xs px-2 py-0.5 rounded">
                  {code}
                </span>
              ))}
            </div>
          </div>
        )}

        {blockers.length > 0 && (
          <div>
            <div className="text-xs font-semibold text-red-500 uppercase tracking-wide mb-1">Blockers</div>
            <div className="flex flex-wrap gap-1">
              {blockers.map((b) => (
                <span key={b} className="bg-red-50 text-red-700 text-xs px-2 py-0.5 rounded">
                  {b}
                </span>
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

        {p.ownername && (
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Owner</div>
            <div className="text-sm text-gray-700">{p.ownername}</div>
          </div>
        )}

        <div className="text-xs text-gray-400 space-y-0.5 pt-2 border-t border-gray-100">
          <div>BBL: {p.bbl}</div>
          <div>BIN: {p.bin}</div>
          <div>Pulled: {p.source_pulled_on}</div>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-lg p-2.5 text-xs text-amber-700">
          LL18 data pending — prohibited buildings list not yet integrated
        </div>
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

function FilterPanel({ tierThreshold, setTierThreshold, showPriorOps, setShowPriorOps, showReversion, setShowReversion, minUnits, setMinUnits, minConfidence, setMinConfidence, featureCount }) {
  const thresholdTier = SIGNAL_TIERS[tierThreshold];

  return (
    <div className="absolute top-4 left-4 w-72 bg-white/95 backdrop-blur rounded-xl shadow-xl border border-gray-200 z-20">
      <div className="p-4 border-b border-gray-100">
        <h1 className="text-sm font-bold text-gray-900 tracking-tight">NYC Transient Capacity</h1>
        <p className="text-[11px] text-gray-500 mt-0.5">Manhattan below 96th St &middot; v1 cheap signals</p>
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

        {/* Prior operator overlay toggle */}
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
              <span className="text-[10px] text-gray-400 ml-1">overlay</span>
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
              <span className="text-[10px] text-gray-400 ml-1">overlay</span>
              <InfoTip text={REVERSION.info} />
            </div>
          </label>
        </div>

        {/* Unit count slider */}
        <div>
          <div className="flex justify-between items-baseline mb-1">
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Min units</span>
            <span className="text-xs font-mono text-gray-700">{minUnits}</span>
          </div>
          <input
            type="range"
            min={0}
            max={200}
            value={minUnits}
            onChange={(e) => setMinUnits(Number(e.target.value))}
            className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-gray-700"
          />
        </div>

        {/* Confidence floor */}
        <div>
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">Min confidence</div>
          <div className="flex gap-1">
            {CONFIDENCE_LEVELS.map((level) => (
              <button
                key={level}
                onClick={() => setMinConfidence(level)}
                className={`flex-1 text-xs py-1 rounded cursor-pointer transition-colors ${
                  minConfidence === level
                    ? "bg-gray-800 text-white"
                    : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                }`}
              >
                {level}
              </button>
            ))}
          </div>
        </div>

        <div className="text-xs text-gray-400 pt-2 border-t border-gray-100">
          {featureCount.toLocaleString()} buildings shown
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

export default function App() {
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const [selectedFeature, setSelectedFeature] = useState(null);
  const [tierThreshold, setTierThreshold] = useState(1); // Default: show legal_transient + class_b
  const [showPriorOps, setShowPriorOps] = useState(true);
  const [showReversion, setShowReversion] = useState(true);
  const [minUnits, setMinUnits] = useState(0);
  const [minConfidence, setMinConfidence] = useState("low");
  const [featureCount, setFeatureCount] = useState(0);

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
      center: [-73.985, 40.748],
      zoom: 13,
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

      map.addLayer({
        id: "buildings-fill",
        type: "fill",
        source: "buildings",
        paint: {
          "fill-color": buildColorExpr(),
          "fill-opacity": buildOpacityExpr(),
        },
      });

      map.addLayer({
        id: "buildings-outline",
        type: "line",
        source: "buildings",
        paint: {
          "line-color": buildColorExpr(),
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            13, 0.3,
            16, 1,
            18, 2,
          ],
          "line-opacity": 0.6,
        },
      });

      // Reversion window highlight outline — thick rose border on qualifying buildings
      map.addLayer({
        id: "reversion-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_reversion"], true],
        paint: {
          "line-color": REVERSION.color,
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            13, 2.5,
            16, 4,
            18, 6,
          ],
          "line-opacity": 0.9,
        },
      });

      // Prior operator highlight outline — purple border
      map.addLayer({
        id: "prior-op-outline",
        type: "line",
        source: "buildings",
        filter: ["==", ["get", "has_prior_op"], true],
        paint: {
          "line-color": PRIOR_OP.color,
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            13, 2.5,
            16, 4,
            18, 6,
          ],
          "line-opacity": 0.9,
        },
      });

      // Point markers for overlays — visible at low zoom where footprints are tiny
      map.addSource("buildings-points", {
        type: "geojson",
        data: "/buildings.geojson",
        promoteId: "bbl",
      });

      map.addLayer({
        id: "prior-op-circle",
        type: "circle",
        source: "buildings-points",
        filter: ["==", ["get", "has_prior_op"], true],
        paint: {
          "circle-color": PRIOR_OP.color,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 4, 14, 7, 17, 3],
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1.5,
          "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0.9, 17, 0],
        },
      });

      map.addLayer({
        id: "reversion-circle",
        type: "circle",
        source: "buildings-points",
        filter: ["==", ["get", "has_reversion"], true],
        paint: {
          "circle-color": REVERSION.color,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3, 14, 5, 17, 2],
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1,
          "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0.8, 17, 0],
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
          setSelectedFeature(e.features[0]);
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

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("buildings-fill")) return;

    const filter = buildFilter(tierThreshold, showPriorOps, showReversion, minUnits, minConfidence);
    map.setFilter("buildings-fill", filter);
    map.setFilter("buildings-outline", filter);

    // Toggle overlay layers (outlines + circle markers)
    for (const id of ["reversion-outline", "reversion-circle"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", showReversion ? "visible" : "none");
    }
    for (const id of ["prior-op-outline", "prior-op-circle"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", showPriorOps ? "visible" : "none");
    }

    setTimeout(() => {
      const features = map.queryRenderedFeatures({ layers: ["buildings-fill"] });
      const uniqueBBLs = new Set(features.map((f) => f.properties.bbl));
      setFeatureCount(uniqueBBLs.size);
    }, 100);
  }, [tierThreshold, showPriorOps, showReversion, minUnits, minConfidence]);

  return (
    <div className="relative w-full h-full">
      <div ref={mapContainer} className="w-full h-full" />

      <FilterPanel
        tierThreshold={tierThreshold}
        setTierThreshold={setTierThreshold}
        showPriorOps={showPriorOps}
        setShowPriorOps={setShowPriorOps}
        showReversion={showReversion}
        setShowReversion={setShowReversion}
        minUnits={minUnits}
        setMinUnits={setMinUnits}
        minConfidence={minConfidence}
        setMinConfidence={setMinConfidence}
        featureCount={featureCount}
      />

      <SearchBar mapRef={mapRef} />

      <DetailPanel
        feature={selectedFeature}
        onClose={() => setSelectedFeature(null)}
      />

      <Legend />
    </div>
  );
}
