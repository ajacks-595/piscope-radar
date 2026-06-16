'use strict';
// Embed mode (?embed=1, query params not hash). Chrome is stripped, the map stays, and
// interactive=locked freezes the Leaflet handlers.
module.exports = {
  name: '11 · Embed mode',
  url: '?embed=1&interactive=locked&center=51.5,-0.1&zoom=8',
  async run(page, t) {
    t.eq('<html data-embed="1">', await page.evaluate(`document.documentElement.getAttribute('data-embed')`), '1');
    t.ok('sidebar hidden in embed', !(await page.visible('#sidebar')));
    t.ok('detail content hidden in embed', !(await page.visible('#detail-content')));
    await t.visible('map still present in embed', page, '#map');

    const locked = await page.evaluate(`(typeof map!=='undefined'&&map&&map.dragging)?(!map.dragging.enabled()):null`);
    t.ok('interactive=locked disables map dragging', locked === true, String(locked));
    t.gt('embed expand link present', await page.count('#embed-expand-link'), 0);

    t.eq('no exceptions in embed mode', page.exceptions.length, 0);
  },
};
