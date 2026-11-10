/* PiScope Radar service worker. Only caches the app shell so install-to-homescreen works offline-ish;
 * the live data path (WS, API) always hits the network. */
// IMPORTANT: do NOT pre-cache `/piscope` (the HTML root). Each release stamps a new `?v=…`
// onto the static asset URLs in that HTML, and if we cache the HTML we keep serving stale
// asset URLs forever. Network-first for the HTML, cache-first for the static assets — that
// way deep-links and version bumps just work on the very next navigation.
const SHELL = [
  '/piscope/static/app.css',
  '/piscope/static/themes.css',
  '/piscope/static/app.js',
  '/piscope/static/radar.js',
  '/piscope/static/icons/aircraft.svg',
  '/piscope/static/icons/piscope.svg',
  '/piscope/static/manifest.webmanifest',
  // Iteration 5: airport overlay data — 464 KB, pre-cached so the overlay toggles instantly
  // and works offline. Refreshed by the same version-eviction logic when the cache bumps.
  '/piscope/static/data/airports.json',
  // Iteration 13.2: Leaflet vendored locally — precache it so the map shell (and
  // therefore the whole app) boots with zero internet. Tiles still need network,
  // but the radar-sweep themes are fully self-contained.
  '/piscope/static/vendor/leaflet/leaflet.css',
  '/piscope/static/vendor/leaflet/leaflet.js',
  '/piscope/static/vendor/leaflet.heat/leaflet-heat.js',
];
// NOTE: this literal is rewritten on the wire to a content-hash-derived tag
// (`piscope-shell-<version>-<sha256-prefix>`) by app/main.py's /piscope/sw.js
// route — see iter 9.4. The literal here is just a sensible fallback if anyone
// serves sw.js as a static file without going through the FastAPI rewrite.
const CACHE = 'piscope-shell-rewritten';
// The release this worker's shell belongs to, recovered from the tag above
// ('piscope-shell-1_8_0-<hash>' -> '1.8.0'). null for the un-rewritten fallback.
const SHELL_VERSION = ((/^piscope-shell-(\d+(?:_\d+)*)-/.exec(CACHE) || [])[1] || '').replace(/_/g, '.') || null;

self.addEventListener('install', (event) => {
  // `cache: 'reload'` — precache straight from the server, never from the browser's
  // HTTP cache. The shell URLs carry no ?v= stamp, so they are served without explicit
  // cache headers and the browser may treat an old copy as heuristically fresh; without
  // the bypass a brand-new worker precached the PREVIOUS release's CSS and then served
  // it cache-first for as long as that release lasted (found during the 1.8.0 redesign).
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
      .catch(() => {}),
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Never cache API or WS traffic.
  if (url.pathname.startsWith('/piscope/api') || url.pathname.startsWith('/piscope/ws')) return;
  // Same-origin static assets get a cache-first strategy.
  if (url.origin === self.location.origin && url.pathname.startsWith('/piscope/static')) {
    // The page requests its assets as `...?v=<release>`. If it asks for a different
    // release than this worker's shell, the app has been upgraded and this worker is
    // about to be replaced — step aside (plain network) instead of answering new HTML
    // with the old release's JS and CSS.
    const v = url.searchParams.get('v');
    if (v && SHELL_VERSION && v !== SHELL_VERSION) return;
    event.respondWith(
      // Look only in THIS worker's cache (not caches.match across every cache), so a
      // leftover cache from another release can never answer.
      caches.open(CACHE).then((cache) => cache.match(req, { ignoreSearch: true }).then((cached) => cached || fetch(req).then((resp) => {
        // Only cache genuine successes. Without the resp.ok gate a 502/503/404
        // returned for a `?v=…` asset URL mid-deploy would be cached and — since
        // this is cache-first — served forever until the next version bump.
        if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
        return resp;
      }))),
    );
  }
});
