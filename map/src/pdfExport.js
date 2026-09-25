// PDF packet for whatever is on the export list.
//
// Page 1 is a map of the selected buildings — a second, offscreen maplibre
// instance framed on the selection rather than a screenshot of whatever the
// user happened to be looking at, so the packet frames the same thing every
// time. Pages 2..N are one sheet per building, drawn from the same
// propertyFacts builders the detail panel renders, so the paper and the screen
// cannot disagree.

import { jsPDF } from "jspdf";
import maplibregl from "maplibre-gl";
import {
  featureCentroid, estRooms, parseJsonProp, buildRecordLinks, distinctAddresses,
  segmentColor, segmentLabel, computeScore, buildScoreSignals, buildFeasibilityItems,
  buildConsiderations, buildDistressSignals, CRM_STATUSES,
} from "./propertyFacts";

const MAP_STYLE = "https://tiles.openfreemap.org/styles/positron";
const MAP_ATTRIBUTION = "Map data © OpenStreetMap contributors · Tiles by OpenFreeMap / OpenMapTiles";

// Letter, portrait, millimetres.
const PW = 215.9;
const PH = 279.4;
const M = 14;
const CW = PW - M * 2;

const INK = "#111827";
const MUTED = "#6b7280";
const FAINT = "#9ca3af";
const RULE = "#e5e7eb";

// jsPDF's built-in fonts encode WinAnsi, so anything outside it draws as
// mojibake. Map the few characters this data actually carries, keep the cp1252
// extras that do encode, and drop the rest rather than print rubbish.
const CP1252_EXTRAS = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ";
function sanitize(text) {
  return String(text ?? "")
    .replace(/[✓✔]/g, "")
    .replace(/[⚠▶◀]/g, "")
    .replace(/[→➔]/g, "->")
    .replace(/ /g, " ")
    .split("")
    .filter((ch) => ch.charCodeAt(0) < 0x100 || CP1252_EXTRAS.includes(ch))
    .join("")
    .trim();
}

const num = (v) => (v == null || v === "" ? null : Number(v).toLocaleString());
const money = (v) => (v == null || v === "" || Number(v) === 0 ? null : `$${Number(v).toLocaleString()}`);

function fmtDate(d) {
  if (!d) return null;
  const t = new Date(d);
  if (Number.isNaN(t.getTime())) return String(d);
  return t.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

const SEVERITY_COLOR = { high: "#ef4444", medium: "#f59e0b", low: "#9ca3af" };
const ICON_COLOR = { check: "#16a34a", warn: "#f59e0b", info: "#9ca3af" };

// --- Offscreen map capture ---

function once(map, event, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    map.once(event, finish);
    if (timeoutMs) setTimeout(finish, timeoutMs);
  });
}

