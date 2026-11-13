#!/usr/bin/env node
'use strict';
/*
 * PiScope icon + aircraft-silhouette generator.
 *
 * Everything PiScope draws as an icon is original geometry defined in this file (simple
 * strokes on a 24px grid; silhouettes on a 32px grid), so the project ships no
 * third-party icon set and needs no build step:
 *
 *   - the UI icons become an inline <svg> sprite in static/index.html, referenced as
 *     <svg class="icon"><use href="#i-NAME"/></svg> (in markup) or icon('NAME') (app.js);
 *   - the aircraft silhouettes become AIRCRAFT_SHAPES in static/app.js and
 *     static/icons/aircraft.svg (the list-row glyph mask / notification icon).
 *
 * Usage (from the repo root; plain node, no dependencies):
 *
 *   node tools/gen-icons.js            rewrite the sprite in static/index.html and
 *                                      static/icons/aircraft.svg, print AIRCRAFT_SHAPES
 *   node tools/gen-icons.js --check    exit 1 if the sprite in index.html is out of date
 *   node tools/gen-icons.js --sheet F  also write a contact sheet (HTML) to F for review
 *
 * Adding an icon: add an entry to ICONS, run the script, use it. tests/test_ui_shell.py
 * fails if markup or app.js references an icon that the sprite does not define.
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const INDEX = path.join(ROOT, 'static', 'index.html');
const AIRCRAFT_SVG = path.join(ROOT, 'static', 'icons', 'aircraft.svg');

const r2 = (n) => Math.round(n * 100) / 100;

// ---- programmatic bits -----------------------------------------------------
function gear(teeth = 8, rOut = 9.4, rIn = 7.1, cx = 12, cy = 12) {
  const pts = [];
  const step = (Math.PI * 2) / teeth;
  for (let i = 0; i < teeth; i++) {
    const a = i * step - Math.PI / 2;
    const w1 = step * 0.17, w2 = step * 0.30;   // tooth top half-width / base half-width
    pts.push([rIn, a - w2], [rOut, a - w1], [rOut, a + w1], [rIn, a + w2]);
  }
  return 'M' + pts.map(([r, a]) => `${r2(cx + r * Math.cos(a))} ${r2(cy + r * Math.sin(a))}`).join('L') + 'Z';
}
function star(points = 5, rOut = 8.8, rIn = 3.9, cx = 12, cy = 12.4) {
  const pts = [];
  for (let i = 0; i < points * 2; i++) {
    const r = i % 2 === 0 ? rOut : rIn;
    const a = (i * Math.PI) / points - Math.PI / 2;
    pts.push(`${r2(cx + r * Math.cos(a))} ${r2(cy + r * Math.sin(a))}`);
  }
  return 'M' + pts.join('L') + 'Z';
}

// ---- UI icons (24x24, stroke) ----------------------------------------------
const F = 'fill="currentColor" stroke="none"';
const ICONS = {
  logo: `<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.6" opacity=".55"/><path d="M12 12L18.4 5.6"/><circle cx="12" cy="12" r="1.5" ${F}/><circle cx="16.6" cy="15.6" r="1.25" ${F}/>`,
  search: `<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>`,
  sliders: `<path d="M4 7h9M19 7h1M4 12h1M11 12h9M4 17h7M17 17h3"/><circle cx="16" cy="7" r="2.4"/><circle cx="8" cy="12" r="2.4"/><circle cx="14" cy="17" r="2.4"/>`,
  plus: `<path d="M12 5.5v13M5.5 12h13"/>`,
  minus: `<path d="M5.5 12h13"/>`,
  x: `<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>`,
  'chevron-right': `<path d="M9.5 6.5L15 12l-5.5 5.5"/>`,
  'chevron-left': `<path d="M14.5 6.5L9 12l5.5 5.5"/>`,
  'arrow-up': `<path d="M12 19.5v-15M6 10.5l6-6 6 6"/>`,
  layers: `<path d="M12 3.5l8.5 4.6-8.5 4.6-8.5-4.6L12 3.5z"/><path d="M3.5 12.4l8.5 4.6 8.5-4.6"/><path d="M3.5 16.6l8.5 4.6 8.5-4.6"/>`,
  fit: `<path d="M4.5 9.5v-5h5M19.5 9.5v-5h-5M4.5 14.5v5h5M19.5 14.5v5h-5"/>`,
  locate: `<circle cx="12" cy="12" r="6.2"/><circle cx="12" cy="12" r="1.7" ${F}/><path d="M12 2.5v3.3M12 18.2v3.3M2.5 12h3.3M18.2 12h3.3"/>`,
  follow: `<path d="M20 4L4.8 10.2l6.3 2.7 2.7 6.3L20 4z"/>`,
  bell: `<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.6 2.2H4.9l1.6-2.2z"/><path d="M10 21a2.2 2.2 0 0 0 4 0"/>`,
  chart: `<path d="M4.5 4.5v15h15"/><path d="M9 15.5v-4M13 15.5v-8M17 15.5v-6"/>`,
  bookmark: `<path d="M7 4.5h10a1 1 0 0 1 1 1v14l-6-3.8-6 3.8v-14a1 1 0 0 1 1-1z"/>`,
  star: `<path d="${star()}"/>`,
  'star-fill': `<path d="${star()}" fill="currentColor"/>`,
  history: `<path d="M4 12a8 8 0 1 0 8-8 8.6 8.6 0 0 0-5.9 2.4L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M12 8v4.4l2.9 1.8"/>`,
  volume: `<path d="M4 9.6v4.8h3.4l4.6 4V5.6l-4.6 4H4z"/><path d="M15.5 9.2a4 4 0 0 1 0 5.6M18.2 6.6a7.6 7.6 0 0 1 0 10.8"/>`,
  'volume-off': `<path d="M4 9.6v4.8h3.4l4.6 4V5.6l-4.6 4H4z"/><path d="M16 9.6l4.8 4.8M20.8 9.6L16 14.4"/>`,
  theme: `<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" ${F}/>`,
  settings: `<path d="${gear()}"/><circle cx="12" cy="12" r="2.9"/>`,
  keyboard: `<rect x="2.8" y="6" width="18.4" height="12" rx="2.4"/><path d="M6.5 10h.01M10.2 10h.01M13.8 10h.01M17.5 10h.01M6.5 13.6h.01M17.5 13.6h.01M9.6 13.9h4.8"/>`,
  plane: `<path d="M12 2.8c.9 0 1.5 1.3 1.5 3v3.4l7 4.3v2l-7-2.1v4l2 1.6v1.6L12 19.7l-3.5.9V19l2-1.6v-4l-7 2.1v-2l7-4.3V5.8c0-1.7.6-3 1.5-3z"/>`,
  pin: `<path d="M12 21s-6.5-5.7-6.5-10.6a6.5 6.5 0 0 1 13 0C18.5 15.3 12 21 12 21z"/><circle cx="12" cy="10.4" r="2.3"/>`,
  airport: `<path d="M5 20.5l2.2-17h9.6l2.2 17"/><path d="M12 6.5v2.4M12 12v2.4M12 17.5v2.6"/>`,
  airspace: `<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><path d="M12 3.5v4.5M12 16v4.5M3.5 12H8M16 12h4.5"/>`,
  weather: `<path d="M7.2 16.5a4 4 0 0 1 .5-7.97 5.3 5.3 0 0 1 10.3 1.3 3.4 3.4 0 0 1-.6 6.67"/><path d="M9 18.6l-.9 2.4M12.7 18.6l-.9 2.4M16.4 18.6l-.9 2.4"/>`,
  heat: `<path d="M12 3.2c.6 3-1.3 4.4-2.8 6.2-1.4 1.7-2.5 3.4-2.5 5.6a5.3 5.3 0 0 0 10.6 0c0-2.4-1.3-3.9-2.4-5-.3 1.4-.9 2.2-1.9 2.7.5-3.2-.3-6.4-1-9.5z"/>`,
  moon: `<path d="M19.5 14.2A8 8 0 0 1 9.8 4.5a8 8 0 1 0 9.7 9.7z"/>`,
  rings: `<circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.4" ${F}/>`,
  external: `<path d="M14 4.8h5.2V10"/><path d="M19.2 4.8l-8 8"/><path d="M17.5 14v4a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V8A1.5 1.5 0 0 1 6 6.5h4"/>`,
  sparkles: `<path d="M11 4.5l1.7 4.6 4.6 1.7-4.6 1.7L11 17.1l-1.7-4.6-4.6-1.7 4.6-1.7L11 4.5z"/><path d="M18 15.2v4.2M15.9 17.3h4.2"/>`,
  panel: `<rect x="3.5" y="4.5" width="17" height="15" rx="2.6"/><path d="M9.5 4.5v15"/>`,
  more: `<circle cx="5.5" cy="12" r="1.5" ${F}/><circle cx="12" cy="12" r="1.5" ${F}/><circle cx="18.5" cy="12" r="1.5" ${F}/>`,
  download: `<path d="M12 4.5v10.5M7.5 10.8L12 15.3l4.5-4.5M5 19.5h14"/>`,
  upload: `<path d="M12 15.5V5M7.5 9.3L12 4.8l4.5 4.5M5 19.5h14"/>`,
  alert: `<path d="M12 4.2l8.6 15H3.4L12 4.2z"/><path d="M12 10v4.2"/><circle cx="12" cy="16.6" r=".9" ${F}/>`,
  antenna: `<path d="M12 12.2V21"/><circle cx="12" cy="10" r="1.7"/><path d="M8.3 13.7a5.3 5.3 0 0 1 0-7.4M15.7 6.3a5.3 5.3 0 0 1 0 7.4M5.5 16.5a9.2 9.2 0 0 1 0-13M18.5 3.5a9.2 9.2 0 0 1 0 13"/>`,
  link: `<path d="M10.2 13.8a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.1 1.1"/><path d="M13.8 10.2a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.1-1.1"/>`,
  live: `<circle cx="12" cy="12" r="2.6" ${F}/><path d="M7.4 16.6a6.5 6.5 0 0 1 0-9.2M16.6 7.4a6.5 6.5 0 0 1 0 9.2M4.6 19.4a10.5 10.5 0 0 1 0-14.8M19.4 4.6a10.5 10.5 0 0 1 0 14.8"/>`,
  route: `<circle cx="6" cy="18" r="2.4"/><circle cx="18" cy="6" r="2.4"/><path d="M8.4 18H14a3.5 3.5 0 0 0 0-7h-4a3.5 3.5 0 0 1 0-7h5.6"/>`,
  image: `<rect x="3.5" y="5" width="17" height="14" rx="2.4"/><circle cx="8.8" cy="10" r="1.6"/><path d="M4.5 17.5l5-4.6 3.4 3 2.6-2.3 4 3.6"/>`,
  mail: `<rect x="3.5" y="5.5" width="17" height="13" rx="2.4"/><path d="M4.5 7.5l7.5 5.6 7.5-5.6"/>`,
  map: `<path d="M9 5L3.5 7v12L9 17l6 2 5.5-2V5L15 7 9 5z"/><path d="M9 5v12M15 7v12"/>`,
};

// ---- aircraft silhouettes (32x32, pointing north, filled) -------------------
// Defined as the right-hand half outline from nose to tail; mirrored for the left.
function mirrorPath(half) {
  // half: array of [cmd, ...coords] for the right side, nose (16,y) -> tail (16,y)
  const fwd = half.map(([c, ...v]) => c + v.map(r2).join(' ')).join('');
  // Build the mirrored return path: reverse segments, x -> 32-x.
  const pts = [];   // list of {cmd, pts:[[x,y]...]} with absolute coords
  for (const [c, ...v] of half) {
    const p = [];
    for (let i = 0; i < v.length; i += 2) p.push([v[i], v[i + 1]]);
    pts.push({ c, p });
  }
  let back = '';
  for (let i = pts.length - 1; i >= 1; i--) {
    const seg = pts[i];
    const prevEnd = pts[i - 1].p[pts[i - 1].p.length - 1];
    const m = ([x, y]) => `${r2(32 - x)} ${r2(y)}`;
    if (seg.c === 'L') back += 'L' + m(prevEnd);
    else if (seg.c === 'C') back += 'C' + m(seg.p[1]) + ' ' + m(seg.p[0]) + ' ' + m(prevEnd);
    else if (seg.c === 'Q') back += 'Q' + m(seg.p[0]) + ' ' + m(prevEnd);
  }
  return fwd + back + 'Z';
}

const SHAPES = {
  // Twin-engine airliner.
  jet: mirrorPath([
    ['M', 16, 1.6],
    ['C', 17.2, 1.6, 17.9, 3.9, 17.9, 6.4],
    ['L', 17.9, 11.3],
    ['L', 20.4, 12.9], ['L', 20.4, 11.6], ['C', 20.4, 10.9, 22.2, 10.9, 22.2, 11.6], ['L', 22.2, 14.1],
    ['L', 29.9, 19.1],
    ['C', 30.6, 19.6, 30.5, 21.2, 29.6, 20.9],
    ['L', 17.9, 17.9],
    ['L', 17.5, 24.4],
    ['L', 22.3, 27.9],
    ['C', 22.9, 28.4, 22.7, 29.7, 21.9, 29.5],
    ['L', 16.9, 28.2],
    ['L', 16, 30.4],
  ]),
  // Four-engine / wide-body: longer span, two nacelles per wing.
  heavy: mirrorPath([
    ['M', 16, 1],
    ['C', 17.5, 1, 18.3, 3.6, 18.3, 6.4],
    ['L', 18.3, 10.6],
    ['L', 20.1, 11.7], ['L', 20.1, 10.3], ['C', 20.1, 9.6, 21.9, 9.6, 21.9, 10.3], ['L', 21.9, 12.8],
    ['L', 24.5, 14.4], ['L', 24.5, 13.1], ['C', 24.5, 12.4, 26.2, 12.4, 26.2, 13.1], ['L', 26.2, 15.5],
    ['L', 30.9, 18.5],
    ['C', 31.6, 19, 31.5, 20.7, 30.6, 20.4],
    ['L', 18.3, 17.6],
    ['L', 17.8, 24.4],
    ['L', 23.3, 28.2],
    ['C', 23.9, 28.7, 23.7, 30, 22.9, 29.8],
    ['L', 17, 28.4],
    ['L', 16, 31],
  ]),
  // Light single / GA: straight high-aspect wing set forward, small tail.
  light: mirrorPath([
    ['M', 16, 3.4],
    ['C', 17.3, 3.4, 17.6, 5.4, 17.6, 7.4],
    ['L', 17.6, 10.2],
    ['L', 29.6, 10.6],
    ['C', 30.6, 10.7, 30.6, 13.9, 29.6, 14],
    ['L', 17.5, 14.6],
    ['L', 16.9, 23.6],
    ['L', 21.4, 24.2],
    ['C', 22.2, 24.3, 22.2, 26.6, 21.4, 26.7],
    ['L', 16.7, 27.1],
    ['L', 16, 28.8],
  ]),
  // Fast jet: delta wing.
  fast: mirrorPath([
    ['M', 16, 1.4],
    ['C', 17, 1.4, 17.5, 4.4, 17.6, 7.6],
    ['L', 18.4, 12.6],
    ['L', 28.6, 22.4],
    ['C', 29.3, 23.1, 29, 24.6, 28.2, 24.6],
    ['L', 19.2, 24.2],
    ['L', 20.9, 28.6],
    ['C', 21.2, 29.4, 20.6, 30.2, 19.9, 30],
    ['L', 16.9, 28.9],
    ['L', 16, 30.2],
  ]),
};
// Helicopter: fuselage + tail boom (mirrored) with a rotor disc drawn separately.
SHAPES.heli = mirrorPath([
  ['M', 16, 6.2],
  ['C', 19.4, 6.2, 20.3, 9.6, 20.3, 12.6],
  ['C', 20.3, 16.2, 18.4, 18.2, 17.1, 19],
  ['L', 16.8, 26.2],
  ['L', 19.9, 26.6],
  ['C', 20.6, 26.7, 20.6, 28.6, 19.9, 28.7],
  ['L', 16.6, 29],
  ['L', 16, 29.6],
]);
const HELI_ROTOR = 'M16 12.4L27 4.2M16 12.4L5 4.2M16 12.4L27 20.6M16 12.4L5 20.6';


// ---- outputs ---------------------------------------------------------------
function buildSprite() {
  const lines = ['<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" aria-hidden="true" focusable="false">'];
  for (const [name, inner] of Object.entries(ICONS)) {
    lines.push(`    <symbol id="i-${name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${inner}</symbol>`);
  }
  lines.push('  </svg>');
  return lines.join('\n');
}

const SPRITE_RE = /<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" class="icon-sprite"[\s\S]*?<\/svg>/;
const args = process.argv.slice(2);
const sprite = buildSprite();
const html = fs.readFileSync(INDEX, 'utf8');
if (!SPRITE_RE.test(html)) {
  console.error('icon sprite block not found in static/index.html');
  process.exit(2);
}

if (args.includes('--check')) {
  const current = html.match(SPRITE_RE)[0];
  if (current !== sprite) {
    console.error('static/index.html icon sprite is out of date — run: node tools/gen-icons.js');
    process.exit(1);
  }
  console.log(`icon sprite up to date (${Object.keys(ICONS).length} icons)`);
  process.exit(0);
}

fs.writeFileSync(INDEX, html.replace(SPRITE_RE, () => sprite));
fs.writeFileSync(AIRCRAFT_SVG,
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">\n  <path d="${SHAPES.jet}" />\n</svg>\n`);
console.log(`wrote ${Object.keys(ICONS).length} icons to static/index.html and static/icons/aircraft.svg`);
console.log('\nAIRCRAFT_SHAPES path data for static/app.js (paste only if a silhouette changed):');
for (const [name, d] of Object.entries(SHAPES)) console.log(`  ${name}: '${d}'`);
console.log(`  heli rotor: '${HELI_ROTOR}'`);

const sheetAt = args.indexOf('--sheet');
if (sheetAt >= 0 && args[sheetAt + 1]) {
  const cell = (name) => `<div class="c"><svg width="28" height="28"><use href="#i-${name}"/></svg><span>${name}</span></div>`;
  const plane = (name, size) => `<div class="c"><svg width="${size}" height="${size}" viewBox="0 0 32 32">${name === 'heli' ? `<path d="${HELI_ROTOR}" fill="none" stroke="#3ddcb0" stroke-width="2.2" stroke-linecap="round"/>` : ''}<path d="${SHAPES[name]}" fill="#3ddcb0" stroke="rgba(0,0,0,.65)" stroke-width="1.5" stroke-linejoin="round" paint-order="stroke"/></svg><span>${name}</span></div>`;
  fs.writeFileSync(args[sheetAt + 1], `<!doctype html><meta charset="utf-8"><style>
 body{margin:0;padding:24px;font:12px system-ui,sans-serif;background:#0d1320;color:#dfe6f2}
 .g{display:grid;grid-template-columns:repeat(10,1fr);gap:14px;margin-bottom:28px}
 .c{display:flex;flex-direction:column;align-items:center;gap:6px;padding:10px 4px;border-radius:10px;background:#141c2c}
 .c span{font-size:10px;color:#8fa0b8} .icon-sprite{position:absolute;width:0;height:0}
</style>${sprite}
<div class="g">${Object.keys(ICONS).map(cell).join('')}</div>
<div class="g">${Object.keys(SHAPES).map((n) => plane(n, 44)).join('')}${Object.keys(SHAPES).map((n) => plane(n, 22)).join('')}</div>`);
  console.log('contact sheet:', args[sheetAt + 1]);
}
