#!/usr/bin/env node
'use strict';
/*
 * Screenshot driver for PiScope — regenerates the README screenshots and captures
 * theme sheets for design review. Zero dependencies: it drives headless Chromium through
 * the E2E suite's CDP harness (tests/e2e/lib/cdp.js; needs `chromium` on PATH).
 *
 *   python tools/dev/mock_tar1090.py &        # synthetic traffic
 *   python tools/dev/run_dev.py &             # PiScope on http://127.0.0.1:8765/piscope
 *   node tools/dev/screenshots.js docs/screenshots readme      # the README set (JPEG)
 *   node tools/dev/screenshots.js /tmp/shots themes            # every theme, detail open
 *   node tools/dev/screenshots.js /tmp/shots overview layers   # named scenes (PNG)
 *
 * Options:  --ui-font "<family>"   override the UI font stack (headless Linux boxes often
 *                                  only have DejaVu; e.g. --ui-font "Nimbus Sans")
 *           PISCOPE_URL=…          target (default http://127.0.0.1:8765/piscope)
 *
 * Nothing leaves the machine except basemap tile requests: the enrichment endpoints are
 * stubbed in the page (so the detail panel renders "full" without hexdb / adsbdb /
 * planespotters), and the placeholder photo host is answered locally with the drawing
 * below. Point it at the mock feed, not a real receiver, for anything you will publish —
 * the mock's receiver is a placeholder position.
 */
const fs = require('fs');
const path = require('path');
const { Browser, sleep } = require('../../tests/e2e/lib/cdp');

const BASE = process.env.PISCOPE_URL || 'http://127.0.0.1:8765/piscope';
const argv = process.argv.slice(2);
const fontAt = argv.indexOf('--ui-font');
const uiFont = fontAt >= 0 ? argv.splice(fontAt, 2)[1] : null;
const [outDir, ...wanted] = argv;
if (!outDir || !wanted.length) {
  console.error('usage: node tools/dev/screenshots.js <outDir> <scene…|readme|themes> [--ui-font "<family>"]');
  process.exit(2);
}

const DESK = { width: 1600, height: 1000, dsf: 1 };
const LAPTOP = { width: 1280, height: 800, dsf: 1 };
const MOBILE = { width: 390, height: 844, dsf: 2, mobile: true };
const CAST = '407c2e';   // BAW117 in tools/dev/mock_tar1090.py

const select = (hex) => `(()=>{selectAircraft(${JSON.stringify(hex)},{pan:true});return true;})()`;
const click = (sel) => `(()=>{const e=document.querySelector(${JSON.stringify(sel)});if(!e)return false;e.click();return true;})()`;
const openDetail = async (p) => { await p.evaluate(select(CAST)); await sleep(1800); };

