'use strict';
// Apply every theme via applyTheme(name,{persist:false}) — applies locally WITHOUT POSTing
// settings (the change-event path persists; we avoid it). Verifies the data-theme hook,
// the radar sweep canvas, the terminal grid, and that no theme throws.
const THEMES = ['radar', 'radarModern', 'terminal', 'dark', 'light', 'tactical', 'sectional',
  'solarizedDark', 'solarizedLight', 'nord', 'synthwave', 'dracula', 'tokyoNight', 'catppuccinMocha'];

module.exports = {
  name: '07 · Themes (non-persisting)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    const original = await page.evaluate(`document.documentElement.dataset.theme`);
    for (const theme of THEMES) {
      await page.evaluate(`applyTheme(${JSON.stringify(theme)},{persist:false})`);
      await sleep(110);
      t.eq(`theme '${theme}' → <html data-theme>`, await page.evaluate(`document.documentElement.dataset.theme`), theme);
    }
    await page.evaluate(`applyTheme('radar',{persist:false})`); await sleep(150);
    t.gt('radar theme renders #radar-canvas', await page.count('#radar-canvas'), 0);
    await page.evaluate(`applyTheme('terminal',{persist:false})`); await sleep(200);
    t.eq('terminal theme applies', await page.evaluate(`document.documentElement.dataset.theme`), 'terminal');
    t.gt('terminal theme draws a canvas (coordinate grid)', await page.count('#map canvas, .coord-grid, canvas'), 0);
    // restore (still persist:false → no settings write)
    await page.evaluate(`applyTheme(${JSON.stringify(original || 'radar')},{persist:false})`);
    t.eq('no exceptions across all themes', page.exceptions.length, 0);
  },
};
