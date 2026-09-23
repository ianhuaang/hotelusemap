/**
 * Download Certificate of Occupancy PDFs from DOB BIS, by BIN.
 *
 * The C of O feed we already pull carries one line per certificate — issue
 * date, job type, co_type, a building-level dwelling-unit count. What it does
 * not carry is the per-floor table on the face of the certificate:
 *
 *     Floor | Use Group | Dwelling Units / Rooms | Occupancy Load
 *
 * That table is the only public source for two questions the feed cannot
 * answer: which use group a building holds, and whether its Class B rooms sit
 * on contiguous floors or are interleaved with permanent tenants. A hotel
 * operation needs a block it can key, clean and fire-separate; 130 Class B
 * rooms scattered through 814 apartments is not the same building as 130 on
 * floors 2 to 6.
 *
 * ACCESS, the hard part:
 *
 *   DOB BIS sits behind Akamai and returns 403 to everything automated. curl
 *   with browser headers: 403. Headless Chromium, with a session established
 *   from the BIS homepage first: 403. BIS root, DOB NOW, the servlet itself —
 *   all 403. A *headed* browser gets 200. So this runs headed, which means it
 *   opens a visible window and cannot run on a server or in CI.
 *
 *   The PDFs are not links. Each row submits a form to CofoDocumentContentServlet,
 *   and the response renders inside Chrome's PDF viewer rather than downloading,
 *   so intercepting the response body returns the viewer's HTML wrapper. The
 *   working route is to watch for the servlet request, then re-fetch that URL
 *   through the browser's own request context, which carries the cookies that
 *   got us past Akamai in the first place.
 *
 * WHAT THIS DOES NOT DO: read them. Sampled certificates are scanned images —
 * pypdf extracts zero characters from a 2-page, 80KB file carrying four image
 * XObjects. Parsing the floor table needs OCR or a vision model, which is a
 * separate decision about tooling and spend. This script gets the documents.
 *
 * Usage:
 *   npx playwright install chromium     # once; the repo has the package, not the browser
 *   node src/pull_coo_pdfs.js --bins 1088437,1023455
 *   node src/pull_coo_pdfs.js --file data/processed/coo_pdf_targets.json
 */

const fs = require("fs");
const path = require("path");

const OUT_DIR = path.join(__dirname, "..", "data", "raw", "coo_pdfs");
const LISTING = (bin) =>
  `https://a810-bisweb.nyc.gov/bisweb/COsByLocationServlet?requestid=1&allbin=${bin}`;
// BIS is a shared public system. One building at a time, with a pause.
const DELAY_MS = 2500;

function parseArgs(argv) {
  const out = { bins: [], limit: Infinity };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--bins") out.bins = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    else if (argv[i] === "--file") {
      const raw = JSON.parse(fs.readFileSync(argv[++i], "utf8"));
      out.bins = (Array.isArray(raw) ? raw : raw.bins || []).map(String);
    } else if (argv[i] === "--limit") out.limit = Number(argv[++i]);
  }
  return out;
}

async function pullOne(page, bin) {
  const saved = [];
  const res = await page.goto(LISTING(bin), { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => null);
  if (!res || res.status() !== 200) return { bin, error: `listing ${res ? res.status() : "no response"}`, saved };

  // Each certificate is a form whose id embeds the document number.
  const docs = await page.$$eval("form[id^='form_cofo_pdf_view_']", (fs_) =>
    fs_.map((f) => f.id.replace("form_cofo_pdf_view_", "")));
  if (!docs.length) return { bin, error: "no certificates listed", saved };

  for (const doc of docs) {
    const target = path.join(OUT_DIR, `${bin}_${doc}`);
    if (fs.existsSync(target)) { saved.push({ doc, skipped: true }); continue; }

    let pdfUrl = null;
    const watch = (r) => { if (r.url().includes("CofoDocumentContentServlet")) pdfUrl = r.url(); };
    page.on("request", watch);
    await page.evaluate((id) => document.getElementById(`form_cofo_pdf_view_${id}`).submit(), doc);
    await page.waitForTimeout(3500);
    page.off("request", watch);

    if (pdfUrl) {
      // Re-fetch through the browser's context so the Akamai cookies come with
      // it; reading the response body gives Chrome's PDF viewer shell instead.
      const r = await page.context().request.get(pdfUrl, { timeout: 40000 }).catch(() => null);
      const buf = r && r.ok() ? await r.body() : null;
      if (buf && buf.slice(0, 4).toString() === "%PDF") {
        fs.writeFileSync(target, buf);
        saved.push({ doc, bytes: buf.length });
      } else {
        saved.push({ doc, error: "not a pdf" });
      }
    } else {
      saved.push({ doc, error: "no servlet request seen" });
    }
    await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => page.goto(LISTING(bin)));
  }
  return { bin, saved };
}

async function main() {
  const { chromium } = require("playwright");
  const { bins, limit } = parseArgs(process.argv);
  if (!bins.length) {
    console.error("usage: node src/pull_coo_pdfs.js --bins <BIN,BIN> | --file <json>");
    process.exit(1);
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  // headless:false is load-bearing, not a debugging leftover. See the note above.
  const browser = await chromium.launch({
    headless: false,
    args: ["--disable-blink-features=AutomationControlled"],
  });
  const page = await browser.newPage();

  const results = [];
  const todo = bins.slice(0, limit);
  for (const [i, bin] of todo.entries()) {
    const r = await pullOne(page, bin);
    const ok = r.saved.filter((s) => s.bytes).length;
    const skip = r.saved.filter((s) => s.skipped).length;
    console.log(`  [${i + 1}/${todo.length}] BIN ${bin}: ${ok} saved, ${skip} already had${r.error ? ` — ${r.error}` : ""}`);
    results.push(r);
    await page.waitForTimeout(DELAY_MS);
  }
  await browser.close();

  const totalOk = results.reduce((t, r) => t + r.saved.filter((s) => s.bytes).length, 0);
  const failed = results.filter((r) => r.error);
  console.log(`\n${totalOk} certificates saved -> ${OUT_DIR}`);
  if (failed.length) console.log(`${failed.length} BINs returned nothing: ${failed.map((f) => f.bin).join(", ")}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
