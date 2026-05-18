/* Radar sweep Canvas overlay.
 *
 * Anchored to the receiver position in map coordinates. On every animation
 * frame we re-derive the receiver pixel and pixels-per-nm from the current
 * Leaflet viewport so the sweep stays glued to the antenna as the map pans.
 *
 * Two variants:
 *   - 'classic'  : phosphor green, 5s rotation, vignette + pulsing centre blip
 *   - 'modern'   : cyan, 3s rotation, crosshair + tick marks + coverage ring
 *
 * When the active theme has a radar variant, aircraft on the map are switched
 * into snapshot mode: positions only update as the sweep passes over them.
 */

class RadarSweep {
  constructor(canvas, ctx) {
    this.canvas = canvas;
    this.ctx = ctx;
    this.variant = null;       // 'classic' | 'modern' | null
    this.map = null;
    this.receiver = null;      // {lat, lon}
    this.antennaRangeNM = 200;
    this.aircraft = [];        // [{hex, lat, lon, heading, altBand}]
    this.snapshots = new Map();// hex → {lat, lon, heading, altBand, ts}
    this.prevAngle = -Math.PI / 2;
    this.lastFrame = 0;
    this.onSnapshotChange = null;
    this.running = false;
  }

  setVariant(variant) {
    const prev = this.variant;
    this.variant = variant;
    if (variant !== prev) {
      // Cold start: clear snapshots so we don't show stale paints from another sweep.
      this.snapshots.clear();
      this.prevAngle = -Math.PI / 2;
      if (this.onSnapshotChange) this.onSnapshotChange();
    }
    if (variant && !this.running) this._start();
    if (!variant && this.running) this._stop();
  }

  setMap(map) { this.map = map; }
  setReceiver(receiver) { this.receiver = receiver; }
  setAntennaRange(nm) { this.antennaRangeNM = Math.max(10, nm || 200); }
  setAircraft(list) { this.aircraft = list; }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.floor(rect.width * dpr);
    this.canvas.height = Math.floor(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _start() {
    this.running = true;
    requestAnimationFrame((t) => this._tick(t));
  }

  _stop() {
    this.running = false;
    const { ctx, canvas } = this;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  _tick(timestamp) {
    if (!this.running) return;
    requestAnimationFrame((t) => this._tick(t));

    // CPU budget: cap repaints at ~30 fps. The radar sweep only rotates over 3-5 s, so
    // a 33 ms frame interval still looks smooth and halves canvas work on high-refresh
    // displays (60/120 Hz) where the default RAF cadence is wasteful.
    if (timestamp - this.lastFrame < 33) return;
    this.lastFrame = timestamp;

    if (!this.map || !this.receiver) return;
    const ctx = this.ctx;
    const W = this.canvas.clientWidth;
    const H = this.canvas.clientHeight;

    ctx.clearRect(0, 0, W, H);

    // Receiver in pixel space
    const recPoint = this.map.latLngToContainerPoint([this.receiver.lat, this.receiver.lon]);
    const cx = recPoint.x;
    const cy = recPoint.y;

    // Pixels per nm at current zoom (use 1 arcminute of latitude as 1nm baseline).
    const center = this.map.getCenter();
    const p1 = this.map.latLngToContainerPoint(center);
    const p2 = this.map.latLngToContainerPoint([center.lat + 1 / 60, center.lng]);
    const pixelsPerNM = Math.abs(p2.y - p1.y);

    const radius = this.antennaRangeNM * pixelsPerNM;
    if (radius < 4) return;

    // Sweep angle. 0 = up (north), clockwise.
    const period = this.variant === 'modern' ? 3000 : 5000;
    const angle = ((timestamp % period) / period) * Math.PI * 2 - Math.PI / 2;

    // Trail width: 25% for modern (sharp ATC), 45% for classic (phosphor glow).
    const trailFraction = this.variant === 'modern' ? 0.25 : 0.45;
    const trailArc = Math.PI * 2 * trailFraction;

    // Pull accent colour from the theme.
    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue('--accent').trim() || '#00ff40';
    const accentMuted = styles.getPropertyValue('--accent-muted').trim() || accent;

    this._drawSweep(ctx, cx, cy, radius, angle, trailArc, accent);
    this._drawLeadingEdge(ctx, cx, cy, radius, angle, accent);

    if (this.variant === 'modern') {
      this._drawTicks(ctx, cx, cy, radius, accentMuted);
      this._drawCoverageRing(ctx, cx, cy, radius, accent);
      this._drawCrosshair(ctx, cx, cy, accent);
    } else {
      this._drawCentreBlip(ctx, cx, cy, timestamp, accent);
      this._drawVignette(ctx, W, H);
    }

    // Update snapshots — find aircraft whose bearing falls in (prevAngle, angle).
    this._updateSnapshots(this.prevAngle, angle, timestamp);
    this.prevAngle = angle;
  }

  _drawSweep(ctx, cx, cy, radius, angle, trailArc, color) {
    const segments = 48;
    const start = angle - trailArc;
    for (let i = 0; i < segments; i++) {
      const t1 = start + (i / segments) * trailArc;
      const t2 = start + ((i + 1) / segments) * trailArc;
      const opacity = Math.pow(i / segments, 1.8) * 0.55;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, t1, t2);
      ctx.closePath();
      ctx.fillStyle = colorWithAlpha(color, opacity);
      ctx.fill();
    }
  }

  _drawLeadingEdge(ctx, cx, cy, radius, angle, color) {
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + radius * Math.cos(angle), cy + radius * Math.sin(angle));
    ctx.strokeStyle = color;
    ctx.lineWidth = this.variant === 'modern' ? 2 : 3;
    ctx.shadowBlur = this.variant === 'classic' ? 12 : 6;
    ctx.shadowColor = color;
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  _drawTicks(ctx, cx, cy, radius, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
      const x1 = cx + (radius - 8) * Math.cos(a);
      const y1 = cy + (radius - 8) * Math.sin(a);
      const x2 = cx + radius * Math.cos(a);
      const y2 = cy + radius * Math.sin(a);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
  }

  _drawCoverageRing(ctx, cx, cy, radius, color) {
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = colorWithAlpha(color, 0.55);
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  _drawCrosshair(ctx, cx, cy, color) {
    const len = 6;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx - len, cy); ctx.lineTo(cx + len, cy);
    ctx.moveTo(cx, cy - len); ctx.lineTo(cx, cy + len);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, Math.PI * 2);
    ctx.stroke();
  }

