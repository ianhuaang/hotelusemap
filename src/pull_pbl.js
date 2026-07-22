/**
 * Scrape the OSE Prohibited Buildings List for all buildings in the pipeline.
 *
 * Uses Playwright to establish a session with the Salesforce Aura portal,
 * then makes batch API calls to check each building address.
 *
 * Output: data/raw/pbl_{date}.json — array of { bbl, address, on_pbl, pbl_data }
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BATCH_SIZE = 10; // requests per batch
const DELAY_BETWEEN_BATCHES = 2000; // ms
const DELAY_BETWEEN_REQUESTS = 300; // ms

async function loadAddresses() {
  // Find most recent pipeline file
  const processedDir = path.join(__dirname, "..", "data", "processed");
  const files = fs.readdirSync(processedDir).filter((f) => f.startsWith("pipeline_") && f.endsWith(".json"));
  files.sort().reverse();
  const pipelinePath = path.join(processedDir, files[0]);
  console.log(`Loading addresses from ${files[0]}`);
  const pipeline = JSON.parse(fs.readFileSync(pipelinePath, "utf8"));

  // Deduplicate by address (many condo lots share one address)
  const byAddr = new Map();
  for (const r of pipeline) {
    const addr = (r.address || "").trim();
    if (!addr) continue;
    if (!byAddr.has(addr)) {
      byAddr.set(addr, { bbl: r.bbl, address: addr });
    }
  }
  return Array.from(byAddr.values());
}

async function checkBatch(page, addresses) {
  return page.evaluate(async ({ addrs, delay }) => {
    const results = [];
    for (const { bbl, address } of addrs) {
      try {
        const message = JSON.stringify({
          actions: [{
            id: "999;a",
            descriptor: "aura://ApexActionController/ACTION$execute",
            callingDescriptor: "UNKNOWN",
            params: {
              namespace: "",
              classname: "SearchBuildingsAddressCtrl",
              method: "getProhibitedBuildingAddress",
              params: { searchValue: address },
              cacheable: false,
              isContinuation: false,
            },
          }],
        });

        const resp = await fetch("/s/sfsites/aura?r=99&aura.ApexAction.execute=1", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ message, "aura.context": "", "aura.token": "null" }),
        });
        const data = await resp.json();
        const rv = data?.actions?.[0]?.returnValue?.returnValue;
        results.push({
          bbl,
          address,
          on_pbl: Array.isArray(rv) && rv.length > 0,
          pbl_data: rv?.[0] || null,
        });
      } catch (e) {
        results.push({ bbl, address, on_pbl: null, error: e.message });
      }
      // Small delay between requests
      await new Promise((r) => setTimeout(r, delay));
    }
    return results;
  }, { addrs: addresses, delay: DELAY_BETWEEN_REQUESTS });
}

async function main() {
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const outPath = path.join(__dirname, "..", "data", "raw", `pbl_${today}.json`);

  if (fs.existsSync(outPath)) {
    console.log(`Already exists: ${outPath}`);
    return;
  }

  const addresses = await loadAddresses();
  console.log(`Checking ${addresses.length} unique addresses against PBL...`);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  // Load the portal to establish session
  console.log("Loading OSE portal...");
  await page.goto("https://strr-portal.ose.nyc.gov/s/searchbuildingsaddress", {
    waitUntil: "networkidle",
    timeout: 30000,
  });

  // Do initial search to warm up the Aura context
  const searchInput = await page.$('input[name="enter-search"]');
  await searchInput.click();
  await page.keyboard.type("test", { delay: 30 });
  await page.waitForTimeout(500);
  await page.click("button.custom-css_style", { force: true });
  await page.waitForTimeout(2000);

  const allResults = [];
  let prohibited = 0;
  let errors = 0;

  for (let i = 0; i < addresses.length; i += BATCH_SIZE) {
    const batch = addresses.slice(i, i + BATCH_SIZE);
    const batchNum = Math.floor(i / BATCH_SIZE) + 1;
    const totalBatches = Math.ceil(addresses.length / BATCH_SIZE);

    try {
      const results = await checkBatch(page, batch);
      allResults.push(...results);

      const batchProhibited = results.filter((r) => r.on_pbl).length;
      const batchErrors = results.filter((r) => r.on_pbl === null).length;
      prohibited += batchProhibited;
      errors += batchErrors;

      console.log(
        `  Batch ${batchNum}/${totalBatches}: ${results.length} checked, ${batchProhibited} prohibited, ${batchErrors} errors (total: ${allResults.length}/${addresses.length})`
      );
    } catch (e) {
      console.error(`  Batch ${batchNum} failed: ${e.message}`);
      // Mark all in batch as errors
      for (const addr of batch) {
        allResults.push({ ...addr, on_pbl: null, error: e.message });
      }
      errors += batch.length;

      // Try to recover by reloading the page
      try {
        await page.goto("https://strr-portal.ose.nyc.gov/s/searchbuildingsaddress", {
          waitUntil: "networkidle",
          timeout: 30000,
        });
        const si = await page.$('input[name="enter-search"]');
        await si.click();
        await page.keyboard.type("test", { delay: 30 });
        await page.waitForTimeout(500);
        await page.click("button.custom-css_style", { force: true });
        await page.waitForTimeout(2000);
      } catch {
        console.error("  Failed to recover session, continuing...");
      }
    }

    // Save progress every 100 batches
    if (batchNum % 100 === 0) {
      const tmpPath = outPath + ".partial";
      fs.writeFileSync(tmpPath, JSON.stringify(allResults, null, 2));
      console.log(`  Saved progress: ${allResults.length} results`);
    }

    await new Promise((r) => setTimeout(r, DELAY_BETWEEN_BATCHES));
  }

  await browser.close();

  fs.writeFileSync(outPath, JSON.stringify(allResults, null, 2));
  console.log(`\nDone! ${allResults.length} addresses checked`);
  console.log(`  Prohibited: ${prohibited}`);
  console.log(`  Not prohibited: ${allResults.length - prohibited - errors}`);
  console.log(`  Errors: ${errors}`);
  console.log(`Saved -> ${outPath}`);
}

main().catch(console.error);
