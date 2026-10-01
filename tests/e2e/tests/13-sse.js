'use strict';
// Dashboard SSE stream (/api/dashboard/events) must STREAM through the reverse proxy.
// lighttpd's default (server.stream-response-body = 0) buffered the whole response, so
// dashboards never received a byte and every abandoned stream stayed connected upstream
// until the next restart (found 2026-10-01). Read-only: opens one stream, reads the first
// chunk, aborts it, and checks the server reclaims the subscription.
module.exports = {
  name: '13 · Dashboard SSE stream via proxy',
  async run(page, t, { sleep }) {
    const subscribers = `fetch('/piscope/api/dashboard/events/stats').then(r=>r.json()).then(d=>d.subscribers).catch(()=>-1)`;
    const r = await page.evaluate(`(async()=>{
      const ctl = new AbortController(); window.__sseCtl = ctl;
      const t0 = performance.now();
      const res = await fetch('/piscope/api/dashboard/events', { signal: ctl.signal });
      const reader = res.body.getReader();
      const first = await Promise.race([reader.read(), new Promise((r) => setTimeout(() => r({ timeout: true }), 8000))]);
      const text = first && first.value ? new TextDecoder().decode(first.value) : '';
      return { status: res.status, type: res.headers.get('content-type'), ms: Math.round(performance.now() - t0),
               text: text.slice(0, 80), timeout: !!(first && first.timeout) };
    })()`);
    t.eq('stream answers 200', r.status, 200);
    t.has('content-type is text/event-stream', r.type || '', 'text/event-stream');
    t.ok('first bytes arrive promptly (not buffered by the proxy)', !r.timeout && r.ms < 5000, `${r.ms} ms`);
    t.has('first chunk is the stream preamble', r.text, ':piscope dashboard events stream');

    const during = await page.evaluate(subscribers);
    t.ge('our subscription is counted while connected', during, 1);
    await page.evaluate(`(window.__sseCtl && window.__sseCtl.abort(), true)`);
    // The proxy notices a dead client on its next write — the ≤ 25 s heartbeat.
    let after = during;
    for (let i = 0; i < 40 && after >= during; i++) { await sleep(1000); after = await page.evaluate(subscribers); }
    t.ok('aborted stream is reclaimed server-side within ~one heartbeat', after < during, `${during} → ${after}`);
  },
};
