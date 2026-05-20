/* PiScope Radar — main client.
 *
 * Responsible for:
 *   • WebSocket connection + reconnection with backoff
 *   • In-memory aircraft + trail state
 *   • Sidebar rendering with filter/sort
 *   • Leaflet map with custom markers, trails, range rings, receiver marker
 *   • Aircraft selection sync across sidebar / map / detail
 *   • Detail panel with hexdb / adsbdb / planespotters / FlightAware sections
 *   • Settings modal
 *   • Browser notifications (military / emergency / watchlist)
 *   • Coordination with RadarSweep (radar.js) for the two radar themes
 */

const API = {
  ws: () => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/piscope/ws`,
  settings: '/piscope/api/settings',
  faKey: '/piscope/api/settings/fa-key',
  testConn: '/piscope/api/test-connection',
  hexdb: (hex) => `/piscope/api/enrich/hexdb/${encodeURIComponent(hex)}`,
  adsbdb: (callsign) => `/piscope/api/enrich/adsbdb/${encodeURIComponent(callsign)}`,
  photo: (hex) => `/piscope/api/enrich/photo/${encodeURIComponent(hex)}`,
  faBudget: '/piscope/api/flightaware/budget',
  faLookup: (callsign, confirm) => `/piscope/api/flightaware/${encodeURIComponent(callsign)}${confirm ? '?confirm_over_budget=true' : ''}`,
  openaipKey: '/piscope/api/settings/openaip-key',
  openaipTiles: '/piscope/api/tiles/openaip/{z}/{x}/{y}.png',
  events: (kind, limit) => `/piscope/api/events${kind ? `?kind=${kind}&limit=${limit||100}` : `?limit=${limit||100}`}`,
  stats: '/piscope/api/stats?days=7',
  health: '/piscope/api/health',
  replayTimeline: '/piscope/api/replay/timeline',
  replayAt: (ts) => `/piscope/api/replay/at?ts=${encodeURIComponent(ts)}`,
  coverage: '/piscope/api/coverage',
  heatmap: '/piscope/api/heatmap?top=3000',
  leaderboard: '/piscope/api/leaderboard?limit=30',
  notes: (hex) => `/piscope/api/notes/${encodeURIComponent(hex)}`,
  webhooks: '/piscope/api/webhooks',
  webhookTest: '/piscope/api/webhooks/test',
  views: '/piscope/api/views',
  records: '/piscope/api/records',
  bookmarks: '/piscope/api/bookmarks',
  bookmark: (hex) => `/piscope/api/bookmarks/${encodeURIComponent(hex)}`,
  importDb: '/piscope/api/import',
};

const RADAR_THEMES = new Set(['radar', 'radarModern']);
const RADAR_VARIANTS = { radar: 'classic', radarModern: 'modern' };

const TILE_PRESETS = {
  // All keyless, free for personal use. The ?{r} is for high-DPI; CartoDB supports it.
  dark:        { url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',  attribution: '© OpenStreetMap, © CartoDB', label: 'Dark (CartoDB)' },
  darkClean:   { url: 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', attribution: '© OpenStreetMap, © CartoDB', label: 'Dark — no labels (CartoDB)' },
  light:       { url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', attribution: '© OpenStreetMap, © CartoDB', label: 'Light (CartoDB)' },
  lightClean:  { url: 'https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', attribution: '© OpenStreetMap, © CartoDB', label: 'Light — no labels (CartoDB)' },
  voyager:     { url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', attribution: '© OpenStreetMap, © CartoDB', label: 'Voyager (CartoDB)' },
  voyagerClean:{ url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png', attribution: '© OpenStreetMap, © CartoDB', label: 'Voyager — no labels (CartoDB)' },
  sat:         { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Satellite (Esri)' },
  esriDark:    { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Dark Gray (Esri)' },
  esriLight:   { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Light Gray (Esri)' },
  esriTopo:    { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Topographic (Esri)' },
  esriStreet:  { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Streets (Esri)' },
  esriOcean:   { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Ocean (Esri)' },
  esriShaded:  { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Shaded Relief (Esri)' },
  esriPhysical:{ url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Physical_Map/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Physical (Esri)' },
  esriTerrain: { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Terrain_Base/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri', label: 'Terrain (Esri)' },
  natgeo:      { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}', attribution: 'Tiles © Esri / Nat Geo', label: 'NatGeo (Esri)' },
  osm:         { url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '© OpenStreetMap contributors', label: 'OpenStreetMap' },
  osmHot:      { url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', attribution: '© OpenStreetMap France, © OpenStreetMap', label: 'OSM Humanitarian' },
  cyclosm:     { url: 'https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png', attribution: '© CyclOSM, © OpenStreetMap', label: 'CyclOSM' },
  terrain:     { url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', attribution: '© OpenTopoMap (CC-BY-SA)', label: 'Topographic (OTM)' },
};

const THEME_DEFAULT_TILES = {
  radar: 'esriDark', radarModern: 'esriDark', terminal: 'dark',
  light: 'esriLight', dark: 'esriDark', tactical: 'sat',
  sectional: 'voyager', solarizedDark: 'esriDark', solarizedLight: 'esriLight',
  nord: 'esriDark', synthwave: 'esriDark',
};

const AIRCRAFT_SVG = '<svg viewBox="0 0 20 22"><path d="M10,0 L13,6 L20,8 L20,10 L13,10 L12,18 L15,20 L15,21 L10,19.5 L5,21 L5,20 L8,18 L7,10 L0,10 L0,8 L7,6 Z"/></svg>';
// Wrapped: SVG inside a non-rotating outer marker so labels can sit beside the icon
// without spinning. The wrap is what gets the heading rotation applied to it.
const MARKER_HTML = `<div class="ac-svg-wrap">${AIRCRAFT_SVG}</div><div class="ac-label" hidden></div>`;
// In-memory route cache (mirrors the server one) for use in map labels.
// Maps callsign -> { origin_iata, destination_iata } or null if no route exists.
const _routeCache = new Map();
const _routeInFlight = new Set();

const state = {
  aircraft: new Map(),
  trails: new Map(),
  receiver: null,
  connectionState: 'idle',
  selectedHex: null,
  settings: {},
  filters: {
    text: '',
    category: 'all',
    altMin: 0,
    altMax: 60000,
    distMax: 500,
    showGround: true,
    sort: 'distance',
  },
  notifiedHexes: new Set(),
  enrichmentCache: { hexdb: new Map(), adsbdb: new Map(), photo: new Map() },
  selectedEnrichment: null,
  /* Feature flags */
  audioEnabled: false,
  audioDirectional: true,
  follow: false,
  replayActive: false,
  replayTimestamps: [],     // ascending list of available snapshot ts
  replayLiveSnapshot: null, // last live WS payload, restored when leaving replay
  eventsUnread: 0,
};

/* Theme-derived colours.  Reading them per marker every tick triggers a costly style recalc, so
 * we capture them once when the theme changes and serve hot-path lookups from this object. */
const themeColors = {
  ground: '#888', low: '#0f0', mid: '#ff0', high: '#f80', veryHigh: '#f00',
  accent: '#0f0', accentMuted: '#0a0', mapTrail: '#0a0', mapRangeRing: '#066',
};

function refreshThemeColorCache() {
  const cs = getComputedStyle(document.documentElement);
  themeColors.ground    = cs.getPropertyValue('--band-ground').trim()    || themeColors.ground;
  themeColors.low       = cs.getPropertyValue('--band-low').trim()       || themeColors.low;
  themeColors.mid       = cs.getPropertyValue('--band-mid').trim()       || themeColors.mid;
  themeColors.high      = cs.getPropertyValue('--band-high').trim()      || themeColors.high;
  themeColors.veryHigh  = cs.getPropertyValue('--band-very-high').trim() || themeColors.veryHigh;
  themeColors.accent    = cs.getPropertyValue('--accent').trim()         || themeColors.accent;
  themeColors.accentMuted = cs.getPropertyValue('--accent-muted').trim() || themeColors.accentMuted;
  themeColors.mapTrail  = cs.getPropertyValue('--map-trail').trim()      || themeColors.mapTrail;
  themeColors.mapRangeRing = cs.getPropertyValue('--map-range-ring').trim() || themeColors.mapRangeRing;
}

let map = null;
let markersLayer = null;
let trailsLayer = null;
let ringsLayer = null;
let receiverMarker = null;
let routeLineLayer = null;
let tileLayer = null;
let aeroOverlayLayer = null;
let weatherOverlayLayer = null;
let heatmapLayer = null;
let terminatorLayer = null;
let radar = null;
let radarCanvas = null;
let radarCtx = null;
let coordGridEl = null;
let aircraftMarkers = new Map();
let aircraftTrails = new Map();
let detailPhotos = [];
let detailPhotoIndex = 0;
let webhooksCache = [];      // last-loaded webhook list, in-flight edits
let savedViewsCache = [];
let bookmarkedHexes = new Set();   // mirror of /api/bookmarks for instant ★ rendering

// ---------- WebSocket client ----------

class PiScopeClient {
  constructor() {
    this.ws = null;
    this.backoff = 1000;
    this.maxBackoff = 30000;
    this.alive = false;
  }

  connect() {
    try {
      this.ws = new WebSocket(API.ws());
    } catch (e) {
      console.error('WS construction failed', e);
      this._scheduleReconnect();
      return;
    }
    this.ws.addEventListener('open', () => {
      this.alive = true;
      this.backoff = 1000;
      console.info('PiScope Radar WS connected');
    });
    this.ws.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        this._handle(data);
      } catch (e) {
        console.warn('bad WS payload', e);
      }
    });
    this.ws.addEventListener('close', () => {
      this.alive = false;
      this._scheduleReconnect();
    });
    this.ws.addEventListener('error', () => {
      // close handler will reconnect
    });
  }

  _scheduleReconnect() {
    setTimeout(() => this.connect(), this.backoff);
    this.backoff = Math.min(this.maxBackoff, this.backoff * 1.7);
  }

  _handle(data) {
    if (data.type === 'ping') return;
    if (data.type === 'aircraft_update') applyAircraftUpdate(data);
  }
}

// ---------- Apply WS update ----------

/* ---------- Audio engine ----------------------------------------------------
 * Web Audio chimes for alerts. Lazily-initialised so we don't grab the AudioContext
 * until the user has interacted with the page (browser autoplay policy). */
let audioCtx = null;
function ensureAudioContext() {
  if (audioCtx) return audioCtx;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    audioCtx = new AC();
  } catch (e) { return null; }
  return audioCtx;
}
function chime({ pitch = 880, duration = 0.18, pan = 0, double = false } = {}) {
  const ctx = ensureAudioContext();
  if (!ctx) return;
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const make = (freq, when) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.value = 0;
    let lastNode = gain;
    if (state.audioDirectional && typeof ctx.createStereoPanner === 'function') {
      const panner = ctx.createStereoPanner();
      panner.pan.value = Math.max(-1, Math.min(1, pan));
      gain.connect(panner);
      lastNode = panner;
    }
    lastNode.connect(ctx.destination);
    osc.connect(gain);
    gain.gain.linearRampToValueAtTime(0.15, when + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + duration);
    osc.start(when);
    osc.stop(when + duration + 0.05);
  };
  const now = ctx.currentTime;
  make(pitch, now);
  if (double) make(pitch * 1.5, now + duration * 0.6);
}

function chimeFor(kind, ac) {
  const pan = computePanForAircraft(ac);
  switch (kind) {
    case 'emergency': return chime({ pitch: 1320, duration: 0.25, pan, double: true });
    case 'military':  return chime({ pitch: 660,  duration: 0.22, pan, double: false });
    case 'watchlist': return chime({ pitch: 990,  duration: 0.18, pan, double: false });
  }
}

function computePanForAircraft(ac) {
  if (!state.audioDirectional || !state.receiver || !ac || ac.lat == null || ac.lon == null) return 0;
  // Stereo pan = sin(bearing). Aircraft to the east → right ear; west → left.
  const lat1 = state.receiver.lat * Math.PI / 180;
  const lat2 = ac.lat * Math.PI / 180;
  const dLon = (ac.lon - state.receiver.lon) * Math.PI / 180;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return Math.sin(Math.atan2(y, x));
}

/* ---------- Closest Point of Approach (ETA to overhead) -------------------- */
function etaToOverhead(ac) {
  // Treat aircraft as moving in a straight line at its current track + ground speed and find
  // when it minimises distance to the receiver. Returns { minutes, distance_nm } or null.
  if (!state.receiver || ac == null) return null;
  if (ac.lat == null || ac.lon == null) return null;
  if (ac.ground_speed == null || ac.ground_speed < 30) return null;
  const track = ac.track ?? ac.heading;
  if (track == null) return null;
  // Convert lat/lon offsets to nm.
  const NM_PER_DEG_LAT = 60;
  const cosLat = Math.cos(state.receiver.lat * Math.PI / 180);
  const dxNm = (ac.lon - state.receiver.lon) * NM_PER_DEG_LAT * cosLat;
  const dyNm = (ac.lat - state.receiver.lat) * NM_PER_DEG_LAT;
  // Velocity vector in nm/min (track is degrees from north, clockwise).
  const trackRad = track * Math.PI / 180;
  const speedNmPerMin = ac.ground_speed / 60;
  const vx =  speedNmPerMin * Math.sin(trackRad);
  const vy =  speedNmPerMin * Math.cos(trackRad);
  // CPA time: t = -(r·v) / |v|².
  const speedSq = vx * vx + vy * vy;
  if (speedSq < 1e-6) return null;
  const t = -((dxNm * vx) + (dyNm * vy)) / speedSq;
  if (t <= 0) return null;     // aircraft is moving away
  const cpaDx = dxNm + vx * t;
  const cpaDy = dyNm + vy * t;
  const cpaDist = Math.sqrt(cpaDx * cpaDx + cpaDy * cpaDy);
  return { minutes: t, distance_nm: cpaDist };
}

function applyAircraftUpdate(data) {
  state.connectionState = data.connection_state || 'polling';
  state.receiver = data.receiver || state.receiver;
  state.feedNow = data.now;
  // Track message rate using deltas across polls. The backend currently exposes total contacts
  // (not raw decoded messages), so this surfaces "active contacts/s" which is the user-meaningful number.
  const nowMs = performance.now();
  const lastTotal = state.totalMessages || 0;
  const newTotal = data.total_messages || 0;
  if (state.lastRateAt && nowMs > state.lastRateAt) {
    const dtSec = (nowMs - state.lastRateAt) / 1000;
    const dContacts = Math.max(0, newTotal - lastTotal);
    state.recentRate = dtSec > 0 ? dContacts / dtSec : 0;
  }
  state.lastRateAt = nowMs;
  state.totalMessages = newTotal;

  state.aircraft.clear();
  for (const ac of data.aircraft || []) {
    state.aircraft.set(ac.hex, ac);
  }
  // Trail handling:
  //   - On initial connect / REST fallback the backend ships `trails_full: true` plus the
  //     full historical `trails` dict — replace local state.
  //   - On regular WS broadcasts the backend ships `trail_appends` — one new point per
  //     aircraft that moved this poll — and we merge them into the existing local trails.
  //   This cuts WS bandwidth by ~70% at the cost of a tiny amount of client merge logic.
  if (data.trails_full && data.trails) {
    state.trails.clear();
    for (const [hex, pts] of Object.entries(data.trails)) state.trails.set(hex, pts);
  } else if (data.trail_appends) {
    const trailLen = parseInt(state.settings?.trail_length, 10) || 30;
    for (const [hex, point] of Object.entries(data.trail_appends)) {
      let trail = state.trails.get(hex);
      if (!trail) { trail = []; state.trails.set(hex, trail); }
      trail.push(point);
      while (trail.length > trailLen) trail.shift();
    }
    // Drop trails for aircraft no longer in the feed (mirrors backend GC).
    for (const hex of [...state.trails.keys()]) {
      if (!state.aircraft.has(hex)) {
        // Keep stale trails for ~2 minutes so they fade gracefully on the map.
        const last = state.trails.get(hex);
        const lastTs = last?.length ? last[last.length - 1][2] : 0;
        if ((data.now || Date.now() / 1000) - lastTs > 120) state.trails.delete(hex);
      }
    }
  }

  updateConnectionPill();
  updateAircraftCount();
  updateReceiverLabel();
  updateMessageRate();
  renderSidebar();
  refreshMapMarkers();
  refreshTrails();
  refreshRangeRings();
  refreshReceiverMarker();
  updateDetailLiveSection();
  fireNotifications();

  // On the first message with data, snap the viewport to where the action is so the user
  // doesn't have to hunt for their aircraft.
  if (!state.didInitialFit && (state.aircraft.size > 0 || state.receiver)) {
    state.didInitialFit = true;
    fitToData();
  }

  // URL-deeplinked selection — apply once the shared aircraft actually appears in the feed.
  // Otherwise we'd lose the selection on first paint (the list is empty during init()).
  if (state._pendingSelect && state.aircraft.has(state._pendingSelect)) {
    const target = state._pendingSelect;
    state._pendingSelect = null;
    selectAircraft(target, { pan: true });
  }

  if (radar) {
    radar.setReceiver(state.receiver);
    radar.setAircraft([...state.aircraft.values()].map((a) => ({
      hex: a.hex, lat: a.lat, lon: a.lon, heading: a.heading, altBand: a.altitude_band,
    })));
  }

  // Trigger route enrichment for visible aircraft when full-info labels are on.
  maybePrefetchRoutes();

  // Follow mode — keep the selected aircraft centred as new positions arrive.
  if (state.follow && state.selectedHex) {
    const ac = state.aircraft.get(state.selectedHex);
    if (ac && ac.lat != null) map.panTo([ac.lat, ac.lon], { animate: true, duration: 0.6 });
  }

  // Keep the most recent live snapshot for replay's "live" return path.
  state.replayLiveSnapshot = data;
}

// ---------- Topbar status ----------

function updateConnectionPill() {
  const el = document.getElementById('connection-pill');
  el.className = `pill ${state.connectionState}`;
  el.textContent = state.connectionState;
}

function updateAircraftCount() {
  document.getElementById('aircraft-count').textContent = state.aircraft.size;
}

function updateReceiverLabel() {
  const el = document.getElementById('receiver-loc');
  if (state.receiver) {
    el.textContent = `${state.receiver.lat.toFixed(2)}, ${state.receiver.lon.toFixed(2)}`;
  } else {
    el.textContent = 'no receiver';
  }
}

function updateMessageRate() {
  const el = document.getElementById('messages-rate');
  if (!el) return;
  if (state.recentRate == null) { el.textContent = '— /s'; return; }
  // Show "contacts/s" alongside a running tally so the user sees activity even when the rate is 0.
  el.textContent = `${state.recentRate.toFixed(1)} ↑/s · ${state.totalMessages.toLocaleString()} total`;
}

// ---------- Sidebar ----------

function passesFilters(ac) {
  const f = state.filters;
  if (!f.showGround && ac.on_ground) return false;
  if (f.text) {
    const t = f.text.toLowerCase();
    const haystack = [ac.callsign, ac.registration, ac.hex, ac.type_code].filter(Boolean).join(' ').toLowerCase();
    if (!haystack.includes(t)) return false;
  }
  if (f.category !== 'all') {
    if (f.category === 'military' && !ac.military) return false;
    if (f.category === 'commercial' && !isCommercial(ac)) return false;
    if (f.category === 'ga' && !isGA(ac)) return false;
    if (f.category === 'heli' && !isHelicopter(ac)) return false;
  }
  const alt = ac.altitude_baro;
  if (alt != null) {
    if (alt < f.altMin || alt > f.altMax) return false;
  }
  if (ac.distance_nm != null && ac.distance_nm > f.distMax) return false;
  return true;
}

function isCommercial(ac) {
  if (!ac.category) return !ac.military && ac.callsign;
  return /^A[1-5]$/.test(ac.category);
}
function isGA(ac) {
  if (!ac.category) return false;
  return ac.category === 'A1' || ac.category === 'A0';
}
function isHelicopter(ac) {
  return ac.category === 'A7' || (ac.type_code || '').startsWith('H');
}

function compareAircraft(a, b) {
  const dir = 1;
  const key = state.filters.sort;
  const f = (v, fallback = Infinity) => (v == null ? fallback : v);
  switch (key) {
    case 'callsign': return (a.display_name || '').localeCompare(b.display_name || '') * dir;
    case 'altitude': return (f(a.altitude_baro, -1) - f(b.altitude_baro, -1)) * -dir; // descending
    case 'speed':    return (f(a.ground_speed, -1) - f(b.ground_speed, -1)) * -dir;
    case 'signal':   return (f(a.rssi, -1000) - f(b.rssi, -1000)) * -dir;
    case 'distance':
    default:
      return (f(a.distance_nm) - f(b.distance_nm)) * dir;
  }
}

/* Diff-based sidebar rendering: re-using existing <li> elements keyed by hex and updating
 * their text content in place is ~10× cheaper than rebuilding 400 DOM nodes every poll.
 * The row layout is cached in a closure-scoped Map so we don't repeatedly query the DOM. */
const sidebarRows = new Map();   // hex → { li, refs: { callsign, typeReg, altitude, speed, dot } }

function renderSidebar() {
  const list = document.getElementById('aircraft-list');
  const visible = [...state.aircraft.values()].filter(passesFilters).sort(compareAircraft);
  const visibleHexes = new Set(visible.map((a) => a.hex));

  // Remove rows that no longer pass filters.
  for (const [hex, row] of sidebarRows) {
    if (!visibleHexes.has(hex)) {
      row.li.remove();
      sidebarRows.delete(hex);
    }
  }

  // Walk the desired order and either move existing rows or create new ones.
  let prev = null;
  for (const ac of visible) {
    let row = sidebarRows.get(ac.hex);
    if (!row) {
      row = createSidebarRow(ac);
      sidebarRows.set(ac.hex, row);
    }
    updateSidebarRow(row, ac);
    // Insert after `prev` if not already there (avoids unnecessary moves).
    const expectedNextSibling = prev ? prev.nextSibling : list.firstChild;
    if (row.li !== expectedNextSibling) {
      list.insertBefore(row.li, expectedNextSibling);
    }
    prev = row.li;
  }
}

function createSidebarRow(ac) {
  const li = document.createElement('li');
  li.className = 'aircraft-row';
  li.dataset.hex = ac.hex;
  // Build the static skeleton once. We'll update text and class state in updateSidebarRow.
  li.innerHTML = `
    <span class="alt-dot"></span>
    <div class="aircraft-info">
      <div class="callsign"><span class="callsign-text"></span><span class="badges"></span></div>
      <div class="type-reg"></div>
    </div>
    <div class="aircraft-stats">
      <span class="altitude"></span>
      <span class="speed-dist"></span>
    </div>
    <span class="source-dot"></span>
  `;
  li.addEventListener('click', () => selectAircraft(ac.hex, { pan: true }));
  return {
    li,
    refs: {
      dot: li.querySelector('.alt-dot'),
      callsign: li.querySelector('.callsign-text'),
      badges: li.querySelector('.badges'),
      typeReg: li.querySelector('.type-reg'),
      altitude: li.querySelector('.altitude'),
      speedDist: li.querySelector('.speed-dist'),
      sourceDot: li.querySelector('.source-dot'),
    },
  };
}

function updateSidebarRow(row, ac) {
  const r = row.refs;
  // Cheap per-field guards — only touch the DOM when the value actually changes.
  const name = ac.display_name;
  if (r.callsign.textContent !== name) r.callsign.textContent = name;
  const typeReg = [ac.type_code, ac.registration].filter(Boolean).join(' · ') || ' ';
  if (r.typeReg.textContent !== typeReg) r.typeReg.textContent = typeReg;
  const altText = `${formatAltitude(ac)} ${trendArrow(ac)}`;
  if (r.altitude.textContent !== altText) r.altitude.textContent = altText;
  const speedDist = `${formatSpeed(ac)} · ${formatDistance(ac)}`;
  if (r.speedDist.textContent !== speedDist) r.speedDist.textContent = speedDist;

  // Badges only re-render when their set changes.
  const badgesKey = `${ac.military}|${ac.is_emergency_squawk}|${isWatchlisted(ac)}`;
  if (r.badges.dataset.key !== badgesKey) {
    r.badges.dataset.key = badgesKey;
    r.badges.innerHTML = badgesFor(ac);
  }

  // Class state + colour
  r.dot.style.background = bandColorVar(ac.altitude_band);
  const sourceCls = sourceClass(ac.data_source);
  if (r.sourceDot.dataset.cls !== sourceCls) {
    r.sourceDot.className = `source-dot ${sourceCls}`;
    r.sourceDot.dataset.cls = sourceCls;
    r.sourceDot.title = ac.data_source || '';
  }
  row.li.classList.toggle('selected', ac.hex === state.selectedHex);
}

function badgesFor(ac) {
  const out = [];
  if (ac.military) out.push('<span class="badge mil" title="Military">MIL</span>');
  if (ac.is_emergency_squawk) out.push(`<span class="badge emg" title="Squawk ${ac.squawk}">EMG</span>`);
  if (isWatchlisted(ac)) out.push('<span class="badge wch" title="Watchlist">WCH</span>');
  return out.join('');
}

function isWatchlisted(ac) {
  const list = (state.settings.watchlist || '').split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
  if (!list.length) return false;
  const keys = [ac.callsign, ac.registration, ac.hex].filter(Boolean).map((s) => s.toUpperCase());
  return keys.some((k) => list.includes(k));
}

function bandColorVar(band) {
  switch (band) {
    case 'ground':    return 'var(--band-ground)';
    case 'low':       return 'var(--band-low)';
    case 'mid':       return 'var(--band-mid)';
    case 'high':      return 'var(--band-high)';
    case 'very_high': return 'var(--band-very-high)';
    default:          return 'var(--accent)';
  }
}

function formatAltitude(ac) {
  if (ac.on_ground) return 'GND';
  if (ac.altitude_baro == null) return '—';
  return `${ac.altitude_baro.toLocaleString()} ft`;
}
function trendArrow(ac) {
  if (ac.vertical_trend === 'climb')   return '↑';
  if (ac.vertical_trend === 'descent') return '↓';
  return '·';
}
function formatSpeed(ac) {
  if (ac.ground_speed == null) return '— kts';
  return `${Math.round(ac.ground_speed)} kts`;
}
function formatDistance(ac) {
  if (ac.distance_nm == null) return '— nm';
  return `${ac.distance_nm.toFixed(0)} nm`;
}

function sourceClass(src) {
  if (!src) return 'other';
  if (src.startsWith('adsb')) return 'adsb';
  if (src === 'mlat') return 'mlat';
  if (src.startsWith('tisb')) return 'tisb';
  if (src === 'mode_s') return 'modes';
  return 'other';
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------- Map setup ----------

function setupMap() {
  map = L.map('map', { zoomControl: true, attributionControl: true }).setView([54, -2], 6);
  // Expose for debugging from the console / preview tools.
  window.__map = map;
  markersLayer = L.layerGroup().addTo(map);
  trailsLayer = L.layerGroup().addTo(map);
  ringsLayer = L.layerGroup().addTo(map);
  routeLineLayer = L.layerGroup().addTo(map);

  applyTileLayerForTheme();

  // Radar canvas
  radarCanvas = document.getElementById('radar-canvas');
  radarCtx = radarCanvas.getContext('2d');
  radar = new RadarSweep(radarCanvas, radarCtx);
  radar.setMap(map);
  radar.onSnapshotChange = () => refreshMapMarkers();

  const resize = () => {
    radar.resize();
    map.invalidateSize();
    refreshMapMarkers();
  };
  resize();
  window.addEventListener('resize', resize);
  // Also catch layout changes that don't fire window resize (e.g. responsive panel toggle, modal open).
  const ro = new ResizeObserver(() => resize());
  ro.observe(document.querySelector('.map-section'));
  // Leaflet handles marker / polyline re-projection natively on pan/zoom — we don't need
  // to rebuild any of them per event. The only state we DO need to sync is the label-pane
  // data attribute (`data-labels-zoomed-out` flips at the configured zoom threshold),
  // and that's cheap to check on `zoomend`.
  map.on('zoomend', syncLabelPaneAttrs);

  // Right-click anywhere on the empty map → menu with "Set receiver here". Marker
  // contextmenu handlers stopPropagation so this only fires on background clicks.
  map.on('contextmenu', (e) => {
    const { lat, lng } = e.latlng;
    showContextMenu(e.originalEvent.clientX, e.originalEvent.clientY, [
      { label: `📍 Set receiver here (${lat.toFixed(3)}, ${lng.toFixed(3)})`,
        run: () => setReceiverLocation(lat, lng) },
      { label: 'Copy coords to clipboard',
        run: () => navigator.clipboard?.writeText(`${lat.toFixed(5)}, ${lng.toFixed(5)}`)
                    .then(() => toast('Coords copied')) },
    ]);
  });

  // Initial pane attribute write so the CSS knows what mode to use immediately.
  syncLabelPaneAttrs();
}

async function setReceiverLocation(lat, lon) {
  // Sanity check — coords outside [-90,90] / [-180,180] are usually a typo or DMS-vs-decimal
  // mix-up; reject loudly rather than silently sending nonsense to adsb.lol.
  if (!Number.isFinite(lat) || lat < -90 || lat > 90 ||
      !Number.isFinite(lon) || lon < -180 || lon > 180) {
    toast(`Invalid coords (${lat}, ${lon}) — latitude must be -90..90, longitude -180..180.`);
    return;
  }
  state.settings.receiver_lat = lat;
  state.settings.receiver_lon = lon;
  // Keep global feed centre in sync so adsb.lol queries also recentre.
  state.settings.global_center_lat = lat;
  state.settings.global_center_lon = lon;
  await fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ receiver_lat: lat, receiver_lon: lon,
                            global_center_lat: lat, global_center_lon: lon }) });
  state.receiver = { lat, lon };
  refreshReceiverMarker();
  refreshRangeRings();
  if (radar) radar.setReceiver(state.receiver);
  // Refresh any open Settings → Connection inputs so they reflect the new values.
  const latI = document.getElementById('setting-global-lat');
  const lonI = document.getElementById('setting-global-lon');
  const rlatI = document.getElementById('setting-receiver-lat');
  const rlonI = document.getElementById('setting-receiver-lon');
  if (latI) latI.value = lat;
  if (lonI) lonI.value = lon;
  if (rlatI) rlatI.value = lat;
  if (rlonI) rlonI.value = lon;
  toast(`Receiver set to ${lat.toFixed(3)}, ${lon.toFixed(3)}`);
}

// "Pick on map" mode — primes the map for a single click that becomes the receiver location.
// Works on any device (touch or pointer) and is more discoverable than right-click.
let _pickMode = false;
function enterPickMode() {
  if (_pickMode) return;
  _pickMode = true;
  closeSettings();
  // Visual hint
  const mapEl = document.getElementById('map');
  mapEl.style.cursor = 'crosshair';
  toast('Click on the map to set your receiver location. Esc to cancel.');
  const onClick = (e) => { finish(e.latlng.lat, e.latlng.lng); };
  const onKey  = (e) => { if (e.key === 'Escape') finish(); };
  function finish(lat, lon) {
    if (!_pickMode) return;
    _pickMode = false;
    mapEl.style.cursor = '';
    map.off('click', onClick);
    document.removeEventListener('keydown', onKey);
    if (lat != null) setReceiverLocation(lat, lon);
  }
  map.on('click', onClick);
  document.addEventListener('keydown', onKey);
}

function fitToData() {
  if (!map) return;
  const positions = [...state.aircraft.values()]
    .filter((a) => a.lat != null && a.lon != null)
    .map((a) => [a.lat, a.lon]);
  if (state.receiver) positions.push([state.receiver.lat, state.receiver.lon]);
  if (positions.length === 0) return;
  const bounds = L.latLngBounds(positions);
  map.fitBounds(bounds, { padding: [40, 40], maxZoom: 9 });
}

function recenterOnReceiver() {
  if (!map || !state.receiver) return;
  map.setView([state.receiver.lat, state.receiver.lon], 7);
}

function applyTileLayerForTheme() {
  const theme = state.settings.theme || 'radar';
  if (tileLayer) {
    map.removeLayer(tileLayer);
    tileLayer = null;
  }
  if (theme === 'terminal') {
    drawCoordinateGrid();
    syncAeroOverlay();
    return;
  }
  removeCoordinateGrid();
  const requested = state.settings.map_style;
  let presetKey = THEME_DEFAULT_TILES[theme] || 'esriDark';
  // Explicit preset overrides the theme default.
  if (requested && requested !== 'automatic' && TILE_PRESETS[requested]) {
    presetKey = requested;
  }
  const preset = TILE_PRESETS[presetKey] || TILE_PRESETS.esriDark;
  tileLayer = L.tileLayer(preset.url, { attribution: preset.attribution, maxZoom: 18 }).addTo(map);
  // Keep the aviation overlay rendering above whichever base tiles are active.
  syncAeroOverlay();
}

function syncAeroOverlay() {
  if (!map) return;
  const enabled = !!state.settings.openaip_overlay_enabled;
  const keySet = !!state.settings.openaip_api_key_set;
  const wantVisible = enabled && keySet;
  if (wantVisible && !aeroOverlayLayer) {
    aeroOverlayLayer = L.tileLayer(API.openaipTiles, {
      attribution: 'Aero © <a href="https://www.openaip.net/" target="_blank" rel="noopener">OpenAIP</a>',
      maxZoom: 14,
      minZoom: 4,
      opacity: 0.85,
      pane: 'overlayPane',
    });
    aeroOverlayLayer.addTo(map);
  } else if (!wantVisible && aeroOverlayLayer) {
    map.removeLayer(aeroOverlayLayer);
    aeroOverlayLayer = null;
  } else if (wantVisible && aeroOverlayLayer) {
    // Force a redraw in case the API key was just updated server-side.
    aeroOverlayLayer.redraw();
  }
  const btn = document.getElementById('toggle-aero');
  if (btn) {
    btn.setAttribute('aria-pressed', wantVisible ? 'true' : 'false');
    btn.classList.toggle('active', wantVisible);
    btn.title = keySet
      ? (enabled ? 'Hide aviation overlay' : 'Show aviation overlay (OpenAIP)')
      : 'Aviation overlay — add an OpenAIP key in Settings → Map';
  }
}

async function toggleAeroOverlay() {
  if (!state.settings.openaip_api_key_set) {
    // No key — open settings on the Map tab so the user can add one.
    openSettings();
    const tab = document.querySelector('.tab[data-tab="map"]');
    if (tab) tab.click();
    return;
  }
  const newEnabled = !state.settings.openaip_overlay_enabled;
  state.settings.openaip_overlay_enabled = newEnabled;
  syncAeroOverlay();
  try {
    await fetch(API.settings, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ openaip_overlay_enabled: newEnabled }),
    });
  } catch (e) {
    // Persistence failure isn't fatal — the toggle still works for this session.
    console.warn('failed to persist openaip toggle', e);
  }
}

async function saveOpenaipKey() {
  const input = document.getElementById('setting-openaip-key');
  const key = (input?.value || '').trim();
  if (!key) return;
  const res = await fetch(API.openaipKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  });
  const data = await res.json();
  state.settings.openaip_api_key_set = !!data.openaip_api_key_set;
  document.getElementById('openaip-key-status').textContent = data.openaip_api_key_set ? 'Key stored.' : 'No key stored.';
  if (input) input.value = '';
  syncAeroOverlay();
}

function drawCoordinateGrid() {
  removeCoordinateGrid();
  coordGridEl = document.createElement('canvas');
  coordGridEl.className = 'coord-grid';
  document.querySelector('.map-section').appendChild(coordGridEl);
  const render = () => {
    const rect = coordGridEl.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    coordGridEl.width = rect.width * dpr;
    coordGridEl.height = rect.height * dpr;
    const ctx = coordGridEl.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent-muted').trim() || '#996b00';
    ctx.lineWidth = 0.5;
    const step = 60;
    for (let x = 0; x < rect.width; x += step) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, rect.height); ctx.stroke();
    }
    for (let y = 0; y < rect.height; y += step) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(rect.width, y); ctx.stroke();
    }
  };
  render();
  window.addEventListener('resize', render);
}

function removeCoordinateGrid() {
  if (coordGridEl) { coordGridEl.remove(); coordGridEl = null; }
}

// ---------- Aircraft markers ----------

function refreshMapMarkers() {
  if (!map) return;
  const seen = new Set();
  const variant = radar?.variant;
  const useSnapshots = !!variant;
  const aircraftToRender = useSnapshots
    ? buildSnapshotAircraft()
    : [...state.aircraft.values()];

  for (const ac of aircraftToRender) {
    if (ac.lat == null || ac.lon == null) continue;
    seen.add(ac.hex);
    const existing = aircraftMarkers.get(ac.hex);
    if (existing) {
      // Diff-skip: stringify everything visible into a signature and only touch the DOM
      // when the signature changes. With 600+ aircraft updating every 2 s, this drops
      // the per-tick CPU dramatically (most aircraft are unchanged from poll to poll).
      const sig = `${ac.lat}|${ac.lon}|${ac.heading ?? 0}|${ac.altitude_band}|`
                + `${ac.is_emergency_squawk?1:0}|${ac.on_ground?1:0}|${ac.military?1:0}|`
                + `${state.selectedHex === ac.hex?1:0}|${state.settings.map_label_mode || 'off'}`;
      if (existing._sig !== sig) {
        existing._sig = sig;
        existing.setLatLng([ac.lat, ac.lon]);
        updateMarkerVisual(existing, ac);
      }
    } else {
      const marker = createMarker(ac);
      marker.addTo(markersLayer);
      aircraftMarkers.set(ac.hex, marker);
    }
  }
  // Drop markers no longer present
  for (const [hex, marker] of aircraftMarkers) {
    if (!seen.has(hex)) {
      markersLayer.removeLayer(marker);
      aircraftMarkers.delete(hex);
    }
  }
}

function buildSnapshotAircraft() {
  const out = [];
  for (const [hex, snap] of radar.snapshots) {
    const ac = state.aircraft.get(hex);
    if (!ac) continue;
    out.push({ ...ac, lat: snap.lat, lon: snap.lon, heading: snap.heading });
  }
  return out;
}

function createMarker(ac) {
  const icon = L.divIcon({
    className: 'aircraft-marker',
    html: MARKER_HTML,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  const marker = L.marker([ac.lat, ac.lon], { icon, riseOnHover: true });
  marker.on('click', () => selectAircraft(ac.hex, { pan: false }));
  marker.on('contextmenu', (ev) => {
    const me = ev.originalEvent;
    me.preventDefault();
    const current = state.aircraft.get(ac.hex);
    showContextMenu(me.clientX, me.clientY, [
      { label: 'Open detail panel', run: () => selectAircraft(ac.hex, { pan: false }) },
      { label: 'Follow this aircraft', run: () => { state.selectedHex = ac.hex; if (!state.follow) toggleFollow(); else toast('Following ' + (current?.display_name || ac.hex)); } },
      { label: 'Copy hex to clipboard', run: () => navigator.clipboard?.writeText(ac.hex).then(() => toast('Copied ' + ac.hex)) },
      { label: 'Open on adsb.fi', run: () => window.open(`https://globe.adsb.fi/?icao=${ac.hex}`, '_blank', 'noopener') },
    ]);
  });
  updateMarkerVisual(marker, ac);
  return marker;
}

