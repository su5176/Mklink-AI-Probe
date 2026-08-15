// Screenshot the MKLink GUI demo tabs against the mock backend.
// Usage: node demo-shots.cjs   (run from the gui/ dir; needs `playwright` + chromium)
const { chromium } = require('playwright');
const path = require('path');

const BASE = 'http://localhost:5173';
const OUT = path.join(__dirname, 'demo-shots');
const fs = require('fs');
fs.mkdirSync(OUT, { recursive: true });

const ROUTES = [
  ['config', '/config'],
  ['dashboard', '/dashboard'],
  ['offline-flash', '/offline-flash'],
  ['online-flash', '/online-flash'],
];

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 920 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)); });
  page.on('pageerror', e => console.log('[pageerror]', String(e).slice(0, 200)));

  for (const [name, route] of ROUTES) {
    await page.goto(`${BASE}/#${route}`, { waitUntil: 'domcontentloaded' });
    // let Vite fetch modules, the app mount, splash removal, and the 3s device-status poll happen
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(1800);
    const splash = await page.locator('#startup-splash').count();
    const bodyText = (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 140);
    await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: false });
    const kb = Math.round(fs.statSync(path.join(OUT, `${name}.png`)).size / 1024);
    console.log(`shot ${name}: ${kb}KB | splash=${splash} | "${bodyText}"`);
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
