'use strict';
// UI shell (1.8.0): the full-bleed map with floating panels. Covers the pieces the redesign
// introduced — icon sprite, map insets, list collapse / drawer, popovers (layers, themes,
// phone overflow menu), category chips, the filter panel, and that selecting an aircraft
// keeps it in the part of the map the panels don't cover.
//
// Ephemeral only: nothing here writes server settings (the theme cards, layer switches and
// label-mode buttons persist, so they are opened and inspected but never clicked). The two
// per-browser preferences it does flip (list collapsed, filter panel open) live in
// localStorage and are restored at the end.
const LS_KEYS = ['piscope_sidebar', 'piscope_filters'];
const DS = (k) => `(document.documentElement.dataset.${k} || '')`;
const attr = (sel, a) => `((document.querySelector(${JSON.stringify(sel)})||{getAttribute(){return null}}).getAttribute(${JSON.stringify(a)}))`;

module.exports = {
  name: '15 · UI shell (panels, popovers, layout)',
  viewport: 'desktop',
  async run(page, t, { sleep, VIEWPORTS }) {
    const saved = await page.evaluate(`JSON.stringify(${JSON.stringify(LS_KEYS)}.map((k)=>[k, localStorage.getItem(k)]))`);

    // --- icons: every control is named and actually draws something -----------------
    const btns = await page.evaluate(`[...document.querySelectorAll('.icon-button, .ctl-btn')].map((b)=>({id:b.id, label:b.getAttribute('aria-label')||'', svg:!!b.querySelector('svg use')}))`);
    t.ge('icon buttons present', btns.length, 15);
    t.eq('every icon button has an aria-label', btns.filter((b) => !b.label).map((b) => b.id).join(','), '');
    t.eq('every icon button draws an svg icon', btns.filter((b) => !b.svg).map((b) => b.id).join(','), '');
    const dangling = await page.evaluate(`[...document.querySelectorAll('svg use')].map((u)=>u.getAttribute('href')).filter((h)=>h&&h[0]==='#'&&!document.getElementById(h.slice(1)))`);
    t.eq('every <use> resolves to a sprite symbol', dangling.join(','), '');

    // --- geometry: map is full-bleed, insets describe the clear area ------------------
    const geo = await page.evaluate(`(()=>{const m=document.getElementById('map').getBoundingClientRect();return {mw:Math.round(m.width),mh:Math.round(m.height),vw:innerWidth,vh:innerHeight,i:mapInsets()};})()`);
    t.ok('map fills the viewport under the chrome', geo.mw === geo.vw && geo.mh === geo.vh, JSON.stringify(geo));
    t.ok('insets: list on the left, top bar above, nothing on the right', geo.i.left > 250 && geo.i.top > 40 && geo.i.right === 0 && geo.i.bottom === 0, JSON.stringify(geo.i));
    t.ok("Leaflet's own zoom control is not used outside embed mode", (await page.count('.leaflet-control-zoom')) === 0);
    const z0 = await page.evaluate(`window.__map.getZoom()`);
    await page.click('#zoom-in'); await sleep(500);
    t.eq('rail zoom-in zooms the map', await page.evaluate(`window.__map.getZoom()`), z0 + 1);
    await page.click('#zoom-out'); await sleep(500);
    t.eq('rail zoom-out zooms back', await page.evaluate(`window.__map.getZoom()`), z0);

    // --- list collapse / expand -------------------------------------------------------
    await page.click('#toggle-sidebar'); await sleep(450);
    t.eq('toggle collapses the list', await page.evaluate(DS('sidebar')), 'collapsed');
    t.eq('collapsed list frees the left inset', await page.evaluate(`mapInsets().left`), 0);
    t.eq('toggle reports the list hidden (aria-pressed=false)', await page.evaluate(attr('#toggle-sidebar', 'aria-pressed')), 'false');
    const railX = await page.evaluate(`Math.round(document.getElementById('map-controls').getBoundingClientRect().left)`);
    t.ok('map rail slides into the freed space', railX < 40, 'left=' + railX);
    await page.press('['); await sleep(450);
    t.ok("'[' brings the list back", await page.evaluate(`${DS('sidebar')} !== 'collapsed' && mapInsets().left > 250`));

    // --- layers popover ---------------------------------------------------------------
    await page.click('#open-layers'); await sleep(200);
    t.ok('layers popover opens', await page.open('#layers-popover'));
    t.eq('its button reports aria-expanded=true', await page.evaluate(attr('#open-layers', 'aria-expanded')), 'true');
    const pop = await page.rect('#layers-popover');
    t.ok('popover is fully on screen', pop && pop.left >= 0 && pop.right <= pop.vw && pop.y >= 0 && pop.y + pop.h <= pop.vh, JSON.stringify(pop));
    await page.press('Escape'); await sleep(150);
    t.ok('Esc closes the popover', !(await page.open('#layers-popover')));
    await page.press('l'); await sleep(200);
    t.ok("'l' opens the layers popover", await page.open('#layers-popover'));
    await page.evaluate(`(document.getElementById('map').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true})), true)`); await sleep(150);
    t.ok('pressing outside closes the popover', !(await page.open('#layers-popover')));
    t.eq('…and resets aria-expanded', await page.evaluate(attr('#open-layers', 'aria-expanded')), 'false');

    // --- theme gallery (inspect only — clicking a card persists the theme) ---------------
    await page.click('#open-themes'); await sleep(200);
    const th = await page.evaluate(`(()=>{const cards=[...document.querySelectorAll('#theme-grid .theme-card')];
      return {open:!document.getElementById('themes-popover').hidden, cards:cards.length,
        options:document.getElementById('theme-select').options.length,
        active:cards.filter((c)=>c.getAttribute('aria-pressed')==='true').map((c)=>c.dataset.themeId),
        theme:document.documentElement.dataset.theme,
        distinct:new Set(cards.map((c)=>getComputedStyle(c.querySelector('.theme-swatch')).backgroundColor)).size,
        settingsCards:document.querySelectorAll('#setting-theme-grid .theme-card').length};})()`);
    t.ok('theme popover opens', th.open);
    t.ok('one card per theme, in the popover and in Settings → Appearance', th.cards === th.options && th.settingsCards === th.options && th.cards >= 15, JSON.stringify(th));
    t.ok('exactly the active theme is marked', th.active.length === 1 && th.active[0] === th.theme, JSON.stringify(th.active) + ' vs ' + th.theme);
    t.ge('swatches render their own theme palettes (distinct backgrounds)', th.distinct, 10);
    await page.press('Escape'); await sleep(150);
    t.ok('Esc closes the theme popover', !(await page.open('#themes-popover')));

    // --- category chips ↔ the select they drive -----------------------------------------
    const total = await page.evaluate(`state.aircraft.size`);
    t.eq("'All' chip shows the feed size", await page.text('#cat-n-all'), total.toLocaleString('en-US'));
    await page.click('#category-chips [data-category="military"]'); await sleep(150);
    const mil = await page.evaluate(`({sel:document.getElementById('filter-category').value, f:state.filters.category,
      active:[...document.querySelectorAll('#category-chips .chip-btn.active')].map((c)=>c.dataset.category).join(','),
      rows:document.querySelectorAll('#aircraft-list .aircraft-row').length,
      count:+document.getElementById('cat-n-military').textContent.replace(/\\D/g,''),
      empty:!document.getElementById('list-empty').hidden})`);
    t.ok('military chip sets the select + filter state', mil.sel === 'military' && mil.f === 'military' && mil.active === 'military', JSON.stringify(mil));
    t.eq('rows shown match the chip count', mil.rows, mil.count);
    t.eq('empty-state shows only when nothing matches', mil.empty, mil.rows === 0);
    await page.evaluate(`(()=>{const s=document.getElementById('filter-category');s.value='all';s.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`); await sleep(150);
    t.eq('setting the select directly re-syncs the chips', await page.evaluate(`[...document.querySelectorAll('#category-chips .chip-btn.active')].map((c)=>c.dataset.category).join(',')`), 'all');

    // --- filter panel + "filters are narrowing" badge -----------------------------------
    const wasOpen = !(await page.evaluate(`document.getElementById('filter-panel').hidden`));
    await page.click('#toggle-filters'); await sleep(120);
    t.eq('filter button toggles the panel', !(await page.evaluate(`document.getElementById('filter-panel').hidden`)), !wasOpen);
    t.eq('…and mirrors it in aria-expanded', await page.evaluate(attr('#toggle-filters', 'aria-expanded')), String(!wasOpen));
    await page.click('#toggle-filters'); await sleep(120);
    await page.evaluate(`(()=>{const d=document.getElementById('dist-max');d.value='100';d.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`); await sleep(120);
    t.ok('badge appears while a tucked-away filter narrows the list', !(await page.evaluate(`document.getElementById('filters-badge').hidden`)));
    t.has('list header says how many of the feed are shown', await page.text('#list-count-label'), 'of ');
    await page.evaluate(`(resetFilters(), true)`); await sleep(150);
    t.ok('reset clears the badge and restores the list', await page.evaluate(`document.getElementById('filters-badge').hidden && document.querySelectorAll('#aircraft-list .aircraft-row').length === state.aircraft.size`));

    // --- selection keeps the aircraft in the unobscured part of the map -------------------
    if (total > 0) {
      // Pick one near the edge of coverage so the pan has real work to do.
      const hex = await page.evaluate(`[...state.aircraft.values()].filter((a)=>a.lat!=null).sort((a,b)=>(b.distance_nm||0)-(a.distance_nm||0))[0].hex`);
      await page.evaluate(`(selectAircraft(${JSON.stringify(hex)},{pan:true}), true)`); await sleep(900);
      const sel = await page.evaluate(`(()=>{const ac=state.aircraft.get(state.selectedHex);const p=window.__map.latLngToContainerPoint([ac.lat,ac.lon]);const s=window.__map.getSize();
        return {detail:document.documentElement.dataset.detail||'', i:mapInsets(), x:Math.round(p.x), y:Math.round(p.y), w:s.x, h:s.y,
          rowSelected:!!document.querySelector('#aircraft-list .aircraft-row.selected'), tiles:document.querySelectorAll('#detail-live .tile').length};})()`);
      t.eq('selecting flags the panel open on <html>', sel.detail, 'open');
      t.gt('open panel adds a right inset', sel.i.right, 300);
      t.ok('selected aircraft sits in the clear area between the panels',
        sel.x > sel.i.left && sel.x < sel.w - sel.i.right && sel.y > sel.i.top && sel.y < sel.h - sel.i.bottom, JSON.stringify(sel));
      t.ok('its list row is marked selected', sel.rowSelected);
      t.eq('live section renders the six stat tiles', sel.tiles, 6);
      const inView = await page.evaluate(`(()=>{const r=document.querySelector('#aircraft-list .aircraft-row.selected').getBoundingClientRect();const l=document.getElementById('aircraft-list').getBoundingClientRect();return r.top>=l.top-1&&r.bottom<=l.bottom+1;})()`);
      t.ok('selected row is scrolled into view', inView);
      await page.press('Escape'); await sleep(450);
      t.ok('Esc closes the panel and clears the inset', await page.evaluate(`!document.documentElement.dataset.detail && mapInsets().right === 0`));
    } else {
      t.ok('skipped selection checks — no live aircraft', true);
    }

    // --- phone layout ----------------------------------------------------------------------
    await page.setViewport(VIEWPORTS.mobile); await sleep(400);
    const vis = (sel) => page.visible(sel);
    t.ok('phone · overflow menu button is shown', await vis('#open-more'));
    t.ok('phone · secondary actions fold away', !(await vis('#open-stats')) && !(await vis('#open-settings')) && !(await vis('#open-themes')));
    t.ok('phone · events stay one tap away', await vis('#open-events'));
    t.eq('phone · list is a closed drawer (no left inset)', await page.evaluate(`mapInsets().left`), 0);
    await page.click('#toggle-sidebar'); await sleep(450);
    t.ok('phone · toggle opens the drawer with a scrim', await page.evaluate(`${DS('sidebar')} === 'open' && !document.getElementById('scrim').hidden`));
    await page.press('Escape'); await sleep(450);
    t.ok('phone · Esc closes the drawer', await page.evaluate(`${DS('sidebar')} !== 'open' && document.getElementById('scrim').hidden`));
    await page.click('#toggle-sidebar'); await sleep(300);
    await page.click('#scrim'); await sleep(300);
    t.ok('phone · tapping the scrim closes the drawer', await page.evaluate(`${DS('sidebar')} !== 'open'`));
    await page.click('#open-more'); await sleep(200);
    t.ok('phone · overflow menu opens', await page.open('#more-menu'));
    await page.click('#more-menu [data-proxy="open-stats"]'); await sleep(300);
    t.ok('phone · menu item opens its dialog and closes the menu', (await page.open('#stats-modal')) && !(await page.open('#more-menu')));
    await page.press('Escape'); await sleep(200);
    await page.click('#open-more'); await sleep(200);
    await page.click('#more-menu [data-proxy="open-themes"]'); await sleep(250);
    const tp = await page.rect('#themes-popover');
    t.ok('phone · theme gallery opens from the menu, on screen', (await page.open('#themes-popover')) && tp && tp.left >= 0 && tp.right <= tp.vw, JSON.stringify(tp));
    await page.press('Escape'); await sleep(150);
    await page.setViewport(VIEWPORTS.desktop); await sleep(300);

    // --- restore the per-browser preferences this module flipped ---------------------------
    await page.evaluate(`(()=>{for (const [k,v] of JSON.parse(${JSON.stringify(saved)})) { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } return true;})()`);
    t.eq('no exceptions across the shell flow', page.exceptions.length, 0);
  },
};