function updateMarkerVisual(marker, ac) {
  const el = marker.getElement();
  if (!el) return;
  const isSelected = ac.hex === state.selectedHex;
  const isGround = !!ac.on_ground;
  const heading = ac.heading != null ? ac.heading : 0;
  const scale = isSelected ? 1.4 : (isGround ? 0.55 : 1);
  const svg = el.querySelector('svg');
  if (svg) svg.style.transform = `rotate(${heading}deg) scale(${scale})`;
  let cls = 'aircraft-marker';
  if (isSelected) cls += ' selected';
  if (isGround) cls += ' ground';
  if (ac.is_emergency_squawk) cls += ' emergency';
  if (ac.military) cls += ' military';
  el.className = cls;
  el.style.color = ac.is_emergency_squawk ? 'var(--alert)' : resolveBandColor(ac.altitude_band);
  el.style.zIndex = isSelected ? 1000 : (ac.is_emergency_squawk ? 800 : '');
  updateMarkerLabel(el, ac);
}

function updateMarkerLabel(el, ac) {
  const label = el.querySelector('.ac-label');
  if (!label) return;
  const mode = state.settings.map_label_mode || 'off';
  if (mode === 'off') {
    label.hidden = true;
    return;
  }
  if (mode === 'callsign') {
    label.hidden = false;
    label.className = 'ac-label callsign-only';
    label.textContent = ac.display_name || ac.hex.toUpperCase();
    return;
  }
  // Full mode — multi-line block.
  label.hidden = false;
  label.className = 'ac-label full';
  const regType = [ac.registration, ac.type_code].filter(Boolean).join(' ') || '';
  const alt = ac.on_ground ? 'GND' : (ac.altitude_baro != null ? `${ac.altitude_baro.toLocaleString()}ft` : '—');
  const altArrow = ac.vertical_trend === 'climb' ? '▲' : (ac.vertical_trend === 'descent' ? '▼' : '');
  const speed = ac.ground_speed != null ? `${Math.round(ac.ground_speed)}kt` : '';
  const speedAlt = `${speed} ${altArrow}${alt}`.trim();
  const callsign = ac.display_name || '';
  const route = _routeCache.get(ac.callsign || '');
  let routeStr = '';
  if (route && route.origin_iata && route.destination_iata) {
    routeStr = `${route.origin_iata} - ${route.destination_iata}`;
  }
  label.innerHTML = `
    ${regType ? `<div>${escapeHtml(regType)}</div>` : ''}
    ${speedAlt ? `<div class="ac-label-line2">${escapeHtml(speedAlt)}</div>` : ''}
    ${callsign ? `<div class="ac-label-line3">${escapeHtml(callsign)}</div>` : ''}
    ${routeStr ? `<div class="ac-label-line4 ac-label-route">${escapeHtml(routeStr)}</div>` : ''}
  `;
}

