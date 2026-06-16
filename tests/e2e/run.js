'use strict';
/*
 * PiScope browser E2E runner.  Drives the DEPLOYED site in headless Chromium via the
 * zero-dep CDP harness (lib/cdp.js) and runs every module under tests/.
 *
 *   node tests/e2e/run.js                       # against http://10.0.0.231/piscope
 *   PISCOPE_URL=http://host/piscope node tests/e2e/run.js
 *   node tests/e2e/run.js --only=detail         # filter modules by file/name substring
 *   node tests/e2e/run.js --headed              # show the browser
 *   node tests/e2e/run.js --include-destructive # also run modules flagged destructive (none ship by default)
 *
 * NON-DESTRUCTIVE: read-only + ephemeral interactions only. A settings snapshot is taken
 * before the run and re-checked after — if any module accidentally persisted a change the
 * run FAILS. Modules never click the persisting toggles (theme-select/weather/day-night/
 * audio/follow) or hit write/AI/FA/webhook endpoints.
 */
const fs = require('fs');
const path = require('path');
const { Browser, sleep } = require('./lib/cdp');
const { Checks, report } = require('./lib/harness');

const BASE = process.env.PISCOPE_URL || 'http://10.0.0.231/piscope';
const argv = process.argv.slice(2);
const includeDestructive = argv.includes('--include-destructive');
const headed = argv.includes('--headed');
const only = (argv.find((a) => a.startsWith('--only=')) || '').split('=')[1];

const VIEWPORTS = { desktop: { width: 1280, height: 900, mobile: false }, mobile: { width: 390, height: 844, mobile: true, dsf: 2 } };
// "Booted" = the app has rendered rows OR (empty skies) the Leaflet map object exists.
const BOOT = `(()=>{try{return (document.querySelectorAll('#aircraft-list .aircraft-row').length>0)||(typeof map!=='undefined'&&!!map);}catch(e){return false;}})()`;

async function boot(page, suffix = '') {
  // about:blank first → guarantees a full cross-document load even when the only change is a
  // #fragment (Chrome treats fragment-only navigations as same-document and won't re-run the
  // app's init / applyUrlState). Also gives every module a clean, isolated page.
  await page.navigate('about:blank');
  await sleep(150);
  await page.navigate(BASE + suffix);
  await page.waitFor(BOOT, { timeout: 25000 });
  await sleep(400);
}

(async () => {
  const dir = path.join(__dirname, 'tests');
  let mods = fs.readdirSync(dir).filter((f) => f.endsWith('.js')).sort().map((f) => ({ file: f, mod: require(path.join(dir, f)) }));
  if (only) mods = mods.filter((m) => m.file.includes(only) || (m.mod.name || '').toLowerCase().includes(only.toLowerCase()));

  const browser = await Browser.launch({ headless: !headed });
  const page = browser.page;
  const results = [];

  // --- non-destructive guard: snapshot settings before anything runs ---
  await boot(page);
  let before = null;
  try { before = await page.evaluate(`fetch('/piscope/api/settings').then(r=>r.json())`); } catch (e) {}

  for (const { file, mod } of mods) {
    const mr = { name: mod.name || file };
    if (mod.destructive && !includeDestructive) { mr.skipped = 'destructive — pass --include-destructive'; results.push(mr); continue; }
    const t = new Checks(mr.name);
    mr.checks = t; // assign first so partial results survive a mid-run throw
    try {
      page.clearLogs(); // clear BEFORE boot so this module owns its load-time console errors
      await boot(page, mod.url || '');
      if (mod.viewport && VIEWPORTS[mod.viewport]) { await page.setViewport(VIEWPORTS[mod.viewport]); await sleep(200); }
      await mod.run(page, t, { base: BASE, sleep, VIEWPORTS });
    } catch (e) { t.ok('module ran to completion without throwing', false, e.message); }
    mr.consoleErrors = [...page.consoleErrors]; mr.exceptions = [...page.exceptions]; mr.logErrors = [...page.logErrors];
    results.push(mr);
  }

  // --- non-destructive guard: settings must be byte-identical afterward ---
  const guard = new Checks('Non-destructive guard');
  try {
    await boot(page);
    const after = await page.evaluate(`fetch('/piscope/api/settings').then(r=>r.json())`);
    guard.ok('settings snapshot captured before run', !!before);
    guard.eq('GET /api/settings unchanged after full suite (no accidental writes)', JSON.stringify(after), JSON.stringify(before));
  } catch (e) { guard.ok('settings guard executed', false, e.message); }
  results.push({ name: guard.moduleName, checks: guard, consoleErrors: [], exceptions: [], logErrors: [] });

  const logs = {
    consoleErrors: results.flatMap((r) => r.consoleErrors || []),
    exceptions: results.flatMap((r) => r.exceptions || []),
    logErrors: results.flatMap((r) => r.logErrors || []),
  };
  const rep = report(results, logs);
  console.log('\n' + rep.text + '\n');
  fs.writeFileSync('/tmp/piscope-e2e-report.json',
    JSON.stringify({ base: BASE, when: process.env.E2E_STAMP || null, results: results.map((r) => ({ name: r.name, skipped: r.skipped, error: r.error, rows: r.checks ? r.checks.rows : null })), ...logs }, null, 2));
  await browser.close();
  process.exit(rep.totFail > 0 || rep.exceptions > 0 ? 1 : 0);
})().catch((e) => { console.error('RUNNER ERROR:', e.stack || e.message); process.exit(3); });