// Each scene: { viewport?, url?, theme?, steps?(page), after?, file?, jpeg? }
const SCENES = {
  overview: {},
  detail: { steps: openDetail },
  laptop: { viewport: LAPTOP, steps: openDetail },
  layers: { steps: async (p) => { await p.evaluate(click('#open-layers')); await sleep(500); } },
  themes_popover: { steps: async (p) => { await p.evaluate(click('#open-themes')); await sleep(500); } },
  themes_popover_laptop: { viewport: LAPTOP, steps: async (p) => { await p.evaluate(click('#open-themes')); await sleep(500); } },
  themes_popover_mobile: { viewport: MOBILE, steps: async (p) => { await p.evaluate(click('#open-themes')); await sleep(500); } },
  settings_appearance: { steps: async (p) => { await p.evaluate(click('#open-settings')); await sleep(300); await p.evaluate(click('.tab[data-tab="appearance"]')); await sleep(400); } },
  stats_polar: { steps: async (p) => { await p.evaluate(click('#open-stats')); await sleep(400); await p.evaluate(click('[data-stats-tab="coverage"]')); await sleep(1000); } },
  labels: { url: '#center=51.47,-0.45&zoom=10', steps: async (p) => { await p.evaluate(`(()=>{state.settings.map_label_mode='full';syncLabelPaneAttrs();refreshMapMarkers();syncToggleButtons();return true;})()`); await sleep(900); } },
  mobile: { viewport: MOBILE },
  mobile_detail: { viewport: MOBILE, steps: openDetail },
};
// README set → docs/screenshots/NN-name.jpg
const R = (file, base, extra) => Object.assign({ jpeg: true, file }, SCENES[base] || {}, extra);
const README = {
  readme_01: R('01-overview', 'overview'),
  readme_02: R('02-detail-panel', 'detail'),
  readme_03: R('03-radar-modern', 'overview', { theme: 'radarModern', after: 4200 }),
  readme_04: R('04-terminal', 'detail', { theme: 'terminal' }),
  readme_05: R('05-synthwave', 'detail', { theme: 'synthwave' }),
  readme_06: R('06-nord', 'overview', { theme: 'nord' }),
  readme_07: R('07-stats-polar', 'stats_polar'),
  readme_08: R('08-labels-full', 'labels'),
  readme_09: R('09-mobile', 'mobile_detail'),
  readme_10: R('10-theme-gallery', 'themes_popover'),
  readme_11: R('11-layers', 'layers'),
  readme_12: R('12-night-vision', 'detail', { theme: 'nightVision' }),
  readme_13: R('13-gruvbox', 'detail', { theme: 'gruvbox' }),
  readme_14: R('14-radar-classic', 'overview', { theme: 'radar', after: 5600 }),
  readme_15: R('15-tactical', 'overview', { theme: 'tactical' }),
};
Object.assign(SCENES, README);

const PHOTO_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450" width="800" height="450">
  <defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b6ea8"/><stop offset="0.6" stop-color="#9cc3e6"/><stop offset="1" stop-color="#e9d9c2"/></linearGradient></defs>
  <rect width="800" height="450" fill="url(#sky)"/>
  <ellipse cx="170" cy="120" rx="130" ry="26" fill="#fff" opacity=".55"/><ellipse cx="610" cy="200" rx="170" ry="30" fill="#fff" opacity=".4"/>
  <rect y="372" width="800" height="78" fill="#55606b"/><rect y="372" width="800" height="5" fill="#d8dde2" opacity=".5"/>
  <g fill="#f4f6f8" stroke="#22303f" stroke-width="2.5" stroke-linejoin="round">
    <path d="M118 296 C150 262 250 252 420 252 L640 252 C700 252 740 268 752 286 C740 302 700 312 640 312 L250 312 C190 312 140 306 118 296Z"/>
    <path d="M118 296 L72 196 L112 196 L190 262Z" fill="#1c3f7a"/>
    <path d="M360 300 L250 368 L318 368 L470 304Z" fill="#dfe5ea"/><ellipse cx="392" cy="336" rx="34" ry="15" fill="#c8d0d8"/>
  </g>
  <path d="M690 268 l34 0 l12 12 l-50 0z" fill="#22303f"/><rect x="250" y="272" width="330" height="6" rx="3" fill="#22303f" opacity=".7"/>
