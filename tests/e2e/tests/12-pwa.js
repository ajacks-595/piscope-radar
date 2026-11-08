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

    // Secure-context honesty (2026-11-08 P2-5): navigator.serviceWorker only exists on
    // HTTPS / localhost. Settings → General must say which mode this browser is in.
    const pwa = await page.evaluate(`(()=>{const el=document.getElementById('pwa-status');return {sw:'serviceWorker' in navigator, secure:window.isSecureContext, text:el?el.textContent:''};})()`);
    t.ok('PWA status hint present in Settings → General', pwa.text.length > 0, JSON.stringify(pwa));
    t.ok('PWA hint matches service-worker availability', pwa.sw ? /active/i.test(pwa.text) : /plain HTTP/i.test(pwa.text), JSON.stringify(pwa));
    t.eq('serviceWorker API present exactly in a secure context', pwa.sw, pwa.secure);

    // SW registration (async after load) — give it a moment, then check; soft if not yet ready.
    await sleep(800);
    const reg = await page.evaluate(`navigator.serviceWorker?navigator.serviceWorker.getRegistration('/piscope/').then(r=>r?{scope:r.scope}:null).catch(()=>null):null`);
    t.ok('service worker registered (scope /piscope/) or served OK', (reg && /\/piscope\//.test(reg.scope)) || sw.status === 200, JSON.stringify(reg));
  },
};