/* Pre-populate the route cache for visible aircraft when in full-label mode. We use the
 * existing /api/enrich/adsbdb endpoint (LRU-cached server-side) and store the result in
 * memory; subsequent label renders read it instantly. */
let _routePrefetchScheduled = false;
function maybePrefetchRoutes() {
  if (state.settings.map_label_mode !== 'full') return;
  if (_routePrefetchScheduled) return;
  _routePrefetchScheduled = true;
  setTimeout(async () => {
    _routePrefetchScheduled = false;
    const callsigns = new Set();
    for (const ac of state.aircraft.values()) {
      if (ac.callsign && !_routeCache.has(ac.callsign) && !_routeInFlight.has(ac.callsign)) {
        callsigns.add(ac.callsign);
      }
    }
    // Cap per-cycle work — at 400 aircraft we'd hammer the server otherwise.
    const batch = [...callsigns].slice(0, 20);
    for (const cs of batch) {
      _routeInFlight.add(cs);
      try {
        const data = await fetch(API.adsbdb(cs)).then((r) => r.json());
        if (data && (data.origin_iata || data.destination_iata)) {
          _routeCache.set(cs, { origin_iata: data.origin_iata, destination_iata: data.destination_iata });
        } else {
          _routeCache.set(cs, null);
        }
      } catch (e) {
        _routeCache.set(cs, null);
      } finally {
        _routeInFlight.delete(cs);
      }
    }
    // Re-render labels for aircraft we just learned about.
    if (batch.length) refreshMapMarkers();
  }, 250);
}

/* Mirror the active label mode + zoom state onto the marker pane via data attributes
 * so the CSS can branch without per-marker class churn. */
function syncLabelPaneAttrs() {
  const pane = document.querySelector('.leaflet-marker-pane');
  if (!pane || !map) return;
  pane.setAttribute('data-labels', state.settings.map_label_mode || 'off');
  const minZoom = parseInt(state.settings.label_full_min_zoom, 10) || 8;
  pane.setAttribute('data-labels-zoomed-out', map.getZoom() < minZoom ? '1' : '0');
}

function resolveBandColor(band) {
  switch (band) {
    case 'low': return themeColors.low;
    case 'mid': return themeColors.mid;
    case 'high': return themeColors.high;
    case 'very_high': return themeColors.veryHigh;
    case 'ground': return themeColors.ground;
    default: return themeColors.accent;
  }
}

// ---------- Trails ----------

function altitudeBandColor(alt) {
  if (alt == null) return themeColors.ground || '#888';
  if (alt < 10000) return themeColors.low;
  if (alt < 25000) return themeColors.mid;
  if (alt < 35000) return themeColors.high;
  return themeColors.veryHigh;
}
function speedColor(speed) {
  if (speed == null) return '#888';
  const t = Math.max(0, Math.min(1, speed / 600));
  const r = Math.round(255 * Math.min(1, t * 2));
  const g = Math.round(255 * Math.min(1, (1 - t) * 2));
  return `rgb(${r},${g},80)`;
}

// Cache the parameters that drove the last trail render per hex, so we can short-circuit
// the (expensive) rebuild for aircraft that didn't actually move this poll.
const _trailRenderCache = new Map();   // hex → { len, mode, fade, isSel, anySel }

function refreshTrails() {
  if (!map) return;
  const mode = state.settings.trail_colour_mode || 'single';
  const fade = state.settings.trail_fade !== false;
  const accent = themeColors.mapTrail;
  const anySel = !!state.selectedHex;
  const seen = new Set();
  for (const [hex, pts] of state.trails) {
    if (!pts.length) continue;
    seen.add(hex);
    const isSel = hex === state.selectedHex;
    // Diff-skip: if nothing that affects the trail's rendering changed since last poll,
    // leave the existing polylines alone. The hot path here is "the aircraft is on the
    // map but didn't move", which is most aircraft on most ticks.
    const cached = _trailRenderCache.get(hex);
    if (cached && cached.len === pts.length && cached.mode === mode && cached.fade === fade
        && cached.isSel === isSel && cached.anySel === anySel) {
      continue;
    }
    _trailRenderCache.set(hex, { len: pts.length, mode, fade, isSel, anySel });

    const baseOpacity = anySel ? (isSel ? 0.95 : 0.18) : 0.55;
    const weight = isSel ? 2.5 : 1.2;
    // Drop any previous rendering for this hex — we always rebuild rather than reuse polyline
    // segments because the per-segment style (colour by altitude band etc.) needs recompute
    // on every change anyway. The diff-skip above is what actually saves CPU.
    const old = aircraftTrails.get(hex);
    if (old) {
      if (Array.isArray(old)) old.forEach((p) => trailsLayer.removeLayer(p));
      else trailsLayer.removeLayer(old);
    }
    if (mode === 'single' && !fade) {
      const poly = L.polyline(pts.map((p) => [p[0], p[1]]),
        { color: accent, weight, opacity: baseOpacity, lineJoin: 'round' });
      poly.addTo(trailsLayer);
      aircraftTrails.set(hex, poly);
    } else {
      const segs = [];
      for (let i = 0; i < pts.length - 1; i++) {
        const a = pts[i], b = pts[i + 1];
        const alt = b[3], speed = b[4];
        let color = accent;
        if (mode === 'altitude') color = altitudeBandColor(alt);
        else if (mode === 'speed') color = speedColor(speed);
        const ageFactor = fade ? ((i + 1) / pts.length) : 1;
        const seg = L.polyline([[a[0], a[1]], [b[0], b[1]]],
          { color, weight, opacity: baseOpacity * ageFactor, lineJoin: 'round' });
        seg.addTo(trailsLayer);
        segs.push(seg);
      }
      aircraftTrails.set(hex, segs);
    }
  }
  for (const [hex, val] of aircraftTrails) {
    if (!seen.has(hex)) {
      if (Array.isArray(val)) val.forEach((p) => trailsLayer.removeLayer(p));
      else trailsLayer.removeLayer(val);
      aircraftTrails.delete(hex);
      _trailRenderCache.delete(hex);
    }
  }
}

// ---------- Range rings + receiver ----------

function refreshRangeRings() {
  if (!ringsLayer) return;
  ringsLayer.clearLayers();
  if (!state.receiver) return;
  if (!state.settings.range_rings_enabled) return;
  const ringStr = state.settings.range_rings_nm || '50,100,150,200';
  const rings = ringStr.split(',').map((s) => parseFloat(s.trim())).filter((n) => Number.isFinite(n) && n > 0);
  const ringColor = themeColors.mapRangeRing;
  for (const nm of rings) {
    const c = L.circle([state.receiver.lat, state.receiver.lon], {
      radius: nm * 1852,
      color: ringColor,
      weight: 1,
      dashArray: '4 6',
      fill: false,
    });
    c.addTo(ringsLayer);
  }
}

function refreshReceiverMarker() {
  if (!map) return;
  if (!state.receiver) {
    if (receiverMarker) { map.removeLayer(receiverMarker); receiverMarker = null; }
    return;
  }
  if (!receiverMarker) {
    // The pulse animation lives on the inner div — keeping it off the Leaflet marker root
    // is essential because the animation's `transform: scale(...)` would otherwise clobber
    // Leaflet's `transform: translate3d(...)` positioning.
    const icon = L.divIcon({ className: 'receiver-marker-host', html: '<div class="receiver-pulse"></div>', iconSize: [14, 14], iconAnchor: [7, 7] });
    receiverMarker = L.marker([state.receiver.lat, state.receiver.lon], { icon, interactive: false, keyboard: false });
    receiverMarker.addTo(map);
  } else {
    receiverMarker.setLatLng([state.receiver.lat, state.receiver.lon]);
  }
}

