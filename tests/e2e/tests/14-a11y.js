'use strict';
// Accessibility smoke (2026-11-08 review, Lighthouse a11y findings): every filter slider
// has an accessible name, every aircraft marker (role=button) has one too, and the two
// flagged text elements clear WCAG AA contrast in the current theme.
// Contrast of an element's text against its effective background. The 1.8.0 surfaces are
// translucent color-mix() washes, which getComputedStyle reports as `color(srgb r g b / a)`
// (0–1 channels) rather than rgb(), so: parse both syntaxes, then composite every
// translucent ancestor layer over the first opaque one (falling back to the page colour —
// the real backdrop is the map, which this cannot know).
const CONTRAST = `(sel)=>{const el=document.querySelector(sel);if(!el)return null;
  const parse=(c)=>{if(!c)return null;let m=c.match(/^color\\(srgb\\s+([\\d.e-]+)\\s+([\\d.e-]+)\\s+([\\d.e-]+)(?:\\s*\\/\\s*([\\d.e-]+))?\\)/);
    if(m)return [m[1]*255,m[2]*255,m[3]*255,m[4]===undefined?1:+m[4]];
    m=c.match(/[\\d.]+/g);return m&&m.length>=3?[+m[0],+m[1],+m[2],m.length>3?+m[3]:1]:null;};
  const over=(top,bot)=>[0,1,2].map((i)=>top[i]*top[3]+bot[i]*(1-top[3])).concat(1);
  const lum=(c)=>{const v=c.slice(0,3).map((x)=>{x/=255;return x<=0.03928?x/12.92:Math.pow((x+0.055)/1.055,2.4);});return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2];};
  const layers=[];let n=el;while(n&&n.nodeType===1){const b=parse(getComputedStyle(n).backgroundColor);if(b&&b[3]>0){layers.push(b);if(b[3]>=1)break;}n=n.parentElement;}
  let bg=(layers.length&&layers[layers.length-1][3]>=1)?layers.pop():(parse(getComputedStyle(document.documentElement).getPropertyValue('--bg'))||[0,0,0,1]);
  while(layers.length)bg=over(layers.pop(),bg);
  let fg=parse(getComputedStyle(el).color);if(!fg)return null;if(fg[3]<1)fg=over(fg,bg);
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

    // New in 1.8.0: secondary labels on glass, the active tab/chip, and list rows.
    for (const [name, sel, min] of [
      ['list row callsign', '#aircraft-list .aircraft-row .callsign-text', 4.5],
      ['list row secondary line', '#aircraft-list .aircraft-row .type-reg-text', 4.5],
      ['active category chip', '#category-chips .chip-btn.active', 3],
      ['list count', '.list-count', 4.5],
      ['top-bar stat label', '.status-line .stat-label', 4.5],
    ]) {
      const r = await page.evaluate(`(${CONTRAST})(${JSON.stringify(sel)})`);
      t.ok(`${name} contrast ≥ ${min}:1`, r === null || r >= min, String(r));
    }

    t.ok('window.__piscopeReady set once init() finishes', await page.evaluate('window.__piscopeReady === true'));
  },
};
