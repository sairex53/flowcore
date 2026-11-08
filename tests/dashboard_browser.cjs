/* Playwright smoke against a live dashboard, invoked by dashboard_live.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

async function run() {
  const [url, credential, screenshots, appArtifact, pathArtifact] = process.argv.slice(2);
  const secret = fs.readFileSync(credential, "utf8").trim();
  const browser = await chromium.launch({headless: true, args: ["--disable-dev-shm-usage"]});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 960}});
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(url);
    assert.equal((await page.request.get(url + "/api/v1/overview")).status(), 401);
    await page.getByLabel("Bearer token").fill(secret);
    await page.getByRole("button", {name: /Unlock dashboard/}).click();
    await page.getByRole("heading", {name: "Interface throughput", exact: true}).waitFor();
    await page.getByRole("heading", {name: "Traffic classes", exact: true}).waitFor();
    await page.getByRole("img", {name: "Interface throughput in Mbit/s"}).waitFor();
    assert.ok(await page.getByText("lab", {exact: true}).count());
    const previous = await page.locator("#sample-meta").textContent();
    await page.waitForFunction((value) => document.getElementById("sample-meta").textContent !== value,
      previous, {timeout: 6000});
    await page.screenshot({path: path.join(screenshots, "overview.png"), fullPage: true});

    await page.getByRole("button", {name: "Pause", exact: true}).click();
    await page.getByText("Paused", {exact: true}).waitFor();
    await page.getByRole("link", {name: "Service SLOs"}).click();
    await page.getByRole("heading", {name: "Request latency", exact: true}).waitFor();
    await page.getByRole("img", {name: "Application request latency and SLO target"}).waitFor();
    await page.screenshot({path: path.join(screenshots, "services.png"), fullPage: true});
    await page.getByRole("link", {name: "Controller", exact: true}).click();
    await page.getByRole("heading", {name: "Decision timeline", exact: true}).waitFor();
    await page.locator(".event").first().click();
    await page.getByRole("heading", {name: "Decision detail", exact: true}).waitFor();

    await page.getByRole("link", {name: "Bottlenecks", exact: true}).click();
    await page.getByRole("heading", {name: "Attribution", exact: true}).waitFor();
    await page.getByRole("link", {name: "Incidents", exact: true}).click();
    await page.getByRole("heading", {name: "Incident timeline", exact: true}).waitFor();
    await page.getByLabel("Event kind").selectOption("fault");
    await page.locator(".event").first().click();
    await page.getByText(/Selected event · fault/).waitFor();
    await page.screenshot({path: path.join(screenshots, "incident.png"), fullPage: true});

    await page.getByRole("link", {name: "Multipath", exact: true}).click();
    await page.getByRole("heading", {name: "Current MPTCP scope", exact: true}).waitFor();
    if (pathArtifact) {
      await page.getByLabel("Dataset").selectOption(pathArtifact);
      await page.getByText("Observed subflows", {exact: true}).waitFor();
    }
    await page.getByRole("link", {name: "Experiments", exact: true}).click();
    await page.getByRole("heading", {name: "Recorded experiment matrices", exact: true}).waitFor();
    if (appArtifact) {
      await page.getByLabel("Dataset").selectOption(appArtifact);
      await page.getByRole("img", {name: "P99 by recorded repetition"}).waitFor();
      await page.getByRole("heading", {name: "Run comparison", exact: true}).waitFor();
      await page.screenshot({path: path.join(screenshots, "experiments.png"), fullPage: true});
    }

    await page.setViewportSize({width: 390, height: 844});
    await page.getByRole("link", {name: "Overview", exact: true}).click();
    await page.getByRole("heading", {name: "Interface throughput", exact: true}).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    assert.ok(await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0));
    await page.screenshot({path: path.join(screenshots, "mobile.png"), fullPage: true});
    await page.getByRole("button", {name: "Lock", exact: true}).click();
    await page.getByRole("heading", {name: "Open the dashboard", exact: true}).waitFor();
    assert.deepEqual(errors, []);
    console.log("PASS: browser authentication, charts, refresh, navigation, incident filtering and mobile layout");
  } finally {
    await browser.close();
  }
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
