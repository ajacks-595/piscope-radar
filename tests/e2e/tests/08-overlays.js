'use strict';
// Overlay / mode toggle controls. weather / day-night / audio / follow / airports / aero
// PERSIST to settings when clicked, so this module is read-only: it asserts the controls
// exist with a valid initial aria-pressed. Actual toggling lives in TEST_PLAN.md (manual,
// click-and-revert). Replay (#toggle-replay) is ephemeral and exercised in 09-replay.js.
const TOGGLES = ['#toggle-audio', '#toggle-follow', '#toggle-weather', '#toggle-day-night',
  '#toggle-heatmap', '#toggle-aero', '#toggle-airports', '#toggle-replay'];

module.exports = {
  name: '08 · Overlay/mode controls (presence)',
  viewport: 'desktop',
  async run(page, t) {
    for (const sel of TOGGLES) {
      const info = await page.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});return e?{vis:e.offsetParent!==null,ap:e.getAttribute('aria-pressed')}:null;})()`);
      t.ok(`${sel} present & visible`, info && info.vis, JSON.stringify(info));
      if (info) t.ok(`${sel} exposes aria-pressed`, info.ap === 'true' || info.ap === 'false', String(info.ap));
    }
    await t.visible('map legend (#map-legend)', page, '#map-legend');
    t.eq('reading control state caused no exceptions', page.exceptions.length, 0);
  },
};
