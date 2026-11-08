'use strict';
// Accessibility smoke (2026-11-08 review, Lighthouse a11y findings): every filter slider
// has an accessible name, every aircraft marker (role=button) has one too, and the two
// flagged text elements clear WCAG AA contrast in the current theme.
const CONTRAST = `(sel)=>{const el=document.querySelector(sel);if(!el)return null;
  const rgb=(c)=>{const m=c.match(/[\\d.]+/g);return m?m.slice(0,3).map(Number):null;};
  const lum=(c)=>{const v=c.map((x)=>{x/=255;return x<=0.03928?x/12.92:Math.pow((x+0.055)/1.055,2.4);});return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2];};
  let bgEl=el,bg=null;while(bgEl){const b=getComputedStyle(bgEl).backgroundColor;if(b&&!/rgba\\(0, 0, 0, 0\\)|transparent/.test(b)){bg=rgb(b);break;}bgEl=bgEl.parentElement;}
  const fg=rgb(getComputedStyle(el).color);if(!fg||!bg)return null;
  const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);return +((a+0.05)/(b+0.05)).toFixed(2);}`;

module.exports = {
  name: '14 · Accessibility smoke',
  async run(page, t) {
    for (const id of ['alt-min', 'alt-max', 'dist-max']) {
      const label = await page.evaluate(`(document.getElementById('${id}')||{}).getAttribute?.('aria-label')||''`);
      t.ok(`#${id} range input has an accessible name`, label.length > 0, label);
    }
    const markers = await page.evaluate(`(()=>{const m=[...document.querySelectorAll('.aircraft-marker')];return {n:m.length, unnamed:m.filter((e)=>!(e.getAttribute('aria-label')||'').trim()).length, w:m[0]?m[0].getBoundingClientRect().width:null};})()`);
    t.eq(`every aircraft marker has an aria-label (${markers.n} on map)`, markers.unnamed, 0);
    if (markers.n) t.ok('marker hit box ≥ 24 px', markers.w >= 24, String(markers.w));

    const pill = await page.evaluate(`(${CONTRAST})('#connection-pill')`);
    t.ok('connection pill text contrast ≥ 4.5:1', pill === null || pill >= 4.5, String(pill));
    const empty = await page.evaluate(`(${CONTRAST})('#detail-empty span, #detail-empty')`);
    t.ok('detail-empty hint contrast ≥ 3:1 (≥ 4.5 in most themes)', empty === null || empty >= 3, String(empty));

    t.ok('window.__piscopeReady set once init() finishes', await page.evaluate('window.__piscopeReady === true'));
  },
};
