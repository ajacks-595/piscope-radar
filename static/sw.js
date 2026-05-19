/* PiScope Radar service worker. Only caches the app shell so install-to-homescreen works offline-ish;
 * the live data path (WS, API) always hits the network. */
const SHELL = [
  '/piscope',
  '/piscope/static/index.html',
  '/piscope/static/app.css',
  '/piscope/static/themes.css',
  '/piscope/static/app.js',
  '/piscope/static/radar.js',
  '/piscope/static/icons/aircraft.svg',
  '/piscope/static/manifest.webmanifest',
];
const CACHE = 'piscope-shell-v2';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
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
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return resp;
      }).catch(() => cached)),
    );
  }
});