// ---------- Selection + detail panel ----------

function selectAircraft(hex, { pan }) {
  const prev = state.selectedHex;
  state.selectedHex = hex;
  if (prev) refreshRowSelection(prev, false);
  refreshRowSelection(hex, true);
  refreshMapMarkers();
  refreshTrails();
  routeLineLayer.clearLayers();
  const ac = state.aircraft.get(hex);
  if (pan && ac && ac.lat != null) {
    map.panTo([ac.lat, ac.lon], { animate: true });
  }
  renderDetailPanel(ac);
  // Reflect the selection in the URL so the share-link round-trips.
  if (typeof scheduleUrlStateWrite === 'function') scheduleUrlStateWrite();
}

function refreshRowSelection(hex, on) {
  const row = document.querySelector(`.aircraft-row[data-hex="${hex}"]`);
  if (row) row.classList.toggle('selected', !!on);
}

function renderDetailPanel(ac) {
  const empty = document.getElementById('detail-empty');
  const content = document.getElementById('detail-content');
  if (!ac) {
    empty.hidden = false;
    content.hidden = true;
    return;
  }
  empty.hidden = true;
  content.hidden = false;
  state.selectedEnrichment = { hex: ac.hex, callsign: ac.callsign };
  content.innerHTML = `
    <section>
      <div class="detail-header">
        <span class="callsign">${escapeHtml(ac.display_name)}</span>
        <span class="badges-row">${badgesFor(ac)}</span>
      </div>
      <div class="detail-subtitle" id="detail-subtitle">${escapeHtml([ac.type_code, ac.registration, ac.hex.toUpperCase()].filter(Boolean).join(' · '))}</div>
      <div style="margin-top:6px"><button id="bookmark-toggle" class="secondary" style="font-size:11px; padding:4px 10px">☆ Bookmark</button></div>
    </section>
    <section id="detail-live">${liveSectionHtml(ac)}</section>
    <section><h3>Photo</h3><div class="detail-photo skeleton" id="detail-photo"></div></section>
    <section><h3>Route</h3><div id="detail-route"><div class="skeleton skeleton-line wide"></div><div class="skeleton skeleton-line"></div></div></section>
    <section>
      <h3>FlightAware</h3>
      <div class="fa-action-row">
        <button class="secondary" id="fa-fetch-btn">Fetch flight data</button>
        <span class="hint" id="fa-fetch-hint"></span>
      </div>
      <div id="fa-result"></div>
    </section>
    <section>
      <h3>Quick links</h3>
      <div class="detail-quick-links" id="quick-links">${quickLinks(ac)}</div>
    </section>
  `;
  document.getElementById('fa-fetch-btn').addEventListener('click', () => fetchFlightAware(ac));
  document.getElementById('bookmark-toggle').addEventListener('click', toggleBookmarkSelected);
  updateBookmarkStar();
  fetchEnrichment(ac);
}

function bearingFromReceiver(ac) {
  if (!state.receiver || ac.lat == null || ac.lon == null) return null;
  const lat1 = state.receiver.lat * Math.PI / 180;
  const lat2 = ac.lat * Math.PI / 180;
  const dLon = (ac.lon - state.receiver.lon) * Math.PI / 180;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
}
function compassDirection(deg) {
  if (deg == null) return '';
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(deg / 45) % 8];
}
function compassRoseSvg(track, vrate) {
  // Small instrument-style indicator: ring + cardinal label + heading arrow + vertical-rate text.
  const t = track != null ? Math.round(track) : null;
  const arrow = t != null ? `<g transform="rotate(${t} 30 30)"><path d="M30,8 L35,32 L30,27 L25,32 Z" fill="var(--accent)"/></g>` : '';
  const trackLabel = t != null ? `<text x="30" y="58" text-anchor="middle" font-size="9" fill="var(--primary-text)" font-weight="700">${t}°</text>` : '';
  const v = vrate;
  let vArrow = '';
  if (v != null && Math.abs(v) > 50) {
    const sign = v > 0 ? '▲' : '▼';
    const colour = v > 0 ? 'var(--success)' : 'var(--alert)';
    vArrow = `<text x="46" y="58" font-size="10" fill="${colour}" font-weight="700">${sign}${Math.abs(v)}</text>`;
  }
  return `
    <svg class="compass-rose" viewBox="0 0 60 64" width="64" height="64" aria-hidden="true">
      <circle cx="30" cy="30" r="24" fill="none" stroke="var(--separator)" stroke-width="1"/>
      <text x="30" y="11" text-anchor="middle" font-size="9" fill="var(--secondary-text)">N</text>
      ${arrow}
      ${trackLabel}
      ${vArrow}
    </svg>
  `;
}

function signalBarSvg(rssi) {
  // rssi is in dBFS, typically -50 (weak) to 0 (very strong). Render as 5 stacked bars.
  if (rssi == null) return '<span class="hint">— dBFS</span>';
  const norm = Math.max(0, Math.min(1, (rssi + 50) / 50));   // 0..1
  const filled = Math.max(1, Math.min(5, Math.round(norm * 5)));
  let bars = '';
  for (let i = 0; i < 5; i++) {
    const h = 4 + i * 2;
    const fill = i < filled ? 'var(--accent)' : 'var(--separator)';
    bars += `<rect x="${i * 4}" y="${10 - h}" width="3" height="${h}" fill="${fill}"/>`;
  }
  return `<span class="signal-cluster"><svg viewBox="0 0 19 10" width="36" height="14" aria-hidden="true">${bars}</svg><span class="signal-num">${rssi.toFixed(1)} dBFS</span></span>`;
}

function liveSectionHtml(ac) {
  const eta = etaToOverhead(ac);
  const bearing = bearingFromReceiver(ac);
  const vrate = ac.baro_rate != null ? ac.baro_rate : ac.geom_rate;
  const altStr = ac.on_ground ? 'GND'
                              : (ac.altitude_baro != null ? `${ac.altitude_baro.toLocaleString()} ft` : '—');
  const speedStr = ac.ground_speed != null ? `${Math.round(ac.ground_speed)} kt` : '—';
  const distStr = ac.distance_nm != null ? `${ac.distance_nm.toFixed(1)} nm` : '—';
  const bearingStr = bearing != null ? `${Math.round(bearing)}° ${compassDirection(bearing)}` : '—';

  // Performance fields — only render rows whose value is present.
  const perf = [
    ['IAS', ac.ias != null ? `${Math.round(ac.ias)} kt` : null],
    ['TAS', ac.tas != null ? `${Math.round(ac.tas)} kt` : null],
    ['Mach', ac.mach != null ? ac.mach.toFixed(3) : null],
    ['Geom. alt.', ac.altitude_geom != null ? `${ac.altitude_geom.toLocaleString()} ft` : null],
    ['Roll', ac.roll != null ? `${ac.roll.toFixed(1)}° ${Math.abs(ac.roll) < 1 ? 'level' : (ac.roll > 0 ? 'right' : 'left')}` : null],
    ['Turn rate', ac.track_rate != null ? `${ac.track_rate >= 0 ? '+' : ''}${ac.track_rate.toFixed(1)} °/s` : null],
  ].filter((r) => r[1] != null);
  const autopilot = [
    ['Selected alt.', ac.nav_altitude_mcp != null ? `${ac.nav_altitude_mcp.toLocaleString()} ft` : null],
    ['Selected QNH', ac.nav_qnh != null ? `${ac.nav_qnh.toFixed(1)} mb` : null],
  ].filter((r) => r[1] != null);
  const extras = [
    ['CPA', eta ? `${eta.minutes.toFixed(1)} min · ${eta.distance_nm.toFixed(1)} nm` : null],
    ['Position', ac.lat != null ? `${ac.lat.toFixed(4)}, ${ac.lon.toFixed(4)}` : null],
    ['Source', sourceLabel(ac.data_source)],
    ['Last seen', ac.seen != null ? `${ac.seen.toFixed(1)} s ago` : null],
  ].filter((r) => r[1] != null);

  const squawkChip = ac.squawk
    ? `<span class="chip ${ac.is_emergency_squawk ? 'chip-alert' : ''}"># SQ ${escapeHtml(ac.squawk)}${ac.is_emergency_squawk ? ` · ${escapeHtml(ac.emergency)}` : ''}</span>`
    : '';

  return `
    <h3>Live</h3>
    <div class="live-grid">
      <div class="live-stats">
        <div class="stat-row"><span class="stat-label">Altitude</span><span class="stat-val">${escapeHtml(altStr)}</span></div>
        <div class="stat-row"><span class="stat-label">Speed</span><span class="stat-val">${escapeHtml(speedStr)}</span></div>
        <div class="stat-row"><span class="stat-label">Distance</span><span class="stat-val">${escapeHtml(distStr)}</span></div>
        <div class="stat-row"><span class="stat-label">Bearing</span><span class="stat-val">${escapeHtml(bearingStr)}</span></div>
        <div class="stat-row"><span class="stat-label">Signal</span><span class="stat-val">${signalBarSvg(ac.rssi)}</span></div>
      </div>
      <div class="live-compass">
        ${compassRoseSvg(ac.track, vrate)}
        ${vrate != null ? `<div class="vrate">${vrate > 0 ? '+' : ''}${vrate} fpm</div>` : ''}
      </div>
    </div>
    ${squawkChip ? `<div class="chip-row">${squawkChip}</div>` : ''}
    ${perf.length ? `
      <h4 class="subhead">⊕ Performance</h4>
      <dl class="detail-grid compact">
        ${perf.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}
      </dl>` : ''}
    ${autopilot.length ? `
      <h4 class="subhead">▶ Autopilot</h4>
      <dl class="detail-grid compact">
        ${autopilot.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}
      </dl>` : ''}
    ${extras.length ? `
      <h4 class="subhead">⌖ More</h4>
      <dl class="detail-grid compact">
        ${extras.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}
      </dl>` : ''}
  `;
}

function sourceLabel(src) {
  if (!src) return '—';
  if (src.startsWith('adsb')) return 'ADS-B';
  if (src === 'mlat') return 'MLAT';
  if (src.startsWith('tisb')) return 'TIS-B';
  if (src === 'mode_s') return 'Mode-S';
  return src;
}

function quickLinks(ac) {
  const cs = encodeURIComponent(ac.callsign || '');
  const hex = encodeURIComponent(ac.hex);
  const reg = encodeURIComponent(ac.registration || '');
  const links = [];
  if (ac.callsign) links.push(`<a target="_blank" rel="noopener" href="https://flightaware.com/live/flight/${cs}">FlightAware</a>`);
  if (ac.callsign) links.push(`<a target="_blank" rel="noopener" href="https://www.flightradar24.com/${cs}">FR24</a>`);
  links.push(`<a target="_blank" rel="noopener" href="https://globe.adsb.fi/?icao=${hex}">adsb.fi</a>`);
  links.push(`<a target="_blank" rel="noopener" href="https://www.planespotters.net/hex/${hex}">Planespotters</a>`);
  if (reg) links.push(`<a target="_blank" rel="noopener" href="https://www.jetphotos.com/photo/keyword/${reg}">JetPhotos</a>`);
  return links.join('');
}

function updateDetailLiveSection() {
  if (!state.selectedHex) return;
  const ac = state.aircraft.get(state.selectedHex);
  const live = document.getElementById('detail-live');
  if (!live) return;
  if (!ac) {
    // Aircraft left coverage
    live.innerHTML = `<h3>Live</h3><p class="hint err">Aircraft has left coverage.</p>`;
    return;
  }
  live.innerHTML = liveSectionHtml(ac);
}

// ---------- Enrichment fetches ----------

async function fetchEnrichment(ac) {
  // hexdb (registration, type)
  const hex = ac.hex;
  cachedFetch('hexdb', hex, () => fetch(API.hexdb(hex)).then((r) => r.json())).then((data) => {
    if (!data) return;
    const subtitle = document.getElementById('detail-subtitle');
    if (subtitle && (data.manufacturer || data.type || data.registered_owners)) {
      const parts = [data.manufacturer && data.type ? `${data.manufacturer} ${data.type}` : (data.type || data.manufacturer), data.registration || ac.registration, data.registered_owners].filter(Boolean);
      subtitle.textContent = parts.join(' · ');
    }
  });

  // photo gallery — render a small carousel for any aircraft with more than one photo.
  const photoEl = document.getElementById('detail-photo');
  if (photoEl) {
    cachedFetch('photo', hex, () => fetch(API.photo(hex)).then((r) => r.json())).then((data) => {
      if (!photoEl.isConnected) return;
      const photos = Array.isArray(data?.photos) ? data.photos
        : (data?.photo_url ? [{ url: data.photo_url, photographer: data.photographer, link: data.link }] : []);
      renderPhotoGallery(photos);
    }).catch(() => { renderPhotoGallery([]); });
  }

  // Personal note on this aircraft.
  renderNoteField(hex);

  // route
  const routeEl = document.getElementById('detail-route');
  if (routeEl) {
    if (!ac.callsign) { routeEl.innerHTML = '<span class="hint">No callsign — cannot look up route.</span>'; return; }
    cachedFetch('adsbdb', ac.callsign, () => fetch(API.adsbdb(ac.callsign)).then((r) => r.json())).then((data) => {
      if (!routeEl.isConnected) return;
      if (data && data.origin_icao && data.destination_icao) {
        routeEl.innerHTML = `
          <div class="route-line">
            <div class="airport">
              <div class="code">${escapeHtml(data.origin_iata || data.origin_icao)}</div>
              <div class="name">${escapeHtml(data.origin_name || '')}</div>
            </div>
            <div class="arrow">→</div>
            <div class="airport">
              <div class="code">${escapeHtml(data.destination_iata || data.destination_icao)}</div>
              <div class="name">${escapeHtml(data.destination_name || '')}</div>
            </div>
          </div>
          ${data.airline_name ? `<div class="hint" style="margin-top:6px">${escapeHtml(data.airline_name)}</div>` : ''}
        `;
        drawRouteLineOnMap(data, ac);
      } else {
        routeEl.innerHTML = '<span class="hint">No route found</span>';
      }
    }).catch(() => routeEl.innerHTML = '<span class="hint err">Route lookup failed</span>');
  }
}

function cachedFetch(cacheKey, key, fn) {
  const cache = state.enrichmentCache[cacheKey];
  if (!cache) return fn();
  if (cache.has(key)) return Promise.resolve(cache.get(key));
  return fn().then((data) => {
    cache.set(key, data);
    return data;
  });
}

function drawRouteLineOnMap(route, ac) {
  routeLineLayer.clearLayers();
  if (route.origin_lat != null && route.destination_lat != null) {
    const acPos = ac.lat != null ? [ac.lat, ac.lon] : null;
    const origin = [route.origin_lat, route.origin_lon];
    const dest = [route.destination_lat, route.destination_lon];
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    const style = { color: accent, weight: 1.5, dashArray: '4 6', opacity: 0.8 };
    if (acPos) {
      L.polyline([origin, acPos], style).addTo(routeLineLayer);
      L.polyline([acPos, dest], style).addTo(routeLineLayer);
    } else {
      L.polyline([origin, dest], style).addTo(routeLineLayer);
    }
  }
}

// ---------- FlightAware ----------

async function fetchFlightAware(ac) {
  if (!ac.callsign) {
    document.getElementById('fa-fetch-hint').textContent = 'No callsign available';
    return;
  }
  const btn = document.getElementById('fa-fetch-btn');
  const hint = document.getElementById('fa-fetch-hint');
  const result = document.getElementById('fa-result');
  btn.disabled = true;
  hint.textContent = 'Loading…';

  let res;
  try {
    res = await fetch(API.faLookup(ac.callsign, false), { method: 'POST' });
  } catch (e) {
    hint.textContent = 'Network error';
    btn.disabled = false;
    return;
  }
  let payload = await res.json();
  if (payload.blocked === 'over_budget') {
    const ok = confirm(`Monthly FlightAware budget reached ($${(payload.budget.spent_cents / 100).toFixed(2)} of $${(payload.budget.limit_cents / 100).toFixed(2)}). Continue anyway?`);
    if (!ok) { btn.disabled = false; hint.textContent = 'Cancelled'; return; }
    res = await fetch(API.faLookup(ac.callsign, true), { method: 'POST' });
    payload = await res.json();
  }
  btn.disabled = false;
  hint.textContent = '';
  if (payload.error) {
    result.innerHTML = `<p class="hint err">${escapeHtml(payload.error)}</p>`;
    return;
  }
  if (!payload.flight) {
    result.innerHTML = `<p class="hint">No flight match found for ${escapeHtml(ac.callsign)}.</p>`;
    return;
  }
  result.innerHTML = renderFlightAwareFlight(payload.flight);
  if (payload.budget) updateBudgetBar(payload.budget);
}

