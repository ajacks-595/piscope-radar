'use strict';
// Share-link round-trip. The runner loads BASE + this.url, so applyUrlState() runs on boot.
// Theme from the hash applies WITHOUT persisting (applyUrlState uses persist:false).
module.exports = {
  name: '10 · URL share-state',
  url: '#center=48.85,2.35&zoom=9&theme=synthwave',
  async run(page, t, { sleep }) {
    t.eq('#zoom applied to the map', await page.evaluate(`(typeof map!=='undefined'&&map)?map.getZoom():null`), 9);
    const c = await page.evaluate(`(typeof map!=='undefined'&&map)?(()=>{const x=map.getCenter();return{lat:+x.lat.toFixed(2),lon:+x.lng.toFixed(2)};})():null`);
    t.ok('#center applied (~48.85, 2.35)', c && Math.abs(c.lat - 48.85) < 0.5 && Math.abs(c.lon - 2.35) < 0.5, JSON.stringify(c));
    t.eq('#theme applied without persisting', await page.evaluate(`document.documentElement.dataset.theme`), 'synthwave');

    // Writing: pan → location.hash should pick up the new center (debounced ~350ms).
    // NB return a primitive — Leaflet methods return the map (returnByValue can't serialize it).
    await page.evaluate(`(()=>{map.panTo([50,3]);return true;})()`); await sleep(750);
    t.has('panning updates location.hash', await page.evaluate(`location.hash`), 'center=');
  },
};