// A pin rather than a dot: the tip marks the building, so a reader can tell
// which of two neighbouring lots a number belongs to.
function drawPin(ctx, x, y, label, color) {
  const r = 17;
  const cy = y - 25;
  ctx.save();
  ctx.shadowColor = "rgba(0,0,0,0.35)";
  ctx.shadowBlur = 5;
  ctx.shadowOffsetY = 1.5;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - 9, cy + 12);
  ctx.lineTo(x + 9, cy + 12);
  ctx.closePath();
  ctx.arc(x, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#ffffff";
  ctx.shadowColor = "transparent";
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.fillStyle = "#ffffff";
  ctx.font = `bold ${label.length > 2 ? 15 : 19}px "Helvetica Neue", Arial, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x, cy + 1);
  ctx.restore();
}

async function captureMapImage(points, widthPx, heightPx) {
  const container = document.createElement("div");
  container.setAttribute("aria-hidden", "true");
  container.style.cssText =
    `position:fixed;left:-20000px;top:0;width:${widthPx}px;height:${heightPx}px;pointer-events:none;`;
  document.body.appendChild(container);

  let map;
  try {
    map = new maplibregl.Map({
      container,
      style: MAP_STYLE,
      // The export is print, not screen: render at 2x regardless of the
      // monitor so the image lands near 300dpi in the page.
      pixelRatio: 2,
      // Without this the WebGL drawing buffer is cleared before we can read it.
      preserveDrawingBuffer: true,
      attributionControl: false,
      interactive: false,
      fadeDuration: 0,
      center: points[0].lngLat,
      zoom: 15,
    });

    await once(map, "load", 15000);

    const lngs = points.map((p) => p.lngLat[0]);
    const lats = points.map((p) => p.lngLat[1]);
    const spanLng = Math.max(...lngs) - Math.min(...lngs);
    const spanLat = Math.max(...lats) - Math.min(...lats);
    // fitBounds on one building, or on two in the same block, zooms to the
    // roof. Anything under ~90m across gets a fixed neighbourhood zoom instead.
    if (spanLng < 0.001 && spanLat < 0.0008) {
      map.jumpTo({
        center: [(Math.max(...lngs) + Math.min(...lngs)) / 2, (Math.max(...lats) + Math.min(...lats)) / 2],
        zoom: 15.5,
      });
    } else {
      map.fitBounds(
        [[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
        { padding: { top: 70, bottom: 45, left: 55, right: 55 }, animate: false, maxZoom: 16 },
      );
    }

    await once(map, "idle", 20000);
    map.redraw();

    const src = map.getCanvas();
    const out = document.createElement("canvas");
    out.width = src.width;
    out.height = src.height;
    const ctx = out.getContext("2d");
    ctx.drawImage(src, 0, 0);
    // Draw in CSS pixels; map.project() returns them.
    ctx.scale(src.width / widthPx, src.height / heightPx);
    // North to south, so where pins collide the nearer one sits in front —
    // the layering a reader expects, rather than whatever order the list was
    // built in.
    const byLatitude = [...points].sort((a, b) => b.lngLat[1] - a.lngLat[1]);
    for (const pt of byLatitude) {
      const { x, y } = map.project(pt.lngLat);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      drawPin(ctx, x, y, String(pt.n), pt.color);
    }
    return out.toDataURL("image/jpeg", 0.92);
  } finally {
    map?.remove();
    container.remove();
  }
}

// --- Page drawing ---

function makeDoc() {
  const doc = new jsPDF({ unit: "mm", format: "letter", orientation: "portrait", compress: true });
  doc.setFont("helvetica", "normal");
  return doc;
}

function numberedDisc(doc, x, y, r, label, color) {
  doc.setFillColor(color);
  doc.circle(x, y, r, "F");
  doc.setTextColor("#ffffff");
  doc.setFont("helvetica", "bold");
  // ~3.5pt of type per mm of radius fills the disc without touching its
  // edge; half a cap height below the centre puts the digit on the middle.
  doc.setFontSize(r * 3.5);
  doc.text(String(label), x, y + r * 0.42, { align: "center" });
}

function coverPage(doc, rows, mapImage, mapBox) {
  doc.setTextColor(INK);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(18);
  doc.text("NYC Transient Capacity", M, 20);

  const totalRooms = rows.reduce((sum, r) => sum + (r.rooms.value || 0), 0);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9.5);
  doc.setTextColor(MUTED);
  doc.text(
    sanitize(`${rows.length} propert${rows.length === 1 ? "y" : "ies"} · ${totalRooms.toLocaleString()} est. rooms · ${fmtDate(new Date())}`),
    M, 26.5,
  );

  if (mapImage) {
    doc.addImage(mapImage, "JPEG", mapBox.x, mapBox.y, mapBox.w, mapBox.h);
    doc.setDrawColor(RULE);
    doc.setLineWidth(0.3);
    doc.rect(mapBox.x, mapBox.y, mapBox.w, mapBox.h);
  } else {
    doc.setDrawColor(RULE);
    doc.rect(mapBox.x, mapBox.y, mapBox.w, mapBox.h);
    doc.setTextColor(FAINT);
    doc.setFontSize(9);
    doc.text("Map unavailable - basemap tiles could not be loaded.", mapBox.x + mapBox.w / 2, mapBox.y + mapBox.h / 2, { align: "center" });
  }

  doc.setFontSize(6.5);
  doc.setTextColor(FAINT);
  doc.text(sanitize(MAP_ATTRIBUTION), mapBox.x, mapBox.y + mapBox.h + 4.6);
}

// Two columns of numbered addresses under the map, spilling to its own page
// when the selection outruns the space.
function drawLegend(doc, rows, startY) {
  const colW = CW / 2;
  const rowH = 8.4;
  let y = startY;
  let i = 0;

  while (i < rows.length) {
    const perCol = Math.max(1, Math.floor((PH - 20 - y) / rowH));
    const chunk = rows.slice(i, i + perCol * 2);
    chunk.forEach((r, k) => {
      const x = M + (k < perCol ? 0 : colW);
      const ry = y + (k % perCol) * rowH;
      numberedDisc(doc, x + 3, ry, 3, r.n, r.color);
      doc.setFont("helvetica", "bold");
      doc.setFontSize(8.5);
      doc.setTextColor(INK);
      doc.text(doc.splitTextToSize(sanitize(r.title), colW - 12)[0], x + 8, ry - 0.6);
      doc.setFont("helvetica", "normal");
      doc.setFontSize(7.2);
      doc.setTextColor(MUTED);
      doc.text(doc.splitTextToSize(sanitize(r.subtitle), colW - 12)[0], x + 8, ry + 3);
    });
    i += chunk.length;
    if (i < rows.length) {
      doc.addPage();
      doc.setFont("helvetica", "bold");
      doc.setFontSize(11);
      doc.setTextColor(INK);
      doc.text("Selected properties (continued)", M, 18);
      y = 26;
    }
  }
}

// A cursor over the body of a sheet: everything below measures itself, asks
// for the room, and takes a new page when it does not fit.
function sheetCursor(doc, bottom = PH - 16) {
  const state = { y: M + 6 };
  return {
    get y() { return state.y; },
    set y(v) { state.y = v; },
    room(h) {
      if (state.y + h > bottom) {
        doc.addPage();
        state.y = M + 6;
        return true;
      }
      return false;
    },
  };
}

function sectionHeading(doc, cur, text, needs = 10) {
  cur.room(9.3 + needs);
  cur.y += 3.5;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(7.5);
  doc.setTextColor(FAINT);
  doc.text(sanitize(text).toUpperCase(), M, cur.y);
  cur.y += 1.8;
  doc.setDrawColor(RULE);
  doc.setLineWidth(0.25);
  doc.line(M, cur.y, PW - M, cur.y);
  cur.y += 4;
}

function bullet(doc, cur, text, dotColor, detail) {
  doc.setFont("helvetica", "normal");
  doc.setFontSize(8.5);
  const lines = doc.splitTextToSize(sanitize(text), CW - 6);
  cur.room(lines.length * 3.9 + 2);
  doc.setFillColor(dotColor);
  doc.circle(M + 1.3, cur.y - 1.1, 1.1, "F");
  doc.setTextColor(INK);
  doc.text(lines, M + 5, cur.y);
  cur.y += lines.length * 3.9;
  if (detail) {
    const dl = doc.splitTextToSize(sanitize(detail), CW - 6);
    cur.room(dl.length * 3.4 + 2);
    doc.setFontSize(7.5);
    doc.setTextColor(MUTED);
    doc.text(dl, M + 5, cur.y);
    cur.y += dl.length * 3.4;
  }
  cur.y += 1.4;
}

// Label over value, four to a row — the same shape as the panel's stat grid.
function statGrid(doc, cur, stats) {
  const cols = 4;
  const colW = CW / cols;
  const rowH = 13.5;
  const rows = Math.ceil(stats.length / cols);
  cur.room(rows * rowH + 2);
  stats.forEach((s, i) => {
    const x = M + (i % cols) * colW;
    const y = cur.y + Math.floor(i / cols) * rowH;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.5);
    doc.setTextColor(FAINT);
    doc.text(sanitize(s.label).toUpperCase(), x, y);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9.5);
    doc.setTextColor(INK);
    doc.text(doc.splitTextToSize(sanitize(String(s.value ?? "-")), colW - 3)[0], x, y + 4.4);
    if (s.note) {
      doc.setFont("helvetica", "normal");
      doc.setFontSize(6.2);
      doc.setTextColor(MUTED);
      doc.text(doc.splitTextToSize(sanitize(s.note), colW - 3)[0], x, y + 7.8);
    }
  });
  cur.y += rows * rowH;
}

// Key on the left, value wrapped on the right. Rows whose value is empty are
// dropped by the caller, so an absent record leaves no blank line behind.
function keyValues(doc, cur, pairs) {
  const keyW = 42;
  for (const [k, v] of pairs) {
    const lines = doc.splitTextToSize(sanitize(v), CW - keyW);
    cur.room(lines.length * 3.9 + 1.5);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8);
    doc.setTextColor(MUTED);
    doc.text(sanitize(k), M, cur.y);
    doc.setTextColor(INK);
    doc.setFontSize(8.5);
    doc.text(lines, M + keyW, cur.y);
    cur.y += lines.length * 3.9 + 1.2;
  }
}

function badge(doc, x, y, text, bg, fg = "#ffffff") {
  doc.setFont("helvetica", "bold");
  doc.setFontSize(7);
  const label = sanitize(text);
  const w = doc.getTextWidth(label) + 5;
  doc.setFillColor(bg);
  doc.roundedRect(x, y - 3.4, w, 5.2, 1.2, 1.2, "F");
  doc.setTextColor(fg);
  doc.text(label, x + 2.5, y);
  return w + 2.5;
}

function calloutBox(doc, cur, title, lines, tone) {
  const body = lines.filter(Boolean).map((t) => doc.splitTextToSize(sanitize(t), CW - 8));
  const h = 7 + body.reduce((n, l) => n + l.length * 3.9, 0);
  cur.room(h + 3);
  doc.setFillColor(tone.bg);
  doc.setDrawColor(tone.border);
  doc.setLineWidth(0.3);
  doc.roundedRect(M, cur.y - 3.5, CW, h, 1.5, 1.5, "FD");
  doc.setFont("helvetica", "bold");
  doc.setFontSize(7);
  doc.setTextColor(tone.title);
  doc.text(sanitize(title).toUpperCase(), M + 3, cur.y);
  let y = cur.y + 4.4;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(8);
  doc.setTextColor(tone.text);
  for (const l of body) {
    doc.text(l, M + 3, y);
    y += l.length * 3.9;
  }
  cur.y += h + 1.5;
}

function scoreChip(doc, score) {
  const bg = score >= 60 ? "#059669" : score >= 35 ? "#f59e0b" : "#9ca3af";
  const x = PW - M - 26;
  doc.setFillColor("#f9fafb");
  doc.setDrawColor(RULE);
  doc.roundedRect(x, 14, 26, 13, 1.5, 1.5, "FD");
  doc.setFont("helvetica", "normal");
  doc.setFontSize(6);
  doc.setTextColor(FAINT);
  doc.text("LEGAL SCORE", x + 13, 18, { align: "center" });
  doc.setFont("helvetica", "bold");
  doc.setFontSize(12);
  doc.setTextColor(bg);
  doc.text(String(score), x + 13, 24.5, { align: "center" });
}

function propertySheet(doc, row) {
  const { feature, n, color } = row;
  const p = feature.properties;
  const cur = sheetCursor(doc, PH - 26);
  const firstPage = doc.internal.getCurrentPageInfo().pageNumber;
  const reasonCodes = parseJsonProp(p.reason_codes) || [];
  const priorOp = parseJsonProp(p.prior_operator);
  const reversion = parseJsonProp(p.reversion);
  const rooms = estRooms(p);
  const score = computeScore(p);

  // Header
  numberedDisc(doc, M + 4, 19, 4, n, color);
  const titleW = CW - 42;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(14);
  doc.setTextColor(INK);
  const titleLines = doc.splitTextToSize(sanitize(p.hotel_name || p.address || "Unnamed building"), titleW);
  doc.text(titleLines.slice(0, 2), M + 11, 19);
  let hy = 19 + titleLines.slice(0, 2).length * 5.6;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(8.5);
  doc.setTextColor(MUTED);
  if (p.hotel_name && p.address) { doc.text(sanitize(p.address), M + 11, hy); hy += 4; }
  if (p.neighborhood) {
    doc.setTextColor(FAINT);
    doc.text(sanitize(p.neighborhood), M + 11, hy);
    hy += 4;
  }
  scoreChip(doc, score);
  cur.y = Math.max(hy + 2, 30);

  // Badges
  let bx = M;
  bx += badge(doc, bx, cur.y, segmentLabel(p.segment), color);
  if (p.has_reversion) bx += badge(doc, bx, cur.y, "Reversion", "#ef4444");
  if (p.has_prior_op) bx += badge(doc, bx, cur.y, "Prior operator", "#8b5cf6");
  if (p.coo_has_temporary) bx += badge(doc, bx, cur.y, "Temp C of O", "#f59e0b");
  const crm = (CRM_STATUSES.find((s) => s.value === row.crmStatus) || {}).label;
  if (crm && row.crmStatus) badge(doc, bx, cur.y, crm, "#374151");
  cur.y += 5;

  // Stats
  sectionHeading(doc, cur, "Capacity", 27);
  statGrid(doc, cur, [
    { label: "Est. rooms", value: rooms.value || "-", note: rooms.source },
    { label: "HPD Class B", value: num(p.hpd_class_b) || "-", note: "transient" },
    { label: "HPD Class A", value: num(p.hpd_class_a) || "-", note: "permanent" },
    { label: "C of O units", value: num(p.coo_dwelling_units) || "-", note: "DOB approved" },
    { label: "Floors", value: p.numfloors ? Math.round(p.numfloors) : "-" },
    { label: "Zoning", value: p.zonedist1 || "-" },
    { label: "Building class", value: p.bldgclass || "-" },
    { label: "BBL", value: p.bbl || "-", note: p.bin ? `BIN ${p.bin}` : null },
  ]);

  // Legal feasibility
  const feasibility = buildFeasibilityItems(p, reasonCodes);
  if (feasibility.length) {
    sectionHeading(doc, cur, "Legal feasibility");
    for (const item of feasibility) bullet(doc, cur, item.text, ICON_COLOR[item.icon] || FAINT);
    if (reasonCodes.length) {
      cur.room(6);
      doc.setFont("helvetica", "normal");
      doc.setFontSize(7);
      doc.setTextColor(MUTED);
      doc.text(doc.splitTextToSize(sanitize(`Reason codes: ${reasonCodes.join(", ")}`), CW)[0], M, cur.y);
      cur.y += 4;
    }
  }

  // Prior operator / reversion callouts
  if (priorOp) {
    calloutBox(doc, cur, "Prior flex operator", [priorOp.name, priorOp.notes, "Legality unverified"], {
      bg: "#faf5ff", border: "#e9d5ff", title: "#7e22ce", text: "#581c87",
    });
  }
  if (p.has_reversion && reversion) {
    calloutBox(doc, cur, "Post-2021 reversion opportunity", [
      reversion.former_hotel,
      reversion.closure_year ? `Closed ${reversion.closure_year}` : null,
      reversion.note,
      "Hotel class preserved - can revert without CPC special permit",
    ], { bg: "#fef2f2", border: "#fecaca", title: "#b91c1c", text: "#7f1d1d" });
  }

  // Current use
  const occupants = parseJsonProp(p.current_use_occupants) || [];
  const inBuilding = occupants.filter((o) => o.use !== "ground_floor_tenant").map((o) => o.name).filter(Boolean);
  if (p.current_use_label || inBuilding.length) {
    sectionHeading(doc, cur, "Current use");
    const src = [p.current_use_source, p.current_use_confidence && `${p.current_use_confidence} confidence`]
      .filter(Boolean).join(", ");
    keyValues(doc, cur, [
      p.current_use_label && ["In use as", `${p.current_use_label}${p.current_use_name ? ` (${p.current_use_name})` : ""}${src ? ` — ${src}` : ""}`],
      inBuilding.length && ["Occupants", inBuilding.join(", ")],
      p.current_use_conflict && ["Conflict", "City records show transient capacity here; the building on the ground is in other use."],
    ].filter(Boolean));
  }

  // Considerations
  const considerations = buildConsiderations(p);
  if (considerations.length) {
    const legal = considerations.filter((c) => c.kind === "legal");
    const operational = considerations.filter((c) => c.kind !== "legal");
    if (legal.length) {
      sectionHeading(doc, cur, "Legal considerations");
      for (const c of legal) bullet(doc, cur, c.text, SEVERITY_COLOR[c.severity] || FAINT, c.detail);
    }
    if (operational.length) {
      sectionHeading(doc, cur, "Operating considerations");
      for (const c of operational) bullet(doc, cur, c.text, SEVERITY_COLOR[c.severity] || FAINT, c.detail);
    }
  }

  // Condition
  const distress = buildDistressSignals(p);
  if (distress.length) {
    sectionHeading(doc, cur, "Property condition");
    for (const s of distress) bullet(doc, cur, s.label, SEVERITY_COLOR[s.severity] || FAINT, s.detail);
  }

  // Ownership and operator
  const ownership = [
    p.ownername && ["Owner (PLUTO)", p.ownername],
    p.owner_portfolio_size > 1 && ["Portfolio", `${p.owner_portfolio_size} buildings under this owner name`],
    p.acris_deed_owner && ["Deed owner (ACRIS)", `${p.acris_deed_owner}${p.acris_deed_date ? ` — ${fmtDate(p.acris_deed_date)}` : ""}`],
    p.acris_deed_address && ["Deed owner address", p.acris_deed_address],
    p.acris_borrower && ["Mortgage borrower", p.acris_borrower],
    p.acris_lender && ["Lender", `${p.acris_lender}${p.mortgage_amount ? ` — ${money(p.mortgage_amount)}` : ""}${p.acris_mtge_date ? ` (${fmtDate(p.acris_mtge_date)})` : ""}`],
    p.mortgage_approaching_maturity && ["Mortgage", `Approaching maturity${p.mortgage_age_years ? ` — ${Math.round(p.mortgage_age_years)} yrs old` : ""}`],
    p.last_sale_date && ["Last sale", `${fmtDate(p.last_sale_date)}${money(p.last_sale_price) ? ` — ${money(p.last_sale_price)}` : ""}${p.sale_count > 1 ? ` (${p.sale_count} sales on record)` : ""}`],
  ].filter(Boolean);
  if (ownership.length) {
    sectionHeading(doc, cur, "Ownership");
    keyValues(doc, cur, ownership);
  }

  const operator = [
    p.operator_name && ["Operator", `${p.operator_name}${p.operator_source ? ` (${p.operator_source})` : ""}`],
    p.hotel_license_status && ["DCWP license", `${p.hotel_license_status}${p.hotel_license_name ? ` — ${p.hotel_license_name}` : ""}`],
    p.hpd_managing_agent_corp && ["HPD managing agent", p.hpd_managing_agent_corp],
    p.hpd_head_officer && ["HPD head officer", p.hpd_head_officer],
    p.hotel_phone && ["Phone", p.hotel_phone],
    p.hotel_website && ["Website", p.hotel_website],
  ].filter(Boolean);
  if (operator.length) {
    sectionHeading(doc, cur, "Operator and contacts");
    keyValues(doc, cur, operator);
  }

  // Score breakdown, kept short: the hits only.
  const hits = buildScoreSignals(p).filter((s) => s.hit);
  if (hits.length) {
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8);
    const text = hits.map((s) => (s.pts ? `${s.pts > 0 ? "+" : ""}${s.pts} ${s.label}` : s.label)).join("  ·  ");
    const lines = doc.splitTextToSize(sanitize(text), CW);
    sectionHeading(doc, cur, `Legal score ${score}/100`, lines.length * 3.9);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8);
    doc.setTextColor(INK);
    doc.text(lines, M, cur.y);
    cur.y += lines.length * 3.9;
  }

  // Alternate addresses
  const alts = distinctAddresses(parseJsonProp(p.alt_addresses) || [], p.address);
  if (alts.length) {
    sectionHeading(doc, cur, "Also recorded as");
    keyValues(doc, cur, [["Addresses", alts.join(" · ")]]);
  }

  // Notes
  if (row.note) {
    sectionHeading(doc, cur, "Notes");
    const lines = doc.splitTextToSize(sanitize(row.note), CW);
    cur.room(lines.length * 3.9);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8.5);
    doc.setTextColor(INK);
    doc.text(lines, M, cur.y);
    cur.y += lines.length * 3.9;
  }

  // Record links, pinned to the foot of the sheet's first page.
  const links = buildRecordLinks(p.bbl, p.bin);
  const lastPage = doc.internal.getCurrentPageInfo().pageNumber;
  doc.setPage(firstPage);
  const ly = PH - 19;
  doc.setDrawColor(RULE);
  doc.setLineWidth(0.25);
  doc.line(M, ly - 5, PW - M, ly - 5);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(7);
  doc.setTextColor(FAINT);
  doc.text("CITY RECORDS", M, ly - 1.5);
  doc.setFontSize(8);
  doc.setTextColor("#2563eb");
  let lx = M + 24;
  for (const [label, url] of [["HPD Online", links.hpd], ["DOB BIS", links.dob], ["ACRIS", links.acris]]) {
    doc.textWithLink(label, lx, ly - 1.5, { url });
    lx += doc.getTextWidth(label) + 8;
  }
  doc.setPage(lastPage);
}

function paginate(doc) {
  const total = doc.internal.getNumberOfPages();
  for (let i = 1; i <= total; i++) {
    doc.setPage(i);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7);
    doc.setTextColor(FAINT);
    doc.text("NYC Transient Capacity", M, PH - 9);
    doc.text(`${i} / ${total}`, PW - M, PH - 9, { align: "right" });
  }
}

// --- Entry point ---

export async function exportToPdf(features, { notes = {}, crmStatuses = {} } = {}) {
  if (!features.length) return;

  const rows = features.map((feature, i) => {
    const p = feature.properties;
    return {
      feature,
      n: i + 1,
      color: segmentColor(p.segment),
      lngLat: featureCentroid(feature),
      title: p.hotel_name || p.address || "Unnamed building",
      rooms: estRooms(p),
      note: notes[p.bbl] || "",
      crmStatus: crmStatuses[p.bbl] || "",
      get subtitle() {
        const r = this.rooms;
        return [
          p.hotel_name ? p.address : p.neighborhood,
          r.value ? `${r.value} rooms` : null,
          segmentLabel(p.segment),
        ].filter(Boolean).join(" · ");
      },
    };
  });

  const doc = makeDoc();

  // Sized before the capture so the image is rendered at the aspect it will be
  // printed at: scaling a raster map to fit afterwards is what makes exported
  // maps look stretched.
  const legendRows = Math.ceil(rows.length / 2);
  const legendH = Math.min(legendRows, 12) * 8.4 + 6;
  const mapY = 31;
  const mapH = Math.max(88, Math.min(176, PH - mapY - legendH - 16));
  const mapBox = { x: M, y: mapY, w: CW, h: mapH };

  let mapImage = null;
  const points = rows.filter((r) => Array.isArray(r.lngLat));
  if (points.length) {
    const widthPx = 1120;
    const heightPx = Math.round((widthPx * mapH) / CW);
    try {
      mapImage = await captureMapImage(points, widthPx, heightPx);
    } catch (err) {
      // A packet without its map still beats no packet.
      console.error("PDF map capture failed", err);
    }
  }

  coverPage(doc, rows, mapImage, mapBox);
  drawLegend(doc, rows, mapBox.y + mapBox.h + 10);

  for (const row of rows) {
    doc.addPage();
    propertySheet(doc, row);
  }

  paginate(doc);
  doc.save(`nyc_transient_packet_${new Date().toISOString().slice(0, 10)}.pdf`);
}
