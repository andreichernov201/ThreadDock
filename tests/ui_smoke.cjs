const { chromium } = require("playwright");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const root = path.resolve("archive/web");
  const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  const server = http.createServer(async (req, res) => {
    const name = new URL(req.url, "http://localhost").pathname.replace(/^\/assets\//, "/");
    const file = name === "/" ? "index.html" : name.slice(1);
    if (!["index.html", "app.js", "style.css", "texts.js", "demo.js", "icon.svg"].includes(file)) {
      res.writeHead(404).end(); return;
    }
    try {
      res.writeHead(200, { "Content-Type": types[path.extname(file)] });
      res.end(await fs.readFile(path.join(root, file)));
    } catch { res.writeHead(500).end(); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("https://telegram.org/**", route => route.abort());
  const url = "http://127.0.0.1:" + server.address().port + "/?demo=1";
  async function screenshot(name) {
    const bytes = await page.screenshot();
    const directory = path.join(process.env.RUNNER_TEMP || ".deploy-work", "threaddock-ui");
    await fs.mkdir(directory, { recursive: true });
    await fs.writeFile(path.join(directory, name + ".png"), bytes);
    if (process.env.UI_SCREENSHOT_LOG !== "1") return;
    const encoded = bytes.toString("hex").replace(/[0-9a-f]/g, c => String.fromCharCode(97 + parseInt(c, 16)));
    for (let index = 0; index < encoded.length; index += 16000) {
      console.log("SCREENSHOT_CHUNK " + JSON.stringify({ name, data: encoded.slice(index, index + 16000) }));
    }
  }
  try {
    await page.goto(url);
    await page.locator(".event-row").first().waitFor();
    assert(await page.locator(".feed-day").count() > 0);
    assert.equal(await page.locator(".sidebar").isVisible(), false);
    assert(await page.locator(".bottom-nav").isVisible());
    await screenshot("mobile-feed");
    console.log("UI_OVERFLOW " + JSON.stringify(await page.evaluate(() => [...document.querySelectorAll("*")].filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => ({tag:el.tagName, cls:el.className, width:el.getBoundingClientRect().width, right:el.getBoundingClientRect().right})))));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.locator('[data-action="filter"][data-mode="edited"]').click();
    await page.locator(".event-row").first().click();
    await page.locator('dialog[open][data-view="record"]').waitFor();
    assert(await page.locator(".record-content").isVisible());
    assert(await page.locator(".version ins").count() > 0);
    assert(await page.locator(".version del").count() > 0);
    await screenshot("mobile-event");
    await page.locator('.record-toolbar [data-action="close-modal"]').click();
    await page.locator('.bottom-nav [data-view="stats"]').click();
    await page.locator(".heatmap").waitFor();
    assert.equal(await page.locator(".stat").count(), 4);
    await page.locator('.bottom-nav [data-view="feed"]').click();
    await page.setViewportSize({ width: 1200, height: 900 });
    await page.locator(".event-row").first().waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(errors, []);
    console.log("UI_CHECK_PASSED mobile, desktop, event history and statistics");
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
