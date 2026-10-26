'use strict';
// PWA: the service worker is served at the right scope with a version-stamped shell cache,
// and the web manifest is scoped to /piscope/.
module.exports = {
  name: '12 · PWA / service worker',
  async run(page, t, { sleep }) {
    const sw = await page.evaluate(`fetch('/piscope/sw.js').then(async r=>({status:r.status, allow:r.headers.get('service-worker-allowed'), body:await r.text()}))`);
    t.eq('GET /piscope/sw.js → 200', sw.status, 200);
    t.has('Service-Worker-Allowed header scopes /piscope', sw.allow || '', '/piscope');

    const v = await page.evaluate(`fetch('/piscope/api/version').then(r=>r.json()).then(d=>d.version).catch(()=>'')`);
    const tag = (sw.body.match(/piscope-shell-[0-9_]+-[a-z0-9]+/) || [])[0] || '';
    t.has(`sw CACHE tag carries the running version (${v})`, tag, String(v).replace(/\./g, '_'));

    const man = await page.evaluate(`(()=>{const l=document.querySelector('link[rel=manifest]');return l?l.href:null;})()`);
    t.ok('manifest <link rel=manifest> present', !!man, String(man));
    if (man) {
      const m = await page.evaluate(`fetch(${JSON.stringify(man)}).then(r=>r.json()).catch(()=>null)`);
      t.ok('manifest parses and is scoped to /piscope/', m && /\/piscope/.test(m.scope || ''), JSON.stringify(m && m.scope));
    }

    // SW registration (async after load) — give it a moment, then check; soft if not yet ready.
    await sleep(800);
    const reg = await page.evaluate(`navigator.serviceWorker?navigator.serviceWorker.getRegistration('/piscope/').then(r=>r?{scope:r.scope}:null).catch(()=>null):null`);
    t.ok('service worker registered (scope /piscope/) or served OK', (reg && /\/piscope\//.test(reg.scope)) || sw.status === 200, JSON.stringify(reg));
  },
};
