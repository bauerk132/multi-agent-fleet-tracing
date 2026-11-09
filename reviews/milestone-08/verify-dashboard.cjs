// Browser regression checks for the standalone M8 rendering repair.
// Uses build-only Playwright and installed Google Chrome, not a dashboard dependency.
const { chromium } = require('./.build/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const file = path.resolve(process.argv[2] || 'm8-node-graph-dashboard.html');
  const prefix = process.argv[3] || 'offline-verification';
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  // A new context has no CDN cache; network is disabled BEFORE navigation.
  const context = await browser.newContext({ offline: true, serviceWorkers: 'block', viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const runtimeErrors = [], missingResources = [], networkRequests = [], checks = [];
  page.on('pageerror', error => runtimeErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') missingResources.push(message.text()); });
  page.on('request', request => { if (/^https?:/.test(request.url())) networkRequests.push(request.url()); });
  page.on('requestfailed', request => missingResources.push(`${request.url()}: ${request.failure()?.errorText}`));
  async function check(name, fn) {
    try { await fn(); checks.push({ name, passed: true }); }
    catch (error) { checks.push({ name, passed: false, error: error.message }); }
  }
  await page.goto(pathToFileURL(file).href);
  await page.evaluate(() => document.fonts.ready);
  const tree = page.locator('#tree-container');
  const details = page.locator('[aria-label="Span Details"]');
  const attributes = page.locator('[aria-label="Recorded Attributes"]');
  const nodes = tree.locator('.tree-node');
  await check('offline dark surfaces and positioned shell', async () => {
    const shell = await page.evaluate(() => ({
      background: getComputedStyle(document.body).backgroundColor,
      header: document.body.querySelector('header').getBoundingClientRect().toJSON(),
      aside: document.querySelector('aside').getBoundingClientRect().toJSON(),
      panel: getComputedStyle(document.querySelector('aside')).backgroundColor,
      viewport: innerHeight,
      footer: document.querySelector('[aria-label="Span Inspector Panel"] footer').getBoundingClientRect().toJSON()
    }));
    assert.equal(shell.background, 'rgb(14, 17, 22)');
    assert.equal(shell.panel, 'rgb(22, 27, 34)');
    assert.equal(shell.header.height, 40);
    assert.equal(shell.aside.x, 0);
    assert.equal(shell.aside.width, 224);
    assert.equal(shell.aside.y, 40);
    assert.ok(shell.footer.bottom <= shell.viewport, 'inspector footer must fit viewport');
  });
  await check('three spans retain parent hierarchy and proof values', async () => {
    assert.equal(await nodes.count(), 3);
    const values = await nodes.evaluateAll(elements => elements.map(el => ({
      id: el.dataset.nodeId, row: el.firstElementChild.textContent,
      parent: el.parentElement.closest('.tree-node')?.dataset.nodeId || null
    })));
    assert.deepEqual(values.map(x => x.parent), [null, 'spn_7dc938624bbd0ad1', 'spn_7dc938624bbd0ad1']);
    assert.match(values[0].row, /agent\.run[\s\S]*2000ms/);
    assert.match(values[1].row, /llm\.call[\s\S]*120[\s\S]*340[\s\S]*1000ms/);
    assert.match(values[2].row, /tool\.exec[\s\S]*web-search[\s\S]*300ms/);
  });
  await check('select root updates inspector and clears LLM attributes', async () => {
    await nodes.nth(0).locator(':scope > div').first().click();
    assert.match(await details.innerText(), /agent\.run[\s\S]*2000ms/);
    assert.doesNotMatch(await attributes.innerText(), /gpt-5|gen_ai\.usage/);
    assert.match(await details.innerText(), /Unknown/);
  });
  await check('select tool updates duration, tool name and call ID', async () => {
    await nodes.nth(2).locator(':scope > div').first().click();
    assert.match(await details.innerText(), /tool\.exec[\s\S]*300ms/);
    assert.match(await attributes.innerText(), /web-search[\s\S]*call-123/);
    assert.doesNotMatch(await attributes.innerText(), /gpt-5|gen_ai\.usage/);
  });
  await check('select LLM restores model and 120/340 token inspector', async () => {
    await nodes.nth(1).locator(':scope > div').first().click();
    assert.match(await details.innerText(), /llm\.call[\s\S]*1000ms/);
    assert.match(await details.innerText(), /460 total[\s\S]*120 in[\s\S]*340 out/);
    assert.match(await attributes.innerText(), /gpt-5/);
    assert.equal(await page.locator('#native-span-id').innerText(), 'spn_742570ebc6606ada');
    assert.match(await page.locator('[aria-label="Span Inspector Panel"] footer').innerText(), /spn_742570ebc6606ada/);
  });
  await check('collapse and expand controls operate on hierarchy', async () => {
    await page.locator('#btn-collapse-all').click();
    assert.equal(await nodes.nth(1).isVisible(), false);
    assert.equal(await nodes.nth(2).isVisible(), false);
    await page.locator('#btn-expand-all').click();
    assert.equal(await nodes.nth(1).isVisible(), true);
    assert.equal(await nodes.nth(2).isVisible(), true);
    await nodes.nth(0).getByRole('button', { name: 'Toggle node' }).click();
    assert.equal(await nodes.nth(1).isVisible(), false);
    await nodes.nth(0).getByRole('button', { name: 'Toggle node' }).click();
    assert.equal(await nodes.nth(1).isVisible(), true);
  });
  await check('search preserves matching descendants and their ancestor', async () => {
    await page.locator('#span-search-input').fill('web-search');
    assert.equal(await nodes.nth(0).isVisible(), true);
    assert.equal(await nodes.nth(1).isVisible(), false);
    assert.equal(await nodes.nth(2).isVisible(), true);
    await page.locator('#span-search-input').fill('no-such-span');
    assert.equal(await nodes.nth(0).isVisible(), false);
    await page.locator('#span-search-input').fill('');
    assert.equal(await nodes.nth(0).isVisible(), true);
    assert.equal(await nodes.nth(1).isVisible(), true);
  });
  await check('two original findings and honest partial provenance remain', async () => {
    const findings = await page.locator('[aria-label="Findings"]').innerText();
    assert.match(findings, /FINDINGS \(2\)/i);
    assert.match(findings, /Slowest span #1: llm\.call[\s\S]*1000\.0 ms[\s\S]*50\.0%/);
    assert.match(findings, /Slowest span #2: tool\.exec[\s\S]*300\.0 ms[\s\S]*15\.0%/);
    const text = await page.locator('body').innerText();
    assert.match(text, /run_939d2ebd75b177e3/);
    assert.match(text, /imp_875401aa964d651a/);
    assert.match(text, /No native graph — shown as a tree\. Partial is honest, not broken\./);
    assert.match(text, /synthetic Copilot-style source/i);
    assert.match(text, /genuine Copilot exporter validation pending/i);
    for (const old of ['run_c73771bb67feeeee', 'copilot_request_handler', 'workspace_fs_query']) assert.ok(!text.includes(old), `obsolete value ${old}`);
  });
  await check('desktop widths keep panes and horizontal tree scrolling usable', async () => {
    for (const viewport of [{width:1280,height:800},{width:1440,height:900},{width:1920,height:1080}]) {
      await page.setViewportSize(viewport);
      const geometry = await page.evaluate(() => {
        const inspector = document.querySelector('[aria-label="Span Inspector Panel"]');
        const container = document.querySelector('#tree-container');
        return { inspector: inspector.getBoundingClientRect().toJSON(), bodyWidth: document.body.scrollWidth,
          treeOverflow: getComputedStyle(container).overflowX, paneOverlap: document.querySelector('[aria-label="Trace Tree Hierarchy"]').getBoundingClientRect().right > inspector.getBoundingClientRect().left + 1 };
      });
      assert.ok(geometry.inspector.width >= 300);
      assert.ok(geometry.inspector.right <= viewport.width + 1);
      assert.ok(geometry.inspector.bottom <= viewport.height + 1);
      assert.ok(geometry.bodyWidth <= viewport.width);
      assert.equal(geometry.paneOverlap, false);
      assert.equal(geometry.treeOverflow, 'auto');
    }
  });
  await check('embedded fonts and icons load without remote resources', async () => {
    assert.equal(await page.evaluate(() => document.fonts.check('12px "Inter"') && document.fonts.check('12px "IBM Plex Mono"') && document.fonts.check('16px "Material Symbols Outlined"')), true);
    assert.deepEqual(networkRequests, []);
    assert.deepEqual(missingResources, []);
    assert.deepEqual(runtimeErrors, []);
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: `${prefix}.png`, fullPage: true });
  const report = { file, browser: browser.version(), offlineBeforeNavigation: true, freshContext: true, checks, runtimeErrors, missingResources, networkRequests };
  fs.writeFileSync(`${prefix}.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
  if (checks.some(check => !check.passed)) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