function renderFlightAwareFlight(f) {
  const fmt = (k) => f[k] ? new Date(f[k]).toLocaleTimeString() : '—';
  const block = (label, kind) => `
    <dt>${label} ${kind.toUpperCase()}</dt>
    <dd>${fmt('scheduled_' + kind)} <span class="hint">sch</span> · ${fmt('estimated_' + kind)} <span class="hint">est</span> · ${fmt('actual_' + kind)} <span class="hint">act</span></dd>
  `;
  return `
    <dl class="detail-grid">
      <dt>Operator</dt><dd>${escapeHtml(f.operator || '—')}${f.operator_iata ? ` (${f.operator_iata})` : ''}</dd>
      <dt>Aircraft</dt><dd>${escapeHtml(f.aircraft_type || '—')} · ${escapeHtml(f.registration || '—')}</dd>
      <dt>Status</dt><dd>${escapeHtml(f.status || '—')}${f.progress_percent != null ? ` (${f.progress_percent}%)` : ''}</dd>
      <dt>Origin</dt><dd>${escapeHtml(f.origin_code_icao || '—')}${f.gate_origin ? ' · gate ' + escapeHtml(f.gate_origin) : ''}${f.terminal_origin ? ' · term ' + escapeHtml(f.terminal_origin) : ''}</dd>
      <dt>Destination</dt><dd>${escapeHtml(f.destination_code_icao || '—')}${f.gate_destination ? ' · gate ' + escapeHtml(f.gate_destination) : ''}${f.terminal_destination ? ' · term ' + escapeHtml(f.terminal_destination) : ''}${f.baggage_claim ? ' · bag ' + escapeHtml(f.baggage_claim) : ''}</dd>
      <dt>Route</dt><dd>${escapeHtml(f.route || '—')}</dd>
      <dt>Filed</dt><dd>${f.filed_altitude != null ? f.filed_altitude * 100 + ' ft' : '—'} · ${f.filed_airspeed != null ? f.filed_airspeed + ' kts' : '—'}</dd>
      ${block('OUT (gate)', 'out')}
      ${block('OFF (wheels up)', 'off')}
      ${block('ON (wheels down)', 'on')}
      ${block('IN (gate)', 'in')}
    </dl>
  `;
}

// ---------- Notifications ----------

function fireNotifications() {
  const s = state.settings;
  const browserOK = (typeof Notification !== 'undefined') && Notification.permission === 'granted';
  for (const ac of state.aircraft.values()) {
    if (state.notifiedHexes.has(ac.hex)) continue;
    let kind = null;
    if (s.notify_military && ac.military) kind = 'military';
    else if (s.notify_emergency && ac.is_emergency_squawk) kind = 'emergency';
    else if (s.notify_watchlist && isWatchlisted(ac)) kind = 'watchlist';
    if (!kind) continue;
    state.notifiedHexes.add(ac.hex);
    if (browserOK) {
      const title = kind === 'emergency' ? 'Emergency squawk!'
                  : kind === 'military'  ? 'Military aircraft detected'
                  : 'Watchlist match';
      notify(title, `${ac.display_name} · ${formatDistance(ac)}`);
    }
    if (state.audioEnabled) chimeFor(kind, ac);
    if (kind === 'emergency') autoSnapshotEmergency(ac);
    // Bump events badge — full list is loaded on demand from the server.
    state.eventsUnread += 1;
    updateEventsBadge();
  }
}

function updateEventsBadge() {
  const b = document.getElementById('events-badge');
  if (!b) return;
  if (state.eventsUnread > 0) {
    b.hidden = false;
    b.textContent = state.eventsUnread > 99 ? '99+' : String(state.eventsUnread);
  } else {
    b.hidden = true;
  }
}

/* Trigger a one-off PNG screenshot of the map area when an emergency squawk fires.
 * We use the browser's existing canvas — the Leaflet tile pane is HTML, so we draw
 * everything we have into a fresh canvas (markers via dom-to-image would be heavy;
 * instead we save the marker locations + map URL into a metadata JSON the user can use). */
