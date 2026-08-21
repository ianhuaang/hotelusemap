/**
 * Look up hotel names, phone numbers, and ratings from Google Maps
 * for H-class buildings in the pipeline.
 *
 * Uses Playwright to search Google Maps by address and extract business info.
 * Output: data/raw/hotel_info_{date}.json
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BATCH_SIZE = 5;
const DELAY_BETWEEN = 2000;

async function loadAddresses() {
  const processedDir = path.join(__dirname, "..", "data", "processed");
  const files = fs.readdirSync(processedDir).filter((f) => f.startsWith("pipeline_") && f.endsWith(".json"));
  files.sort().reverse();
  const pipeline = JSON.parse(fs.readFileSync(path.join(processedDir, files[0]), "utf8"));

  const targets = pipeline.filter(
    (r) =>
      (r.tier === "legal_transient" || r.tier === "partial" || r.prior_operator) &&
      ((r.bldgclass || "").startsWith("H") || r.prior_operator)
  );

  // Dedupe by address
  const byAddr = new Map();
  for (const r of targets) {
    const addr = (r.address || "").trim();
    if (!addr) continue;
    if (!byAddr.has(addr)) {
      byAddr.set(addr, { bbl: r.bbl, address: addr, borough: addr.includes("Queens") ? "Queens" : addr.includes("Brooklyn") ? "Brooklyn" : "Manhattan" });
    }
  }
  return Array.from(byAddr.values());
}

async function searchGoogleMaps(page, address) {
  const query = address + " hotel, New York, NY";
  const searchUrl = `https://www.google.com/maps/search/${encodeURIComponent(query)}`;

  try {
    await page.goto(searchUrl, { waitUntil: "networkidle", timeout: 20000 });
    await page.waitForTimeout(4000);

    // Try clicking the first result if we're on a list view
    try {
      const firstResult = await page.$('[role="feed"] > div:first-child a[aria-label]');
      if (firstResult) {
        await firstResult.click();
        await page.waitForTimeout(3000);
      }
    } catch {}

    // Extract from aria-labels on action buttons (most reliable)
    const info = await page.evaluate(() => {
      const result = {};
      const body = document.body.innerText;

      // Name from h1
      const h1 = document.querySelector("h1");
      if (h1) {
        const name = h1.textContent.trim();
        // Skip if it's just an address
        if (name && !/^\d+\s/.test(name)) {
          result.name = name;
        }
      }

      // Scan all aria-labels for phone, website, address info
      const allEls = document.querySelectorAll("[aria-label]");
      for (const el of allEls) {
        const label = el.getAttribute("aria-label") || "";

        // Phone
        if (/^Phone:/.test(label)) {
          result.phone = label.replace("Phone: ", "").trim();
        }
        // Website
        if (/^Website:/.test(label)) {
          result.website = label.replace("Website: ", "").trim();
        }
      }

      // Rating from aria-label like "4.5 stars"
      const ratingEl = document.querySelector('[role="img"][aria-label*="stars"]');
      if (ratingEl) {
        const m = ratingEl.getAttribute("aria-label").match(/([\d.]+)\s*star/);
        if (m) result.rating = m[1];
      }

      // Review count
      const reviewMatch = body.match(/([\d,]+)\s*reviews?\b/i);
      if (reviewMatch) result.reviews = reviewMatch[1].replace(/,/g, "");

      // Category from the business type text below name
      const catEls = document.querySelectorAll('[class*="fontBodyMedium"] button, [class*="fontBodyMedium"] span');
      for (const el of catEls) {
        const t = el.textContent.trim();
        if (/hotel|hostel|inn|motel|lodge|resort|apartment/i.test(t) && t.length < 40) {
          result.category = t;
          break;
        }
      }

      return result;
    });

    // If no name found, check if h1 has a hotel-ish name
    if (!info.name) {
      const h1Text = await page.$eval("h1", (el) => el.textContent.trim()).catch(() => "");
      if (h1Text && /hotel|inn|suite|residence|hostel|lodge/i.test(h1Text)) {
        info.name = h1Text;
      }
    }

    return info;
  } catch (e) {
    return { error: e.message };
  }
}

async function main() {
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const outPath = path.join(__dirname, "..", "data", "raw", `hotel_info_${today}.json`);

  if (fs.existsSync(outPath)) {
    console.log(`Already exists: ${outPath}`);
    return;
  }

  const addresses = await loadAddresses();
  console.log(`Looking up ${addresses.length} hotel/operator addresses on Google Maps...`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    locale: "en-US",
  });
  const page = await context.newPage();

  // Accept cookies on first load
  await page.goto("https://www.google.com/maps", { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForTimeout(2000);
  try {
    const acceptBtn = await page.$('button:has-text("Accept all")');
    if (acceptBtn) await acceptBtn.click();
  } catch {}
  await page.waitForTimeout(1000);

  const results = [];
  let found = 0;

  for (let i = 0; i < addresses.length; i++) {
    const { bbl, address } = addresses[i];

    try {
      const info = await searchGoogleMaps(page, address);
      results.push({ bbl, address, ...info });
      if (info.name) found++;
    } catch (e) {
      results.push({ bbl, address, error: e.message });
    }

    if ((i + 1) % 10 === 0 || i === addresses.length - 1) {
      console.log(`  ${i + 1}/${addresses.length} checked, ${found} with business info`);
    }

    // Save progress every 100
    if ((i + 1) % 100 === 0) {
      fs.writeFileSync(outPath + ".partial", JSON.stringify(results, null, 2));
    }

    await page.waitForTimeout(DELAY_BETWEEN);
  }

  await browser.close();

  fs.writeFileSync(outPath, JSON.stringify(results, null, 2));
  console.log(`\nDone! ${results.length} addresses checked`);
  console.log(`  With business name: ${results.filter((r) => r.name).length}`);
  console.log(`  With phone: ${results.filter((r) => r.phone).length}`);
  console.log(`  With rating: ${results.filter((r) => r.rating).length}`);
  console.log(`Saved -> ${outPath}`);

  // Clean up partial
  try { fs.unlinkSync(outPath + ".partial"); } catch {}
}

main().catch(console.error);
