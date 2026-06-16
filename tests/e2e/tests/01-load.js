'use strict';
// Page boot: the shell renders, core read endpoints answer, and nothing throws on load.
module.exports = {
  name: '01 · Page load & shell',
  async run(page, t) {
    t.eq('document.title', await page.evaluate('document.title'), 'PiScope Radar');
    await t.visible('Leaflet map container (#map)', page, '#map');
    t.gt('Leaflet base tiles rendered', await page.count('.leaflet-tile'), 0);
    await t.visible('sidebar (#sidebar)', page, '#sidebar');
    await t.visible('topbar settings button (#open-settings)', page, '#open-settings');
    t.ok('aircraft-count element present', (await page.count('#aircraft-count')) > 0);

    const v = await page.evaluate(`fetch('/piscope/api/version').then(r=>r.json()).catch(()=>null)`);
    t.ok('GET /api/version → {version}', !!(v && v.version), JSON.stringify(v));
    const h = await page.evaluate(`fetch('/piscope/health').then(r=>r.json()).catch(e=>({err:String(e)}))`);
    t.ok('GET /piscope/health → has status field', !!(h && h.status), JSON.stringify(h));

    t.eq('no uncaught JS exceptions during load', page.exceptions.length, 0);
  },
};