  _drawCentreBlip(ctx, cx, cy, ts, color) {
    const pulse = (Math.sin(ts / 600) + 1) / 2;     // 0..1
    const r = 3 + pulse * 4;
    ctx.fillStyle = colorWithAlpha(color, 0.4 + pulse * 0.5);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
  }

  _drawVignette(ctx, W, H) {
    const grad = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.2, W / 2, H / 2, Math.max(W, H) * 0.7);
    grad.addColorStop(0, 'rgba(0,0,0,0)');
    grad.addColorStop(1, 'rgba(0,0,0,0.55)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);
  }

  _updateSnapshots(prevAngle, currentAngle, ts) {
    if (!this.receiver) return;
    let changed = false;
    for (const ac of this.aircraft) {
      if (ac.lat == null || ac.lon == null) continue;
      const bearing = this._bearingFromReceiver(ac);
      if (bearing == null) continue;
      // Convert bearing (degrees from north, clockwise) into the same angle space as our sweep
      const ang = ((bearing - 90) * Math.PI) / 180;   // 0deg = up, clockwise
      const angNorm = normaliseAngle(ang);
      const prevNorm = normaliseAngle(prevAngle);
      const currentNorm = normaliseAngle(currentAngle);
      if (this._angleInSweep(prevNorm, currentNorm, angNorm)) {
        this.snapshots.set(ac.hex, { lat: ac.lat, lon: ac.lon, heading: ac.heading, altBand: ac.altBand, ts });
        changed = true;
      }
    }
    // Decay snapshots that have fallen too far behind.
    const period = this.variant === 'modern' ? 3000 : 5000;
    for (const [hex, snap] of this.snapshots) {
      if (ts - snap.ts > period * 1.4) {
        this.snapshots.delete(hex);
        changed = true;
      }
    }
    if (changed && this.onSnapshotChange) this.onSnapshotChange();
  }

  _angleInSweep(a, b, target) {
    if (a <= b) return target >= a && target <= b;
    return target >= a || target <= b;
  }

  _bearingFromReceiver(ac) {
    const lat1 = (this.receiver.lat * Math.PI) / 180;
    const lat2 = (ac.lat * Math.PI) / 180;
    const dLon = ((ac.lon - this.receiver.lon) * Math.PI) / 180;
    const y = Math.sin(dLon) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
    let brng = (Math.atan2(y, x) * 180) / Math.PI;
    return (brng + 360) % 360;
  }
}

function normaliseAngle(a) {
  while (a < 0) a += Math.PI * 2;
  while (a >= Math.PI * 2) a -= Math.PI * 2;
  return a;
}

function colorWithAlpha(color, alpha) {
  // Accept rgb(), rgba() or #hex.
  if (color.startsWith('rgb')) {
    const m = color.match(/rgba?\(([^)]+)\)/);
    if (!m) return color;
    const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
    const [r, g, b] = parts;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  if (color.startsWith('#')) {
    const hex = color.replace('#', '');
    const r = parseInt(hex.length === 3 ? hex[0] + hex[0] : hex.slice(0, 2), 16);
    const g = parseInt(hex.length === 3 ? hex[1] + hex[1] : hex.slice(2, 4), 16);
    const b = parseInt(hex.length === 3 ? hex[2] + hex[2] : hex.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return color;
}

window.RadarSweep = RadarSweep;