async function autoSnapshotEmergency(ac) {
  try {
    const url = `https://globe.adsb.fi/?icao=${ac.hex}`;
    const blob = new Blob([JSON.stringify({
      ts: new Date().toISOString(),
      aircraft: ac, receiver: state.receiver, external_link: url,
    }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `emergency-${ac.hex}-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { console.warn('emergency snapshot failed', e); }
}

function notify(title, body) {
  try { new Notification(title, { body, icon: '/piscope/static/icons/aircraft.svg' }); } catch (e) {}
}

// ---------- Settings ----------

async function loadSettings() {
  const res = await fetch(API.settings);
  state.settings = await res.json();
  applySettingsToUI();
  populateSettingsModal();
  await refreshBudget();
}

function applySettingsToUI() {
  const theme = state.settings.theme || 'radar';
  document.documentElement.dataset.theme = theme;
  document.getElementById('theme-select').value = theme;
  refreshThemeColorCache();
  state.filters.showGround = !!state.settings.show_ground;
  document.getElementById('show-ground').checked = state.filters.showGround;

  // Persisted feature flags ↔ runtime state
  state.audioEnabled = !!state.settings.audio_alerts_enabled;
  state.audioDirectional = !!state.settings.audio_directional;
  state.follow = !!state.settings.follow_selected;
  syncToggleButtons();
  // Re-apply optional overlays based on persisted settings.
  if (map) {
    syncWeatherOverlay();
    if (state.settings.day_night_enabled) drawTerminator(); else removeTerminator();
  }

  // tell radar which variant to use
  if (radar) radar.setVariant(RADAR_VARIANTS[theme] || null);
  radar?.setAntennaRange(parseFloat(state.settings.antenna_range_nm) || 200);

  // re-apply map tiles for the new theme
  if (map) applyTileLayerForTheme();
  if (map && state.receiver) refreshRangeRings();
  if (map) syncAeroOverlay();
  if (map) {
    syncLabelPaneAttrs();
    refreshMapMarkers();   // re-render labels in the new mode
    maybePrefetchRoutes();
  }
}

function syncToggleButtons() {
  const setBtn = (id, on, titleOn, titleOff) => {
    const b = document.getElementById(id); if (!b) return;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.title = on ? titleOn : titleOff;
  };
  setBtn('toggle-audio', state.audioEnabled, 'Audio alerts on — click to mute (A)', 'Audio alerts (off) (A)');
  const audioBtn = document.getElementById('toggle-audio');
  if (audioBtn) audioBtn.textContent = state.audioEnabled ? '🔊' : '🔇';
  setBtn('toggle-follow', state.follow, 'Follow mode on (F)', 'Follow selected aircraft (F)');
  setBtn('toggle-replay', state.replayActive, 'Exit replay (return to live) (P)', 'Replay timeline (P)');
  setBtn('toggle-weather', !!state.settings.weather_overlay_enabled, 'Weather radar visible (W)', 'Weather radar overlay (W)');
  setBtn('toggle-day-night', !!state.settings.day_night_enabled, 'Day/night shading on (N)', 'Day/night terminator (N)');
  setBtn('toggle-heatmap', !!heatmapLayer, 'Heatmap visible', 'Traffic heatmap');
}

function populateSettingsModal() {
  const s = state.settings;
  document.querySelector(`input[name="feed_mode"][value="${s.feed_mode || 'global'}"]`).checked = true;
  document.getElementById('setting-tar-url').value = s.tar1090_base_url || '';
  document.getElementById('setting-poll').value = s.poll_interval || 2;
  document.getElementById('setting-global-lat').value = s.global_center_lat ?? '';
  document.getElementById('setting-global-lon').value = s.global_center_lon ?? '';
  document.getElementById('setting-global-rad').value = s.global_radius_nm || 250;

  const themeSelect = document.getElementById('setting-theme');
  if (themeSelect && !themeSelect.options.length) {
    for (const opt of document.getElementById('theme-select').options) {
      const o = document.createElement('option');
      o.value = opt.value; o.textContent = opt.textContent;
      themeSelect.appendChild(o);
    }
  }
  themeSelect.value = s.theme || 'radar';

  document.getElementById('setting-rings').value = s.range_rings_nm || '50,100,150,200';
  document.getElementById('setting-rings-enabled').checked = !!s.range_rings_enabled;
  document.getElementById('setting-antenna-range').value = s.antenna_range_nm || 200;
  document.getElementById('setting-trail').value = s.trail_length || 30;

  // Populate the map-style dropdown lazily — once. Always re-select the stored value.
  const mapStyleSel = document.getElementById('setting-map-style');
  if (mapStyleSel && mapStyleSel.options.length <= 1) {
    for (const [key, preset] of Object.entries(TILE_PRESETS)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = preset.label;
      mapStyleSel.appendChild(opt);
    }
  }
  mapStyleSel.value = s.map_style && (TILE_PRESETS[s.map_style] || s.map_style === 'automatic') ? s.map_style : 'automatic';

  document.getElementById('setting-fa-key').value = '';
  document.getElementById('fa-key-status').textContent = s.fa_api_key_set ? 'Key stored.' : 'No key stored.';
  document.getElementById('setting-fa-limit').value = ((s.fa_monthly_limit_cents || 0) / 100).toFixed(2);

  const openaipKeyInput = document.getElementById('setting-openaip-key');
  if (openaipKeyInput) openaipKeyInput.value = '';
  const openaipStatus = document.getElementById('openaip-key-status');
  if (openaipStatus) openaipStatus.textContent = s.openaip_api_key_set ? 'Key stored.' : 'No key stored — overlay is disabled until you add one.';

  document.getElementById('setting-notify-military').checked = !!s.notify_military;
  document.getElementById('setting-notify-emergency').checked = !!s.notify_emergency;
  document.getElementById('setting-notify-watchlist').checked = !!s.notify_watchlist;
  document.getElementById('setting-audio-enabled').checked = !!s.audio_alerts_enabled;
  document.getElementById('setting-audio-directional').checked = !!s.audio_directional;
  document.getElementById('setting-watchlist').value = s.watchlist || '';

  document.getElementById('setting-receiver-lat').value = s.receiver_lat ?? '';
  document.getElementById('setting-receiver-lon').value = s.receiver_lon ?? '';
  document.getElementById('setting-contact-url').value = s.contact_url || '';
  document.getElementById('setting-trail-colour').value = s.trail_colour_mode || 'single';
  document.getElementById('setting-trail-fade').checked = s.trail_fade !== false;
  document.getElementById('setting-backup-dir').value = s.daily_backup_dir || '';
  document.getElementById('setting-label-mode').value = s.map_label_mode || 'off';
  document.getElementById('setting-label-min-zoom').value = s.label_full_min_zoom || 8;

  // Render extra feeds back as "Name|url" lines.
  try {
    const arr = JSON.parse(s.extra_feeds_json || '[]');
    document.getElementById('setting-extra-feeds').value =
      (Array.isArray(arr) ? arr : []).map((f) => `${f.name || ''}|${f.url || ''}`).join('\n');
  } catch (e) {
    document.getElementById('setting-extra-feeds').value = '';
  }
}

// Latitude / longitude validators — returns the parsed number or null. Anything outside the
// real-world range is rejected (the most common typo is DMS like 550413.5 instead of 55.07).
function _parseLat(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) && n >= -90 && n <= 90 ? n : null;
}
function _parseLon(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) && n >= -180 && n <= 180 ? n : null;
}

async function saveSettings() {
  // Pre-validate any coordinate fields and bail with a useful message if they're out of range,
  // so a typo doesn't take the global feed offline silently.
  const fields = {
    'setting-global-lat':  _parseLat(document.getElementById('setting-global-lat').value),
    'setting-global-lon':  _parseLon(document.getElementById('setting-global-lon').value),
    'setting-receiver-lat':_parseLat(document.getElementById('setting-receiver-lat').value),
    'setting-receiver-lon':_parseLon(document.getElementById('setting-receiver-lon').value),
  };
  for (const [id, val] of Object.entries(fields)) {
    const raw = document.getElementById(id).value;
    if (raw.trim() !== '' && val === null) {
      const isLat = id.endsWith('-lat');
      toast(`${isLat ? 'Latitude' : 'Longitude'} "${raw}" is out of range (${isLat ? '-90..90' : '-180..180'}). Decimal degrees, e.g. 55.07.`);
      document.getElementById(id).focus();
      return;
    }
  }
  const body = {
    feed_mode: document.querySelector('input[name="feed_mode"]:checked')?.value || 'global',
    tar1090_base_url: document.getElementById('setting-tar-url').value.trim(),
    poll_interval: parseInt(document.getElementById('setting-poll').value, 10) || 2,
    global_center_lat: fields['setting-global-lat'],
    global_center_lon: fields['setting-global-lon'],
    global_radius_nm: parseInt(document.getElementById('setting-global-rad').value, 10) || 250,
    theme: document.getElementById('setting-theme').value,
    map_style: document.getElementById('setting-map-style').value,
    range_rings_nm: document.getElementById('setting-rings').value.trim(),
    range_rings_enabled: document.getElementById('setting-rings-enabled').checked,
    antenna_range_nm: parseInt(document.getElementById('setting-antenna-range').value, 10) || 200,
    trail_length: parseInt(document.getElementById('setting-trail').value, 10) || 30,
    fa_monthly_limit_cents: Math.round(parseFloat(document.getElementById('setting-fa-limit').value || 0) * 100),
    notify_military: document.getElementById('setting-notify-military').checked,
    notify_emergency: document.getElementById('setting-notify-emergency').checked,
    notify_watchlist: document.getElementById('setting-notify-watchlist').checked,
    audio_alerts_enabled: document.getElementById('setting-audio-enabled').checked,
    audio_directional: document.getElementById('setting-audio-directional').checked,
    watchlist: document.getElementById('setting-watchlist').value.trim(),
    receiver_lat: fields['setting-receiver-lat'],
    receiver_lon: fields['setting-receiver-lon'],
    extra_feeds_json: parseExtraFeeds(document.getElementById('setting-extra-feeds').value),
    contact_url: document.getElementById('setting-contact-url').value.trim(),
    trail_colour_mode: document.getElementById('setting-trail-colour').value,
    trail_fade: document.getElementById('setting-trail-fade').checked,
    daily_backup_dir: document.getElementById('setting-backup-dir').value.trim(),
    map_label_mode: document.getElementById('setting-label-mode').value,
    label_full_min_zoom: parseInt(document.getElementById('setting-label-min-zoom').value, 10) || 8,
  };
  const res = await fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  state.settings = await res.json();
  await saveWebhooks();  // persist whatever's in the settings modal's webhook editor
  applySettingsToUI();
  closeSettings();
  refreshBudget();
}

function parseExtraFeeds(raw) {
  const feeds = [];
  for (const line of (raw || '').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parts = trimmed.split('|');
    if (parts.length < 2) continue;
    const name = parts[0].trim();
    const url = parts.slice(1).join('|').trim();   // allow | in URLs (rare)
    if (!name || !url) continue;
    if (!/^https?:\/\//.test(url)) continue;       // basic sanity
    feeds.push({ name, url, type: 'tar1090' });
  }
  return JSON.stringify(feeds);
}

async function saveFaKey() {
  const key = document.getElementById('setting-fa-key').value.trim();
  if (!key) return;
  const res = await fetch(API.faKey, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  const data = await res.json();
  document.getElementById('fa-key-status').textContent = data.fa_api_key_set ? 'Key stored.' : 'No key stored.';
  document.getElementById('setting-fa-key').value = '';
}

async function refreshBudget() {
  try {
    const res = await fetch(API.faBudget);
    const data = await res.json();
    updateBudgetBar(data);
  } catch (e) {}
}

function updateBudgetBar(b) {
  document.getElementById('fa-spent').textContent = `$${(b.spent_cents / 100).toFixed(2)}`;
  document.getElementById('fa-limit-display').textContent = `$${(b.limit_cents / 100).toFixed(2)}`;
  const box = document.getElementById('fa-budget-box');
  const pct = b.limit_cents ? Math.min(100, (b.spent_cents / b.limit_cents) * 100) : 0;
  document.getElementById('fa-budget-bar-fill').style.width = pct + '%';
  box.classList.toggle('over', !!b.over_budget);
}

// ---------- Test connection ----------

async function testConnection() {
  const url = document.getElementById('setting-tar-url').value.trim();
  const out = document.getElementById('test-result');
  if (!url) { out.textContent = 'Enter a tar1090 URL first.'; return; }
  out.textContent = 'Testing…';
  try {
    const res = await fetch(API.testConn, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
    const data = await res.json();
    if (data.ok) {
      out.className = 'hint ok';
      out.textContent = `OK — ${data.count} aircraft visible`;
    } else {
      out.className = 'hint err';
      out.textContent = `Failed: ${data.error || 'unknown'}`;
    }
  } catch (e) {
    out.className = 'hint err';
    out.textContent = `Failed: ${e.message}`;
  }
}

// ---------- Modal + UI wiring ----------

function openSettings()  { document.getElementById('settings-modal').hidden = false; loadWebhooksIntoSettings(); }
function closeSettings() { document.getElementById('settings-modal').hidden = true; }

/* ---------- Weather radar overlay (RainViewer, no key) -------------------- */
async function syncWeatherOverlay() {
  const enabled = !!state.settings.weather_overlay_enabled;
  if (enabled && !weatherOverlayLayer && map) {
    try {
      const res = await fetch('https://api.rainviewer.com/public/weather-maps.json');
      const data = await res.json();
      const frame = (data.radar?.past || []).slice(-1)[0];
      if (!frame) return;
      // Use RainViewer's 512px tiles — sharper at high zoom than the default 256px.
      // RainViewer serves data up to z12 reliably; Leaflet upscales beyond that with
      // `maxNativeZoom`, so the layer stays useful all the way to street level instead
      // of cutting out at z10 like the old config did.
      const url = `${data.host}${frame.path}/512/{z}/{x}/{y}/4/1_1.png`;
      weatherOverlayLayer = L.tileLayer(url, {
        opacity: 0.55, attribution: '© RainViewer',
        minZoom: 3, maxZoom: 18, minNativeZoom: 3, maxNativeZoom: 12,
        tileSize: 512, zoomOffset: -1,    // 512px tile, account for it
        pane: 'overlayPane',
      });
      weatherOverlayLayer.addTo(map);
    } catch (e) { console.warn('weather overlay failed', e); }
  } else if (!enabled && weatherOverlayLayer) {
    map.removeLayer(weatherOverlayLayer);
    weatherOverlayLayer = null;
  }
  syncToggleButtons();
}
function toggleWeatherOverlay() {
  state.settings.weather_overlay_enabled = !state.settings.weather_overlay_enabled;
  fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ weather_overlay_enabled: state.settings.weather_overlay_enabled }) }).catch(() => {});
  syncWeatherOverlay();
}

/* ---------- Traffic heatmap ----------------------------------------------- */
async function toggleHeatmap() {
  if (heatmapLayer) {
    map.removeLayer(heatmapLayer);
    heatmapLayer = null;
    syncToggleButtons();
    return;
  }
  if (typeof L.heatLayer !== 'function') { toast('Heatmap library not loaded'); return; }
  try {
    const data = await fetch(API.heatmap).then((r) => r.json());
    const points = (data.points || []).map((p) => [p.lat, p.lon, Math.min(1, p.hits / 50)]);
    if (!points.length) { toast('No heatmap data yet'); return; }
    heatmapLayer = L.heatLayer(points, { radius: 22, blur: 18, maxZoom: 8, max: 1.0,
      gradient: { 0.2: '#00ffaa', 0.5: '#ffd400', 0.8: '#ff7a00', 1.0: '#ff0040' } });
    heatmapLayer.addTo(map);
  } catch (e) { console.warn('heatmap fetch failed', e); }
  syncToggleButtons();
}

/* ---------- Day / night terminator ---------------------------------------- */
function toggleDayNight() {
  state.settings.day_night_enabled = !state.settings.day_night_enabled;
  if (state.settings.day_night_enabled) drawTerminator(); else removeTerminator();
  fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ day_night_enabled: state.settings.day_night_enabled }) }).catch(() => {});
  syncToggleButtons();
}
function removeTerminator() { if (terminatorLayer) { map.removeLayer(terminatorLayer); terminatorLayer = null; } }
function drawTerminator() {
  removeTerminator();
  if (!map) return;
  const now = new Date();
  const rad = Math.PI / 180, deg = 180 / Math.PI;
  const julian = now.getTime() / 86400000 + 2440587.5;
  const T = (julian - 2451545.0) / 36525;
  const L0 = (280.46646 + 36000.76983 * T) % 360;
  const M = (357.52911 + 35999.05029 * T) * rad;
  const C = (1.914602 - 0.004817 * T) * Math.sin(M)
          + (0.019993 - 0.000101 * T) * Math.sin(2 * M)
          + 0.000289 * Math.sin(3 * M);
  const trueLong = (L0 + C) * rad;
  const obliq = 23.4392911 * rad;
  const decl = Math.asin(Math.sin(obliq) * Math.sin(trueLong));
  const gmst = (18.697374558 + 24.06570982441908 * (julian - 2451545.0)) % 24;
  const subsolarLon = -gmst * 15;
  const points = [];
  for (let lon = -180; lon <= 180; lon += 2) {
    const H = ((lon - subsolarLon) * rad);
    const lat = Math.atan(-Math.cos(H) / Math.tan(decl)) * deg;
    points.push([lat, lon]);
  }
  const nightAtNorthPole = Math.sin(decl) < 0;
  if (nightAtNorthPole) points.push([90, 180], [90, -180]);
  else points.push([-90, 180], [-90, -180]);
  terminatorLayer = L.polygon(points, { className: 'terminator-shade', interactive: false }).addTo(map);
}

/* ---------- Notes (in detail panel) --------------------------------------- */
let noteSaveTimer = null;
async function renderNoteField(hex) {
  const detail = document.getElementById('detail-content');
  if (!detail || detail.querySelector(`.detail-note[data-hex="${hex}"]`)) return;
  let existing = '';
  try { const data = await fetch(API.notes(hex)).then((r) => r.json()); existing = data.note || ''; } catch (e) {}
  const section = document.createElement('section');
  section.className = 'detail-note';
  section.dataset.hex = hex;
  section.innerHTML = `
    <h3>Personal note</h3>
    <textarea id="note-area" placeholder="Spot something notable? Add a private note attached to this aircraft."></textarea>
    <span class="hint" id="note-status"></span>
  `;
  const ql = detail.querySelector('#quick-links')?.closest('section');
  if (ql) detail.insertBefore(section, ql); else detail.appendChild(section);
  const area = section.querySelector('#note-area');
  area.value = existing;
  area.addEventListener('input', () => {
    clearTimeout(noteSaveTimer);
    document.getElementById('note-status').textContent = 'Saving…';
    noteSaveTimer = setTimeout(async () => {
      try {
        await fetch(API.notes(hex), { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note: area.value }) });
        document.getElementById('note-status').textContent = 'Saved.';
      } catch (e) { document.getElementById('note-status').textContent = 'Save failed.'; }
    }, 500);
  });
}

/* ---------- Photo carousel ----------------------------------------------- */
function renderPhotoGallery(photos) {
  const root = document.getElementById('detail-photo');
  if (!root) return;
  detailPhotos = (photos || []).filter((p) => /^https:\/\//i.test(p.url || ''));
  detailPhotoIndex = 0;
  root.innerHTML = '';
  root.classList.add('detail-photos');
  if (!detailPhotos.length) {
    const span = document.createElement('span'); span.className = 'hint'; span.textContent = 'No photo on file';
    root.appendChild(span);
    return;
  }
  const img = document.createElement('img');
  img.id = 'detail-photo-img';
  img.src = detailPhotos[0].url;
  img.alt = 'aircraft photo';
  img.referrerPolicy = 'no-referrer';
  img.loading = 'lazy';
  root.appendChild(img);
  const credit = document.createElement('div');
  credit.className = 'credit';
  credit.id = 'detail-photo-credit';
  credit.textContent = `© ${detailPhotos[0].photographer || 'planespotters.net'}`;
  root.appendChild(credit);
  if (detailPhotos.length > 1) {
    const prev = document.createElement('button'); prev.className = 'photo-nav prev'; prev.textContent = '‹';
    const next = document.createElement('button'); next.className = 'photo-nav next'; next.textContent = '›';
    prev.onclick = (e) => { e.stopPropagation(); rotatePhoto(-1); };
    next.onclick = (e) => { e.stopPropagation(); rotatePhoto(1); };
    root.appendChild(prev); root.appendChild(next);
    const counter = document.createElement('div');
    counter.className = 'photo-counter';
    counter.id = 'detail-photo-counter';
    counter.textContent = `1 / ${detailPhotos.length}`;
    root.appendChild(counter);
  }
}
function rotatePhoto(delta) {
  if (!detailPhotos.length) return;
  detailPhotoIndex = (detailPhotoIndex + delta + detailPhotos.length) % detailPhotos.length;
  const img = document.getElementById('detail-photo-img');
  if (img) img.src = detailPhotos[detailPhotoIndex].url;
  const credit = document.getElementById('detail-photo-credit');
  if (credit) credit.textContent = `© ${detailPhotos[detailPhotoIndex].photographer || 'planespotters.net'}`;
  const counter = document.getElementById('detail-photo-counter');
  if (counter) counter.textContent = `${detailPhotoIndex + 1} / ${detailPhotos.length}`;
}

/* ---------- Saved views --------------------------------------------------- */
async function openViews() { document.getElementById('views-modal').hidden = false; await loadViews(); }
function closeViews() { document.getElementById('views-modal').hidden = true; }
async function loadViews() {
  try { const data = await fetch(API.views).then((r) => r.json()); savedViewsCache = data.views || []; }
  catch (e) { savedViewsCache = []; }
  const list = document.getElementById('views-list');
  list.innerHTML = '';
  if (!savedViewsCache.length) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="hint">No saved views yet.</span>';
    list.appendChild(li);
    return;
  }
  savedViewsCache.forEach((v) => {
    const li = document.createElement('li');
    li.innerHTML = `
      <span><div class="vname">${escapeHtml(v.name)}</div><div class="vmeta">${v.lat.toFixed(2)}, ${v.lon.toFixed(2)} · z${v.zoom}</div></span>
      <button class="secondary go">Go</button>
      <button class="secondary del">Delete</button>
    `;
    li.querySelector('.go').onclick = () => { map.setView([v.lat, v.lon], v.zoom); closeViews(); };
    li.querySelector('.del').onclick = async () => {
      savedViewsCache = savedViewsCache.filter((x) => x !== v);
      await fetch(API.views, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ views: savedViewsCache }) });
      loadViews();
    };
    list.appendChild(li);
  });
}
async function saveCurrentView() {
  const name = document.getElementById('new-view-name').value.trim();
  if (!name) return;
  const c = map.getCenter();
  savedViewsCache.push({ name, lat: c.lat, lon: c.lng, zoom: map.getZoom() });
  await fetch(API.views, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ views: savedViewsCache }) });
  document.getElementById('new-view-name').value = '';
  loadViews();
  toast(`Saved view "${name}"`);
}

/* ---------- Webhooks settings UI ----------------------------------------- */
async function loadWebhooksIntoSettings() {
  try { const data = await fetch(API.webhooks).then((r) => r.json()); webhooksCache = data.webhooks || []; }
  catch (e) { webhooksCache = []; }
  renderWebhookList();
}
function renderWebhookList() {
  const root = document.getElementById('webhook-list');
  if (!root) return;
  root.innerHTML = '';
  webhooksCache.forEach((hook, idx) => {
    const row = document.createElement('div');
    row.className = 'webhook-row';
    row.innerHTML = `
      <input class="wh-label" type="text" placeholder="Label (optional)" value="${escapeHtml(hook.label || '')}" />
      <select class="wh-kind">
        <option value="discord">Discord webhook</option>
        <option value="slack">Slack webhook</option>
        <option value="ntfy">ntfy.sh</option>
        <option value="generic">Generic JSON POST</option>
      </select>
      <input class="wh-url" type="url" placeholder="https://discord.com/api/webhooks/..." value="${escapeHtml(hook.url || '')}" style="grid-column: 1 / -1" />
      <div class="types-row">
        <label><input type="checkbox" class="wh-t-emergency"> Emergency</label>
        <label><input type="checkbox" class="wh-t-military"> Military</label>
        <label><input type="checkbox" class="wh-t-watchlist"> Watchlist</label>
        <label><input type="checkbox" class="wh-t-rare"> Rare type</label>
      </div>
      <div class="controls-row">
        <button class="secondary wh-test">Test</button>
        <button class="secondary wh-remove" style="margin-left:auto; border-color:var(--alert); color:var(--alert)">Remove</button>
      </div>
    `;
    row.querySelector('.wh-kind').value = hook.kind || 'discord';
    const types = hook.types || [];
    row.querySelector('.wh-t-emergency').checked = types.includes('emergency');
    row.querySelector('.wh-t-military').checked  = types.includes('military');
    row.querySelector('.wh-t-watchlist').checked = types.includes('watchlist');
    row.querySelector('.wh-t-rare').checked      = types.includes('rare');
    row.querySelector('.wh-remove').onclick = () => { webhooksCache.splice(idx, 1); renderWebhookList(); };
    row.querySelector('.wh-test').onclick = async () => {
      const url = row.querySelector('.wh-url').value.trim();
      const kind = row.querySelector('.wh-kind').value;
      if (!url) { toast('Enter a URL first'); return; }
      try {
        const res = await fetch(API.webhookTest, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, kind }) });
        toast(res.ok ? 'Test sent — check the destination.' : 'Test failed.');
      } catch (e) { toast('Test failed.'); }
    };
    root.appendChild(row);
  });
}
function collectWebhooks() {
  const out = [];
  document.querySelectorAll('#webhook-list .webhook-row').forEach((row) => {
    const url = row.querySelector('.wh-url').value.trim();
    if (!url) return;
    out.push({
      kind: row.querySelector('.wh-kind').value,
      url,
      label: row.querySelector('.wh-label').value.trim(),
      types: [
        row.querySelector('.wh-t-emergency').checked ? 'emergency' : null,
        row.querySelector('.wh-t-military').checked  ? 'military'  : null,
        row.querySelector('.wh-t-watchlist').checked ? 'watchlist' : null,
        row.querySelector('.wh-t-rare').checked      ? 'rare'      : null,
      ].filter(Boolean),
    });
  });
  return out;
}
async function saveWebhooks() {
  const list = collectWebhooks();
  await fetch(API.webhooks, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ webhooks: list }) });
  webhooksCache = list;
}
function addWebhook() {
  webhooksCache.push({ kind: 'discord', url: '', label: '',
    types: ['emergency', 'military', 'watchlist', 'rare'] });
  renderWebhookList();
}

/* ---------- Polar coverage chart ----------------------------------------- */
let polarBinsCache = [];
let polarMaxNmCache = 0;

async function renderPolarCoverage() {
  const canvas = document.getElementById('polar-canvas');
  if (!canvas) return;
  let bins = [];
  try { const data = await fetch(API.coverage).then((r) => r.json()); bins = data.bins || []; } catch (e) { return; }
  polarBinsCache = bins;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const maxNm = Math.max(50, ...bins.map((b) => b.max_nm || 0));
  const R = Math.min(W, H) / 2 - 30;
  const css = getComputedStyle(document.documentElement);
  const accent = (css.getPropertyValue('--accent').trim() || '#00ff40');
  const separator = (css.getPropertyValue('--separator').trim() || '#333');
  const text = (css.getPropertyValue('--secondary-text').trim() || '#aaa');
  ctx.strokeStyle = separator; ctx.lineWidth = 0.6; ctx.fillStyle = text; ctx.font = '10px system-ui';
  for (let i = 1; i <= 4; i++) {
    const r = (R * i) / 4;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.fillText(`${Math.round((maxNm * i) / 4)} nm`, cx + 4, cy - r);
  }
  for (const [label, d] of [['N', 0], ['E', 90], ['S', 180], ['W', 270]]) {
    const rd = (d - 90) * Math.PI / 180;
    ctx.fillText(label, cx + (R + 12) * Math.cos(rd) - 4, cy + (R + 12) * Math.sin(rd) + 4);
  }
  ctx.beginPath();
  bins.forEach((b, i) => {
    const r = (b.max_nm / maxNm) * R;
    const rd = (b.bearing - 90) * Math.PI / 180;
    const x = cx + r * Math.cos(rd), y = cy + r * Math.sin(rd);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.closePath();
  ctx.fillStyle = accent + '33'; ctx.fill();
  ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.stroke();
  polarMaxNmCache = maxNm;
  if (!canvas._polarHover) { canvas._polarHover = true; canvas.addEventListener('mousemove', polarMouseMove); canvas.addEventListener('mouseleave', polarMouseLeave); }
}

function polarMouseMove(e) {
  const canvas = e.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const dx = e.clientX - rect.left - cx;
  const dy = e.clientY - rect.top - cy;
  const ang = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360) % 360;
  const bearing = Math.round(ang);
  const bin = polarBinsCache.find((b) => b.bearing === bearing) || { max_nm: 0 };
  let tip = document.getElementById('polar-tooltip');
  if (!tip) {
    tip = document.createElement('div'); tip.id = 'polar-tooltip'; tip.className = 'context-menu';
    tip.style.pointerEvents = 'none'; tip.style.padding = '4px 10px';
    document.body.appendChild(tip);
  }
  const compass = ['N','NE','E','SE','S','SW','W','NW'][Math.round(bearing/45)%8];
  tip.innerHTML = `<div style="font-family:var(--font-mono); font-size:11px"><strong>${bearing}°</strong> ${compass}<br>${(bin.max_nm || 0).toFixed(1)} nm</div>`;
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
  tip.style.display = 'block';
}
function polarMouseLeave() { const tip = document.getElementById('polar-tooltip'); if (tip) tip.style.display = 'none'; }

async function renderLeaderboard() {
  let types = [];
  try { const data = await fetch(API.leaderboard).then((r) => r.json()); types = data.types || []; } catch (e) {}
  const tbody = document.getElementById('leaderboard-rows');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (!types.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="4" class="hint">No type history yet — let it run for a while.</td>';
    tbody.appendChild(tr);
    return;
  }
  for (const t of types) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(t.type_code)}</td>
      <td>${(t.sightings || 0).toLocaleString()}</td>
      <td>${t.first_seen ? new Date(t.first_seen * 1000).toLocaleDateString() : '—'}</td>
      <td>${t.last_seen ? new Date(t.last_seen * 1000).toLocaleString() : '—'}</td>
    `;
    tbody.appendChild(tr);
  }
}

function switchStatsTab(name) {
  for (const t of document.querySelectorAll('.tab[data-stats-tab]')) t.classList.toggle('active', t.dataset.statsTab === name);
  for (const s of document.querySelectorAll('.stats-section')) s.classList.toggle('active', s.dataset.statsSection === name);
  if (name === 'coverage') renderPolarCoverage();
  if (name === 'leaderboard') renderLeaderboard();
  if (name === 'records') renderRecords();
  if (name === 'bookmarks') renderBookmarksPanel();
}

/* ---------- Records panel ------------------------------------------------- */
async function renderRecords() {
  const tbody = document.getElementById('records-rows');
  if (!tbody) return;
  let rows = [];
  try { const data = await fetch(API.records).then((r) => r.json()); rows = data.records || []; } catch (e) {}
  tbody.innerHTML = '';
  for (const r of rows) {
    const tr = document.createElement('tr');
    if (r.value == null) {
      tr.innerHTML = `<td>${escapeHtml(r.label)}</td><td class="hint">—</td><td class="hint">—</td><td class="hint">—</td>`;
    } else {
      const ac = escapeHtml(r.callsign || r.registration || r.hex || '—');
      const type = r.type_code ? ` · ${escapeHtml(r.type_code)}` : '';
      const valueText = r.category === 'lowest_alt' || r.category === 'highest'
        ? `${(r.value).toLocaleString()} ft`
        : r.category === 'fastest'
          ? `${Math.round(r.value)} kts`
          : `${(r.value).toFixed(1)} nm`;
      tr.innerHTML = `
        <td>${escapeHtml(r.label)}</td>
        <td><strong>${valueText}</strong></td>
        <td>${ac}${type}</td>
        <td>${new Date(r.recorded_at * 1000).toLocaleString()}</td>
      `;
    }
    tbody.appendChild(tr);
  }
}

/* ---------- Bookmarks ----------------------------------------------------- */
async function refreshBookmarkCache() {
  try {
    const data = await fetch(API.bookmarks).then((r) => r.json());
    bookmarkedHexes = new Set((data.bookmarks || []).map((b) => b.hex));
  } catch (e) { bookmarkedHexes = new Set(); }
  updateBookmarkStar();
}

async function renderBookmarksPanel() {
  const list = document.getElementById('bookmarks-list');
  if (!list) return;
  let items = [];
  try { const data = await fetch(API.bookmarks).then((r) => r.json()); items = data.bookmarks || []; } catch (e) {}
  list.innerHTML = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="hint">No bookmarks yet. Click the ★ in the detail panel to add one.</span>';
    list.appendChild(li);
    return;
  }
  for (const b of items) {
    const li = document.createElement('li');
    const label = b.label || b.callsign || b.registration || b.hex.toUpperCase();
    const meta = [b.type_code, b.registration, b.hex.toUpperCase()].filter(Boolean).join(' · ');
    li.innerHTML = `
      <span><div class="vname">${escapeHtml(label)}</div><div class="vmeta">${escapeHtml(meta)}</div></span>
      <button class="secondary find">Find</button>
      <button class="secondary del">Remove</button>
    `;
    li.querySelector('.find').onclick = () => {
      // Try to find a live aircraft with this hex.
      const ac = state.aircraft.get(b.hex);
      if (ac && ac.lat != null) {
        selectAircraft(b.hex, { pan: true });
        document.getElementById('stats-modal').hidden = true;
      } else {
        toast(`${label} not currently in coverage`);
      }
    };
    li.querySelector('.del').onclick = async () => {
      await fetch(API.bookmark(b.hex), { method: 'DELETE' });
      bookmarkedHexes.delete(b.hex);
      renderBookmarksPanel();
      updateBookmarkStar();
    };
    list.appendChild(li);
  }
}

function updateBookmarkStar() {
  const btn = document.getElementById('bookmark-toggle');
  if (!btn || !state.selectedHex) return;
  const isBookmarked = bookmarkedHexes.has(state.selectedHex);
  btn.textContent = isBookmarked ? '★ Bookmarked' : '☆ Bookmark';
  btn.classList.toggle('active', isBookmarked);
}

async function toggleBookmarkSelected() {
  if (!state.selectedHex) return;
  const ac = state.aircraft.get(state.selectedHex);
  if (bookmarkedHexes.has(state.selectedHex)) {
    await fetch(API.bookmark(state.selectedHex), { method: 'DELETE' });
    bookmarkedHexes.delete(state.selectedHex);
    toast('Bookmark removed');
  } else {
    await fetch(API.bookmark(state.selectedHex), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        label: ac?.display_name || '',
        callsign: ac?.callsign || null,
        registration: ac?.registration || null,
        type_code: ac?.type_code || null,
      }),
    });
    bookmarkedHexes.add(state.selectedHex);
    toast('Bookmark added');
  }
  updateBookmarkStar();
}

