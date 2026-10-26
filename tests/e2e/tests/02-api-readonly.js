'use strict';
// Smoke-test every READ-ONLY endpoint (GET). Asserts 2xx + a minimal shape. Never touches
// any write/AI/FA/webhook/import endpoint (those are in TEST_PLAN.md as destructive).
const GET = (p) => `fetch(${JSON.stringify('/piscope' + p)}).then(async r=>({status:r.status, body:await r.json().catch(()=>null)}))`;

const CHECKS = [
  ['/api/aircraft', (b) => Array.isArray(b.aircraft)],
  ['/api/health', (b) => ('connection_state' in b) || ('aircraft_count' in b)],
  ['/api/events', (b) => Array.isArray(b.events)],
  ['/api/events?kind=military', (b) => Array.isArray(b.events)],
  ['/api/events?kind=emergency', (b) => Array.isArray(b.events)],
  ['/api/stats', (b) => typeof b === 'object'],
  ['/api/coverage', (b) => !!b],
  ['/api/heatmap', (b) => !!b],
  ['/api/leaderboard', (b) => !!b],
  ['/api/records', (b) => !!b],
  ['/api/bookmarks', (b) => !!b],
  ['/api/notes', (b) => !!b],
  ['/api/views', (b) => !!b],
  ['/api/webhooks', (b) => !!b],
  ['/api/analytics?range=24h', (b) => !!b],
  ['/api/analytics/notable?range=7d', (b) => !!b],
  ['/api/analytics/returning?range=7d', (b) => !!b],
  ['/api/replay/timeline', (b) => !!b],
  ['/api/explain/status', (b) => 'configured' in b],
  ['/api/flightaware/budget', (b) => !!b],
  ['/api/digest', (b) => 'digest' in b],
  ['/api/settings', (b) => 'poll_interval' in b],
  ['/api/dashboard/summary?lat=51.5&lon=-0.1&radius_km=50', (b) => 'success' in b],
];

module.exports = {
  name: '02 · API (read-only endpoints)',
  async run(page, t) {
    for (const [p, shape] of CHECKS) {
      let r;
      try { r = await page.evaluate(GET(p)); } catch (e) { t.ok('GET ' + p, false, 'eval error: ' + e.message); continue; }
      const okStatus = r.status >= 200 && r.status < 300;
      const okShape = r.body != null && shape(r.body);
      t.ok(`GET ${p} → ${r.status}`, okStatus && okShape, JSON.stringify(r.body).slice(0, 100));
    }
    // /api/metrics is text/plain (Prometheus) — status-only check
    const m = await page.evaluate(`fetch('/piscope/api/metrics').then(r=>({status:r.status}))`);
    t.ok('GET /api/metrics → 200 (text)', m.status === 200, JSON.stringify(m));
  },
};
