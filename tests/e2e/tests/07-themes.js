'use strict';
// Apply every theme via applyTheme(name,{persist:false}) — applies locally WITHOUT POSTing
// settings (the change-event path persists; we avoid it). Verifies the data-theme hook,
// the radar sweep canvas, the terminal grid, and that no theme throws.
const THEMES = [
  // PiScope originals
  'midnight', 'light', 'dark', 'radar', 'radarModern', 'terminal', 'tactical', 'sectional',
  'nightVision', 'highContrast',
  // community palettes
  'nord', 'gruvbox', 'dracula', 'tokyoNight', 'catppuccinMocha', 'solarizedDark',
  'solarizedLight', 'synthwave',
];

module.exports = {
  name: '07 · Themes (non-persisting)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    const original = await page.evaluate(`document.documentElement.dataset.theme`);
    // Every theme must define the base palette; a missing block leaves the page on the
    // browser's default colours (transparent panels, unreadable text).
    const PALETTE = `(()=>{const cs=getComputedStyle(document.documentElement);return ['--bg','--panel-bg','--primary-text','--accent','--glass-bg','--accent-text','--map-tint','--map-tone'].filter((k)=>!cs.getPropertyValue(k).trim());})()`;
    // What the tile pane actually renders with under "Automatic": the theme's own tint.
    const TILE = `(()=>{const m=document.getElementById('map');const pane=document.querySelector('.leaflet-tile-pane');return {mode:m.dataset.tileFilter||'', filter:pane?getComputedStyle(pane).filter:'', style:state.settings.map_style||'automatic'};})()`;
    const looks = new Map();
    for (const theme of THEMES) {
      await page.evaluate(`applyTheme(${JSON.stringify(theme)},{persist:false})`);
      await sleep(110);
      t.eq(`theme '${theme}' → <html data-theme>`, await page.evaluate(`document.documentElement.dataset.theme`), theme);
      const missing = await page.evaluate(PALETTE);
      t.ok(`theme '${theme}' resolves its palette tokens`, missing.length === 0, missing.join(','));
      // Fingerprint of the look: accent + how the map is treated. Collected to prove
      // no two themes render the same (only meaningful with the Automatic basemap).
      const tile = await page.evaluate(TILE);
      const accent = await page.evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()`);
      const tone = await page.evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--map-tone').trim()`);
      looks.set(theme, { key: [accent, tile.mode, tile.filter, tone].join('|'), tile });
    }
    const automatic = [...looks.values()][0].tile.style === 'automatic';
    if (automatic) {
      const tinted = ['midnight', 'radar', 'nightVision', 'gruvbox', 'tokyoNight', 'solarizedDark'];
      for (const th of tinted) {
        const l = looks.get(th).tile;
        t.ok(`theme '${th}' tints its basemap`, l.mode === 'theme' && l.filter && l.filter !== 'none', JSON.stringify(l));
      }
    } else {
      t.ok('skipped tint checks — an explicit map style is set (tint applies to Automatic)', true);
    }
    const distinct = new Set([...looks.values()].map((l) => l.key)).size;
    t.eq('every theme has its own accent + map treatment', distinct, THEMES.length);
    // The picker offers exactly the themes exercised here (keep the two lists in step).
    const offered = await page.evaluate(`[...document.getElementById('theme-select').options].map((o)=>o.value)`);
    t.eq('theme picker options match this module', offered.slice().sort().join(','), THEMES.slice().sort().join(','));
    await page.evaluate(`applyTheme('radar',{persist:false})`); await sleep(150);
    t.gt('radar theme renders #radar-canvas', await page.count('#radar-canvas'), 0);
    await page.evaluate(`applyTheme('terminal',{persist:false})`); await sleep(200);
    t.eq('terminal theme applies', await page.evaluate(`document.documentElement.dataset.theme`), 'terminal');
    t.gt('terminal theme draws a canvas (coordinate grid)', await page.count('#map canvas, .coord-grid, canvas'), 0);
    // restore (still persist:false → no settings write)
    await page.evaluate(`applyTheme(${JSON.stringify(original || 'midnight')},{persist:false})`);
    t.eq('no exceptions across all themes', page.exceptions.length, 0);
  },
};