/* ---------- DB import ----------------------------------------------------- */
async function handleDbImport(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  const status = document.getElementById('import-status');
  status.textContent = 'Uploading…'; status.className = 'hint';
  const fd = new FormData(); fd.append('file', f);
  try {
    const res = await fetch(API.importDb, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) { status.textContent = `Failed: ${data.detail || res.statusText}`; status.className = 'hint err'; return; }
    status.textContent = 'Restored! Reloading…'; status.className = 'hint ok';
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    status.textContent = `Error: ${err.message}`; status.className = 'hint err';
  } finally { e.target.value = ''; }
}

/* ---------- Toast --------------------------------------------------------- */
let toastTimer = null;
function toast(message) {
  let el = document.getElementById('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = message;
  el.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('visible'), 2200);
}

/* ---------- Context menu -------------------------------------------------- */
let contextMenuEl = null;
function showContextMenu(x, y, items) {
  hideContextMenu();
  const m = document.createElement('div');
  m.className = 'context-menu';
  m.style.left = `${x}px`; m.style.top = `${y}px`;
  for (const item of items) {
    const b = document.createElement('button');
    b.textContent = item.label;
    b.onclick = () => { hideContextMenu(); item.run(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  contextMenuEl = m;
  setTimeout(() => document.addEventListener('click', hideContextMenu, { once: true }), 0);
}
function hideContextMenu() { if (contextMenuEl) { contextMenuEl.remove(); contextMenuEl = null; } }

/* ---------- Keyboard shortcuts -------------------------------------------- */
function handleKeyboard(e) {
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (e.key === '/') { e.preventDefault(); document.getElementById('filter-search')?.focus(); return; }
  if (e.key === '?') { e.preventDefault(); showKeyboardHelp(); return; }
  if (e.key === 'f' || e.key === 'F') { toggleFollow(); return; }
  if (e.key === 'r' || e.key === 'R') { recenterOnReceiver(); return; }
  if (e.key === 'z' || e.key === 'Z') { fitToData(); return; }
  if (e.key === 'a' || e.key === 'A') { toggleAudio(); return; }
  if (e.key === 'e' || e.key === 'E') { openEvents(); return; }
  if (e.key === 's' || e.key === 'S') { openStats(); return; }
  if (e.key === 'v' || e.key === 'V') { openViews(); return; }
  if (e.key === 'p' || e.key === 'P') { toggleReplay(); return; }
  if (e.key === 'w' || e.key === 'W') { toggleWeatherOverlay(); return; }
  if (e.key === 'n' || e.key === 'N') { toggleDayNight(); return; }
  // Sidebar navigation: ↑/↓ cycle through visible aircraft rows; Enter pans to the
  // current selection. The arrow keys deliberately don't pan (selectAircraft({pan:false}))
  // so the user can scrub the list quickly without yanking the map around.
  if (e.key === 'ArrowDown') { e.preventDefault(); moveSidebarFocus(1); return; }
  if (e.key === 'ArrowUp')   { e.preventDefault(); moveSidebarFocus(-1); return; }
  if (e.key === 'Enter') {
    if (state.selectedHex && map) {
      const ac = state.aircraft.get(state.selectedHex);
      if (ac && ac.lat != null) map.panTo([ac.lat, ac.lon], { animate: true });
    }
    return;
  }
  if (e.key === 'Escape') { hideContextMenu(); closeSettings(); closeEvents(); closeStats(); closeViews(); closeKeyboardHelp(); }
}

// Move the sidebar selection by `delta` rows (1 = down, -1 = up). Reads order directly
// from the live DOM rather than re-sorting the aircraft list — guarantees the cursor
// matches what the user sees, regardless of which sort/filter combo is active.
function moveSidebarFocus(delta) {
  const rows = [...document.querySelectorAll('#aircraft-list .aircraft-row')];
  if (!rows.length) return;
  const currentIdx = rows.findIndex((r) => r.dataset.hex === state.selectedHex);
  let nextIdx;
  if (currentIdx === -1) {
    nextIdx = delta > 0 ? 0 : rows.length - 1;
  } else {
    nextIdx = (currentIdx + delta + rows.length) % rows.length;
  }
  const target = rows[nextIdx];
  selectAircraft(target.dataset.hex, { pan: false });
  target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function showKeyboardHelp() {
  let m = document.getElementById('kb-help-modal');
  if (m) { m.hidden = false; return; }
  m = document.createElement('div');
  m.id = 'kb-help-modal'; m.className = 'modal';
  m.innerHTML = `
    <div class="modal-shade" data-close-help></div>
    <div class="modal-panel" style="width:min(420px,96vw)">
      <header class="modal-header"><h2>Keyboard shortcuts</h2><button class="icon-button" data-close-help>✕</button></header>
      <div class="tab-body">
        <table class="help-table">
          <tr><td><kbd>/</kbd></td><td>Focus search</td></tr>
          <tr><td><kbd>↑</kbd> <kbd>↓</kbd></td><td>Cycle aircraft list</td></tr>
          <tr><td><kbd>Enter</kbd></td><td>Pan map to selected aircraft</td></tr>
          <tr><td><kbd>F</kbd></td><td>Toggle follow mode</td></tr>
          <tr><td><kbd>R</kbd></td><td>Recenter on receiver</td></tr>
          <tr><td><kbd>Z</kbd></td><td>Zoom to fit data</td></tr>
          <tr><td><kbd>A</kbd></td><td>Toggle audio alerts</td></tr>
          <tr><td><kbd>E</kbd></td><td>Open event log</td></tr>
          <tr><td><kbd>S</kbd></td><td>Open stats</td></tr>
          <tr><td><kbd>V</kbd></td><td>Saved views</td></tr>
          <tr><td><kbd>P</kbd></td><td>Replay (playback)</td></tr>
          <tr><td><kbd>W</kbd></td><td>Weather overlay</td></tr>
          <tr><td><kbd>N</kbd></td><td>Day/night terminator</td></tr>
          <tr><td><kbd>?</kbd></td><td>This help</td></tr>
          <tr><td><kbd>Esc</kbd></td><td>Close any modal</td></tr>
        </table>
      </div>
    </div>
  `;
  document.body.appendChild(m);
  m.querySelector('[data-close-help]').onclick = closeKeyboardHelp;
  m.querySelector('.modal-shade').onclick = closeKeyboardHelp;
}
function closeKeyboardHelp() { const m = document.getElementById('kb-help-modal'); if (m) m.hidden = true; }

// ---------- Audio + follow toggles ----------------------------------------
async function toggleAudio() {
  state.audioEnabled = !state.audioEnabled;
  state.settings.audio_alerts_enabled = state.audioEnabled;
  syncToggleButtons();
  if (state.audioEnabled) chime({ pitch: 660, duration: 0.12 });   // confirmation blip
  fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ audio_alerts_enabled: state.audioEnabled }) }).catch(() => {});
}

async function toggleFollow() {
  state.follow = !state.follow;
  state.settings.follow_selected = state.follow;
  syncToggleButtons();
  // If turning on with a selection, immediately re-centre.
  if (state.follow && state.selectedHex) {
    const ac = state.aircraft.get(state.selectedHex);
    if (ac && ac.lat != null) map.panTo([ac.lat, ac.lon], { animate: true });
  }
  fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ follow_selected: state.follow }) }).catch(() => {});
}

// ---------- Events panel --------------------------------------------------
let eventsCurrentTab = 'all';

async function openEvents() {
  state.eventsUnread = 0; updateEventsBadge();
  document.getElementById('events-modal').hidden = false;
  await loadEvents(eventsCurrentTab);
}
function closeEvents() { document.getElementById('events-modal').hidden = true; }

async function switchEventsTab(name) {
  eventsCurrentTab = name;
  for (const t of document.querySelectorAll('.tab[data-events-tab]')) {
    t.classList.toggle('active', t.dataset.eventsTab === name);
  }
  await loadEvents(name);
}

async function loadEvents(tab) {
  const kind = tab === 'all' ? '' : tab;
  let events = [];
  try {
    const res = await fetch(API.events(kind, 200));
    const data = await res.json();
    events = data.events || [];
  } catch (e) {}
  const list = document.getElementById('events-list');
  const empty = document.getElementById('events-empty');
  list.innerHTML = '';
  if (!events.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const ev of events) {
    const li = document.createElement('li');
    const t = new Date(ev.ts * 1000).toLocaleTimeString();
    const d = new Date(ev.ts * 1000).toLocaleDateString();
    li.innerHTML = `
      <span class="ev-time">${escapeHtml(d)}<br>${escapeHtml(t)}</span>
      <span class="ev-kind ${escapeHtml(ev.kind)}">${escapeHtml(ev.kind)}</span>
      <span class="ev-info">
        <span class="callsign">${escapeHtml(ev.callsign || ev.registration || ev.hex || '—')}</span>
        <span class="meta">${escapeHtml(ev.registration || ev.hex || '')}${ev.payload?.altitude ? ` · ${ev.payload.altitude} ft` : ''}${ev.payload?.squawk ? ` · sq ${ev.payload.squawk}` : ''}</span>
      </span>
      <span class="ev-distance">${ev.distance_nm != null ? ev.distance_nm.toFixed(0) + ' nm' : '—'}</span>
    `;
    list.appendChild(li);
  }
}

// ---------- Stats / health -------------------------------------------------
async function openStats() {
  document.getElementById('stats-modal').hidden = false;
  await refreshStatsPanel();
}
function closeStats() { document.getElementById('stats-modal').hidden = true; }