</svg>`;

// Runs before any page script: canned enrichment + optional font override.
const INIT = `(() => {
  const font = ${JSON.stringify(uiFont)};
  if (font) document.addEventListener('DOMContentLoaded', () => {
    const st = document.createElement('style');
    st.textContent = ":root{--font-system:'" + font + "',Arial,sans-serif}";
    document.head.appendChild(st);
  });
  const realFetch = window.fetch.bind(window);
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  window.fetch = (input, init) => {
    try {
      const p = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (p.startsWith('/piscope/api/enrich/hexdb/')) return json({ manufacturer: 'Airbus', type: 'A350-1041', registration: 'G-XWBC', registered_owners: 'British Airways' });
      if (p.startsWith('/piscope/api/enrich/photo/')) return json({ photos: [
        { url: 'https://photo.piscope.test/a.svg', photographer: 'Sample photographer', link: '' },
        { url: 'https://photo.piscope.test/b.svg', photographer: 'Sample photographer', link: '' } ] });
      if (p.startsWith('/piscope/api/enrich/adsbdb/')) return json({ origin_icao: 'EGLL', origin_iata: 'LHR', origin_name: 'London Heathrow', origin_lat: 51.4706, origin_lon: -0.461941,
        destination_icao: 'KJFK', destination_iata: 'JFK', destination_name: 'John F Kennedy Intl', destination_lat: 40.6398, destination_lon: -73.7789,
        airline_name: 'British Airways', airline_icao: 'BAW' });
      if (p.startsWith('/piscope/api/analytics/aircraft/')) return json({ hex: 'x', days_seen: 14, first_seen: 1759000000, last_seen: 1762600000, extremes: { max_alt: 41000, max_gs: 512, max_range_nm: 187 } });
      if (p === '/piscope/api/explain/status') return json({ configured: true, provider: { name: 'ollama', model: 'gemma4:latest', reachable: true } });
    } catch (e) { /* fall through to the real fetch */ }
    return realFetch(input, init);
  };
})();`;

(async () => {
  let names = [];
  for (const w of wanted) {
    if (w === 'readme') names.push(...Object.keys(README));
    else if (w === 'themes') names.push('@themes');
    else names.push(w);
  }
  fs.mkdirSync(outDir, { recursive: true });
  const profile = path.join(require('os').tmpdir(), 'piscope-shots-profile');
  fs.rmSync(profile, { recursive: true, force: true });   // fresh: no service worker / cache carried over
  const browser = await Browser.launch({ port: 9333, profile });
  const page = browser.page;
  await page.cdp('Page.addScriptToEvaluateOnNewDocument', { source: INIT });
  await page.cdp('Fetch.enable', { patterns: [{ urlPattern: 'https://photo.piscope.test/*' }] });
  const onMsg = page.ws.onmessage;
  page.ws.onmessage = (m) => {
    try {
      const o = JSON.parse(m);
      if (o.method === 'Fetch.requestPaused') {
        page.cdp('Fetch.fulfillRequest', { requestId: o.params.requestId, responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'image/svg+xml' }], body: Buffer.from(PHOTO_SVG).toString('base64') }).catch(() => {});
        return;
      }
    } catch (e) { /* not JSON we care about */ }
    onMsg(m);
  };

  const boot = async (sc) => {
    await page.setViewport(sc.viewport || DESK);
    await page.navigate('about:blank'); await sleep(120);
    page.clearLogs();
    await page.navigate(BASE + (sc.url || ''));
    await page.waitFor('window.__piscopeReady===true', { timeout: 25000 }).catch(() => console.error('boot timeout'));
    await sleep(2400);
  };
  const shoot = async (name, sc) => {
    const shot = await page.cdp('Page.captureScreenshot', sc.jpeg ? { format: 'jpeg', quality: 86 } : { format: 'png' });
    const file = path.join(outDir, (sc.file || name) + (sc.jpeg ? '.jpg' : '.png'));
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    const errs = [...page.exceptions, ...page.consoleErrors];
    console.log(`${name}: ${file}${errs.length ? '  !! ' + errs.slice(0, 3).join(' | ') : ''}`);
  };

  if (names.includes('@themes')) {
    // One load, then every theme in turn with the detail panel open (laptop size).
    names = names.filter((n) => n !== '@themes');
    await boot({ viewport: LAPTOP });
    await openDetail(page);
    const themes = await page.evaluate(`[...document.getElementById('theme-select').options].map((o)=>o.value)`);
    for (const th of themes) {
      await page.evaluate(`(()=>{applyTheme(${JSON.stringify(th)},{persist:false});return true;})()`);
      await sleep(th === 'radar' ? 5600 : th === 'radarModern' ? 3600 : 1600);
      await shoot('theme_' + th, {});
    }
  }
  for (const name of names) {
    const sc = SCENES[name];
    if (!sc) { console.error('unknown scene:', name, '— have:', Object.keys(SCENES).join(' ')); continue; }
    await boot(sc);
    if (sc.theme) { await page.evaluate(`(()=>{applyTheme(${JSON.stringify(sc.theme)},{persist:false});return true;})()`); await sleep(1400); }
    if (sc.steps) { try { await sc.steps(page); } catch (e) { console.error(name, 'steps failed:', e.message); } }
    await sleep(sc.after || 900);
    await shoot(name, sc);
  }
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error('SCREENSHOT ERROR', e.stack || e.message); process.exit(3); });
