'use strict';
// Overlay / mode toggle controls. weather / day-night / audio / follow / airports / aero /
// range-rings PERSIST to settings when clicked, so this module is read-only: it asserts the
// controls exist with a valid initial aria-pressed. Actual toggling lives in TEST_PLAN.md
// (manual, click-and-revert). Replay (#toggle-replay) is ephemeral and exercised in
// 09-replay.js.
//
// Since 1.8.0 the map overlays sit in the Layers popover (opened from the map rail), so it
// is opened first — opening a popover is ephemeral — and closed again afterwards.
const ALWAYS = ['#toggle-audio', '#toggle-follow', '#toggle-replay', '#open-layers', '#zoom-in', '#zoom-out', '#fit-data', '#recenter'];
const IN_LAYERS = ['#toggle-airports', '#toggle-aero', '#toggle-weather', '#toggle-heatmap', '#toggle-day-night', '#toggle-rings'];
const STATE = (sel) => `(()=>{const e=document.querySelector(${JSON.stringify(sel)});return e?{vis:e.offsetParent!==null,ap:e.getAttribute('aria-pressed')}:null;})()`;

module.exports = {
  name: '08 · Overlay/mode controls (presence)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    for (const sel of ALWAYS) {
      const info = await page.evaluate(STATE(sel));
      t.ok(`${sel} present & visible`, info && info.vis, JSON.stringify(info));
      if (info && sel.startsWith('#toggle-')) t.ok(`${sel} exposes aria-pressed`, info.ap === 'true' || info.ap === 'false', String(info.ap));
    }

    t.ok('layers popover starts closed', !(await page.open('#layers-popover')));
    await page.click('#open-layers'); await sleep(200);
    t.ok('layers popover opens from the map rail', await page.open('#layers-popover'));
    for (const sel of IN_LAYERS) {
      const info = await page.evaluate(STATE(sel));
      t.ok(`${sel} present & visible in the layers popover`, info && info.vis, JSON.stringify(info));
      if (info) t.ok(`${sel} exposes aria-pressed`, info.ap === 'true' || info.ap === 'false', String(info.ap));
    }
    const mode = await page.evaluate(`(()=>{const on=[...document.querySelectorAll('#label-mode [data-label-mode]')].filter((b)=>b.getAttribute('aria-pressed')==='true').map((b)=>b.dataset.labelMode);return {on, setting: state.settings.map_label_mode || 'off'};})()`);
    t.ok('label-mode control marks exactly the stored mode', mode.on.length === 1 && mode.on[0] === mode.setting, JSON.stringify(mode));
    // The badge on the rail counts the overlays that are on (range rings excluded).
    const badge = await page.evaluate(`(()=>{const ids=['toggle-airports','toggle-aero','toggle-weather','toggle-heatmap','toggle-day-night'];const n=ids.filter((id)=>document.getElementById(id).getAttribute('aria-pressed')==='true').length;const b=document.getElementById('layers-badge');return {n, hidden:b.hidden, text:b.textContent};})()`);
    t.ok('layers badge matches the number of active overlays', badge.n === 0 ? badge.hidden : (!badge.hidden && badge.text === String(badge.n)), JSON.stringify(badge));
    await page.press('Escape'); await sleep(150);
    t.ok('Esc closes the layers popover', !(await page.open('#layers-popover')));

    await t.visible('map legend (#map-legend)', page, '#map-legend');
    t.eq('reading control state caused no exceptions', page.exceptions.length, 0);
  },
};