async function refreshStatsPanel() {
  const [healthRes, statsRes] = await Promise.all([
    fetch(API.health).then((r) => r.json()).catch(() => null),
    fetch(API.stats).then((r) => r.json()).catch(() => null),
  ]);
  // Health
  const hg = document.getElementById('health-grid');
  if (healthRes && hg) {
    const fmtSec = (s) => {
      if (s == null) return '—';
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
      return `${h}h ${m}m ${sec}s`;
    };
    const feedRows = Object.entries(healthRes.feeds || {})
      .map(([n, f]) => `${escapeHtml(n)}: ${f.ok ? 'OK' : 'FAIL'} (${f.rows ?? '—'} rows, ${f.duration_ms ?? '—'} ms)`)
      .join('<br>') || '—';
    const rows = [
      ['Uptime', fmtSec(healthRes.uptime_seconds)],
      ['Polls', healthRes.polls?.toLocaleString() ?? '—'],
      ['Errors', healthRes.errors ?? '—'],
      ['Last poll duration', `${healthRes.last_poll_duration_ms ?? '—'} ms`],
      ['Polls / minute', healthRes.polls_per_min ?? '—'],
      ['Connection state', escapeHtml(healthRes.connection_state || '—')],
      ['Receiver', healthRes.receiver ? `${healthRes.receiver.lat.toFixed(3)}, ${healthRes.receiver.lon.toFixed(3)}` : '—'],
      ['WS subscribers', healthRes.subscriber_count ?? '—'],
      ['Aircraft tracked', healthRes.aircraft_count ?? '—'],
      ['Trails kept', healthRes.trails_count ?? '—'],
      ['Today — unique', healthRes.daily?.unique_aircraft ?? '—'],
      ['Today — max range', `${healthRes.daily?.max_range_nm ?? '—'} nm`],
      ['Today — emergencies', healthRes.daily?.emergencies ?? '—'],
      ['Today — military', healthRes.daily?.military_seen ?? '—'],
      ['Feeds', feedRows],
    ];
    hg.innerHTML = rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join('');
  }
  // Daily history
  const tbody = document.getElementById('stats-rows');
  if (statsRes && tbody) {
    tbody.innerHTML = '';
    for (const d of (statsRes.days || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(d.date)}</td>
        <td>${(d.total_polls ?? 0).toLocaleString()}</td>
        <td>${(d.unique_aircraft ?? 0).toLocaleString()}</td>
        <td>${(d.max_range_nm ?? 0).toFixed(0)}</td>
        <td>${d.emergencies ?? 0}</td>
        <td>${d.military_seen ?? 0}</td>
      `;
      tbody.appendChild(tr);
    }
  }
}

// ---------- Replay --------------------------------------------------------
async function toggleReplay() {
  if (state.replayActive) { exitReplay(); return; }
  // Enter replay mode: pull the timeline, show the bar.
  try {
    const data = await fetch(API.replayTimeline).then((r) => r.json());
    state.replayTimestamps = data.timestamps || [];
  } catch (e) { state.replayTimestamps = []; }
  if (!state.replayTimestamps.length) {
    alert('Replay buffer is empty. Wait a minute or two for snapshots to accumulate, then try again.');
    return;
  }
  state.replayActive = true;
  document.getElementById('replay-bar').hidden = false;
  const slider = document.getElementById('replay-slider');
  slider.min = '0';
  slider.max = String(state.replayTimestamps.length - 1);
  slider.value = String(state.replayTimestamps.length - 1);
  syncToggleButtons();
  await onReplayScrub();
}

async function onReplayScrub() {
  if (!state.replayActive) return;
  const idx = parseInt(document.getElementById('replay-slider').value, 10) || 0;
  const ts = state.replayTimestamps[idx];
  if (ts == null) return;
  try {
    const snap = await fetch(API.replayAt(ts)).then((r) => r.json());
    // Render snapshot without modifying state.replayLiveSnapshot.
    renderReplayFrame(snap);
    document.getElementById('replay-clock').textContent = new Date(ts * 1000).toLocaleString();
  } catch (e) {
    document.getElementById('replay-clock').textContent = 'Snapshot missing';
  }
}

function renderReplayFrame(data) {
  state.aircraft.clear();
  for (const ac of data.aircraft || []) state.aircraft.set(ac.hex, ac);
  state.trails.clear();  // replay snapshots strip trails to save space
  updateConnectionPill();
  updateAircraftCount();
  renderSidebar();
  refreshMapMarkers();
  refreshTrails();
  refreshRangeRings();
  refreshReceiverMarker();
  updateDetailLiveSection();
}

function exitReplay() {
  state.replayActive = false;
  document.getElementById('replay-bar').hidden = true;
  syncToggleButtons();
  if (state.replayLiveSnapshot) applyAircraftUpdate(state.replayLiveSnapshot);
}

function bindUI() {
  document.getElementById('open-settings').addEventListener('click', openSettings);
  document.getElementById('fit-data').addEventListener('click', fitToData);
  document.getElementById('recenter').addEventListener('click', recenterOnReceiver);
  document.getElementById('toggle-aero').addEventListener('click', toggleAeroOverlay);
  document.getElementById('save-openaip-key').addEventListener('click', saveOpenaipKey);
  document.getElementById('pick-on-map')?.addEventListener('click', enterPickMode);
  document.getElementById('toggle-audio').addEventListener('click', toggleAudio);
  document.getElementById('toggle-follow').addEventListener('click', toggleFollow);
  document.getElementById('toggle-weather').addEventListener('click', toggleWeatherOverlay);
  document.getElementById('toggle-heatmap').addEventListener('click', toggleHeatmap);
  document.getElementById('toggle-day-night').addEventListener('click', toggleDayNight);
  document.getElementById('open-events').addEventListener('click', openEvents);
  document.getElementById('open-stats').addEventListener('click', openStats);
  document.getElementById('open-views').addEventListener('click', openViews);
  document.getElementById('save-view').addEventListener('click', saveCurrentView);
  document.getElementById('add-webhook').addEventListener('click', addWebhook);
  document.getElementById('toggle-replay').addEventListener('click', toggleReplay);
  document.getElementById('replay-live').addEventListener('click', exitReplay);
  document.getElementById('replay-slider').addEventListener('input', onReplayScrub);
  for (const el of document.querySelectorAll('[data-close-views]')) el.addEventListener('click', closeViews);
  for (const t of document.querySelectorAll('.tab[data-stats-tab]')) t.addEventListener('click', () => switchStatsTab(t.dataset.statsTab));
  document.getElementById('import-db').addEventListener('change', handleDbImport);
  document.addEventListener('keydown', handleKeyboard);
  // Bookmark right-click on aircraft markers
  refreshBookmarkCache();
  for (const el of document.querySelectorAll('[data-close-events]')) el.addEventListener('click', closeEvents);
  for (const el of document.querySelectorAll('[data-close-stats]')) el.addEventListener('click', closeStats);
  for (const el of document.querySelectorAll('.tab[data-events-tab]')) {
    el.addEventListener('click', () => switchEventsTab(el.dataset.eventsTab));
  }
  for (const el of document.querySelectorAll('[data-close]')) el.addEventListener('click', closeSettings);
  document.getElementById('save-settings').addEventListener('click', saveSettings);
  document.getElementById('save-fa-key').addEventListener('click', saveFaKey);
  document.getElementById('test-conn').addEventListener('click', testConnection);
  document.getElementById('enable-notifications').addEventListener('click', async () => {
    const r = await Notification.requestPermission();
    document.getElementById('notif-status').textContent = `Permission: ${r}`;
  });

  // Theme picker (topbar)
  document.getElementById('theme-select').addEventListener('change', (e) => {
    state.settings.theme = e.target.value;
    document.documentElement.dataset.theme = e.target.value;
    refreshThemeColorCache();
    radar?.setVariant(RADAR_VARIANTS[e.target.value] || null);
    if (map) applyTileLayerForTheme();
    // Refresh markers immediately — switching out of a radar theme must drop snapshot positions and
    // restore live ones without waiting for the next WS tick. Trails/rings re-tint to the new accent.
    refreshMapMarkers();
    refreshTrails();
    refreshRangeRings();
    if (typeof scheduleUrlStateWrite === 'function') scheduleUrlStateWrite();
    fetch(API.settings, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ theme: e.target.value }) });
  });

  // Tabs
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.tab-section').forEach((s) => s.classList.remove('active'));
      tab.classList.add('active');
      document.querySelector(`.tab-section[data-section="${tab.dataset.tab}"]`).classList.add('active');
    });
  });

  // Filters
  const search = document.getElementById('filter-search');
  search.addEventListener('input', () => { state.filters.text = search.value; renderSidebar(); });
  document.getElementById('filter-category').addEventListener('change', (e) => { state.filters.category = e.target.value; renderSidebar(); });
  document.getElementById('filter-sort').addEventListener('change', (e) => { state.filters.sort = e.target.value; renderSidebar(); });
  document.getElementById('show-ground').addEventListener('change', (e) => { state.filters.showGround = e.target.checked; renderSidebar(); });
  const altMin = document.getElementById('alt-min');
  const altMax = document.getElementById('alt-max');
  const distMax = document.getElementById('dist-max');
  const altMinVal = document.getElementById('alt-min-val');
  const altMaxVal = document.getElementById('alt-max-val');
  const distMaxVal = document.getElementById('dist-max-val');
  const altFill = document.getElementById('alt-fill');
  function syncSliders() {
    let lo = parseInt(altMin.value, 10);
    let hi = parseInt(altMax.value, 10);
    if (lo > hi) [lo, hi] = [hi, lo];
    state.filters.altMin = lo;
    state.filters.altMax = hi;
    state.filters.distMax = parseInt(distMax.value, 10);
    altMinVal.textContent = lo.toLocaleString();
    altMaxVal.textContent = hi >= 60000 ? '60k' : hi.toLocaleString();
    distMaxVal.textContent = state.filters.distMax >= 500 ? '∞' : state.filters.distMax;
    if (altFill) {
      const min = parseInt(altMin.min, 10);
      const max = parseInt(altMin.max, 10);
      const span = max - min;
      altFill.style.left = `${((lo - min) / span) * 100}%`;
      altFill.style.right = `${100 - ((hi - min) / span) * 100}%`;
    }
    renderSidebar();
  }
  [altMin, altMax, distMax].forEach((el) => el.addEventListener('input', syncSliders));
  syncSliders();
}

// ---------- bootstrap ----------

// Version toast: first load after a bump shows a single "✨ Updated" toast. We compare
// the server's reported version against the one we last shown to this browser in localStorage.
async function checkVersionBump() {
  try {
    const data = await fetch('/piscope/api/version').then((r) => r.json());
    const seen = localStorage.getItem('piscope_seen_version') || '';
    if (data.version && data.version !== seen) {
      // Skip the toast on the very first install (no previous version stored).
      if (seen) {
        toast(`✨ PiScope Radar updated to v${data.version}`);
      }
      localStorage.setItem('piscope_seen_version', data.version);
    }
  } catch (e) { /* non-fatal */ }
}

// Drag-and-drop handler — listens at the document level so you can drop the backup zip
// anywhere on the page, not just on a tiny target. Shows a full-screen overlay during drag.
function installDropHandler() {
  let depth = 0;
  let overlay = null;
  const showOverlay = () => {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'dropzone-overlay';
    overlay.innerHTML = '<span>Drop a piscope backup .zip to restore</span>';
    document.body.appendChild(overlay);
  };
  const hideOverlay = () => { if (overlay) { overlay.remove(); overlay = null; } };
  document.addEventListener('dragenter', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    depth++;
    showOverlay();
  });
  document.addEventListener('dragleave', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) hideOverlay();
  });
  document.addEventListener('dragover', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
  });
  document.addEventListener('drop', async (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    e.preventDefault();
    depth = 0;
    hideOverlay();
    const f = [...(e.dataTransfer?.files || [])][0];
    if (!f) return;
    if (!f.name.toLowerCase().endsWith('.zip')) { toast('Drop a piscope backup .zip file.'); return; }
    toast(`Restoring ${f.name}…`);
    const fd = new FormData(); fd.append('file', f);
    try {
      const res = await fetch(API.importDb, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) { toast(`Restore failed: ${data.detail || res.statusText}`); return; }
      toast('Restored! Reloading…');
      setTimeout(() => location.reload(), 800);
    } catch (err) { toast(`Restore failed: ${err.message}`); }
  });
}

// ---------- Shareable state in URL hash ----------
// Mirror the user-visible view into `location.hash` so a copied URL reproduces
// the same map centre/zoom/theme/selection on someone else's machine. We use the hash
// (not the query string) because hash changes don't trigger a server round-trip, and
// we use `history.replaceState` so map panning doesn't pollute the back/forward stack.

function readUrlState() {
  try {
    const hash = location.hash.replace(/^#/, '');
    if (!hash) return null;
    const params = new URLSearchParams(hash);
    const out = {};
    const c = params.get('center');
    if (c) {
      const [lat, lon] = c.split(',').map(Number);
      // Same validation as setReceiverLocation — silently ignore obviously bad coords.
      if (Number.isFinite(lat) && Number.isFinite(lon) &&
          lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
        out.center = { lat, lon };
      }
    }
    const z = parseInt(params.get('zoom') || '', 10);
    if (Number.isFinite(z) && z >= 2 && z <= 18) out.zoom = z;
    const t = params.get('theme');
    if (t) out.theme = t;
    const sel = params.get('select');
    if (sel) out.select = sel.toLowerCase();
    return out;
  } catch (e) { return null; }
}

// Apply URL state on first load — runs after setupMap so `map` is ready. Theme is applied
// before the map move so the basemap doesn't flash to the default first. Selection is
// stashed in `state._pendingSelect` and consumed by applyAircraftUpdate once that hex appears
// (since on first load the aircraft list is still empty).
function applyUrlState() {
  const u = readUrlState();
  if (!u) return;
  if (u.theme) {
    const sel = document.getElementById('theme-select');
    if (sel && [...sel.options].some((o) => o.value === u.theme)) {
      sel.value = u.theme;
      // Reuse the existing change-handler in bindUI — keeps tile + radar + persist logic in one place.
      sel.dispatchEvent(new Event('change'));
    }
  }
  if (u.center && map) {
    map.setView([u.center.lat, u.center.lon], u.zoom ?? map.getZoom());
    // If the URL told us where to look, skip the auto fit-to-data that would otherwise
    // snap the viewport away from the shared view on first WS message.
    state.didInitialFit = true;
  } else if (u.zoom && map) {
    map.setZoom(u.zoom);
  }
  if (u.select) state._pendingSelect = u.select;
}

// Coalesce frequent writes (a pan-and-zoom can fire dozens of moveend events) into one
// replaceState call so we don't spam the history API or the URL bar.
let _urlStateWriteTimer = null;
function scheduleUrlStateWrite() {
  clearTimeout(_urlStateWriteTimer);
  _urlStateWriteTimer = setTimeout(writeUrlState, 350);
}

function writeUrlState() {
  if (!map) return;
  const center = map.getCenter();
  const zoom = map.getZoom();
  const params = new URLSearchParams();
  params.set('center', `${center.lat.toFixed(4)},${center.lng.toFixed(4)}`);
  params.set('zoom', String(zoom));
  if (state.settings?.theme) params.set('theme', state.settings.theme);
  if (state.selectedHex) params.set('select', state.selectedHex);
  const newHash = `#${params.toString()}`;
  // Avoid no-op writes — replaceState still fires hashchange, and we don't want loops.
  if (location.hash !== newHash) {
    history.replaceState(null, '', `${location.pathname}${location.search}${newHash}`);
  }
}

async function init() {
  bindUI();
  await loadSettings();
  setupMap();
  applySettingsToUI();   // re-apply after radar is created
  // Re-hydrate from URL before the first WS frame — theme + map view + selection.
  applyUrlState();
  // Persist map view + selection into the URL as the user pans/zooms/selects.
  map.on('moveend', scheduleUrlStateWrite);
  map.on('zoomend', scheduleUrlStateWrite);
  const client = new PiScopeClient();
  client.connect();
  // Best-effort initial REST fetch in case WS hasn't connected yet
  try {
    const res = await fetch('/piscope/api/aircraft');
    if (res.ok) applyAircraftUpdate(await res.json());
  } catch (e) {}
  // PWA — register the service worker. We use the `/piscope/sw.js` route (not the static
  // file) because that response sets `Service-Worker-Allowed: /piscope`, which is required
  // for the SW to control the whole /piscope tree from a file that lives inside /piscope/static.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/piscope/sw.js', { scope: '/piscope' }).catch(() => {});
  }
  // Polish add-ons
  installDropHandler();
  checkVersionBump();
}

document.addEventListener('DOMContentLoaded', init);
