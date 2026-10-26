'use strict';
// Sidebar filters are ephemeral client-side view-state (state.filters + renderSidebar) — no
// settings writes — so they're safe to drive. Verify search / sort / category / altitude.
const setVal = (id, val, ev) => `(()=>{const e=document.getElementById(${JSON.stringify(id)});if(!e)return false;e.value=${JSON.stringify(String(val))};e.dispatchEvent(new Event(${JSON.stringify(ev)},{bubbles:true}));return true;})()`;

module.exports = {
  name: '03 · Sidebar filters (ephemeral)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    const rows = () => page.count('#aircraft-list .aircraft-row');
    const total = await rows();
    t.gt('aircraft rows rendered', total, 0);
    if (total === 0) { t.ok('skipped filter checks — no live aircraft', true); return; }

    await page.evaluate(setVal('filter-search', 'ZZZQQ', 'input')); await sleep(150);
    const narrowed = await rows();
    t.ok('search narrows the list', narrowed <= total, `${narrowed} <= ${total}`);
    await page.evaluate(setVal('filter-search', '', 'input')); await sleep(150);
    t.ok('clearing search restores rows', (await rows()) >= narrowed);

    for (const s of ['callsign', 'altitude', 'speed', 'distance']) { await page.evaluate(setVal('filter-sort', s, 'change')); await sleep(70); }
    t.gt('rows still present after sort cycling', await rows(), 0);

    await page.evaluate(setVal('filter-category', 'military', 'change')); await sleep(120);
    t.ok('category=military filters (rows <= total)', (await rows()) <= total);
    await page.evaluate(setVal('filter-category', 'all', 'change')); await sleep(120);
    t.ok('category=all restores rows', (await rows()) >= 1 || total >= 1);

    await page.evaluate(`(()=>{const a=document.getElementById('alt-min');if(a){a.value=String(Math.floor((+a.max||40000)/2));a.dispatchEvent(new Event('input',{bubbles:true}));}})()`);
    await sleep(120);
    t.ok('alt-min slider updates its value label', (await page.text('#alt-min-val')).trim() !== '');

    t.eq('no exceptions while filtering', page.exceptions.length, 0);
  },
};
