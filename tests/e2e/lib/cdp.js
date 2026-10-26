'use strict';
/*
 * Zero-dependency Chrome DevTools Protocol driver for PiScope's browser E2E suite.
 *
 * Why hand-rolled: the dev host (claude-dev) has chromium (snap) + node, but NO
 * Playwright/Puppeteer and pip can't reach pypi, so we speak CDP directly over a
 * minimal RFC6455 WebSocket client (raw `net` socket; node <=21 has no global
 * WebSocket). This module exposes a small Browser/Page API the test modules use.
 *
 * Public API:
 *   const browser = await Browser.launch({ headless, port, profile });
 *   const page = browser.page;
 *   await page.navigate(url);
 *   await page.waitFor("expr returning truthy", { timeout });
 *   const v = await page.evaluate("js expression");          // returnByValue
 *   await page.click("#sel");  await page.press("Escape");
 *   await page.setViewport({ width, height, mobile });
 *   page.consoleErrors / page.exceptions / page.logErrors    // collected arrays
 *   await browser.close();
 */
const net = require('net');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ----------------------------- minimal WS client -----------------------------
class WSClient {
  constructor(url) {
    const m = url.match(/^ws:\/\/([^:/]+):(\d+)(\/.*)$/);
    if (!m) throw new Error('bad ws url: ' + url);
    this.host = m[1]; this.port = +m[2]; this.path = m[3];
    this.buf = Buffer.alloc(0); this.frag = Buffer.alloc(0); this.onmessage = null;
  }
  connect() {
    return new Promise((resolve, reject) => {
      const key = crypto.randomBytes(16).toString('base64');
      let handshook = false;
      this.sock = net.connect(this.port, this.host, () => {
        this.sock.write(`GET ${this.path} HTTP/1.1\r\nHost: ${this.host}:${this.port}\r\n` +
          `Upgrade: websocket\r\nConnection: Upgrade\r\n` +
          `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
      });
      this.sock.on('data', (chunk) => {
        this.buf = Buffer.concat([this.buf, chunk]);
        if (!handshook) {
          const idx = this.buf.indexOf('\r\n\r\n'); if (idx < 0) return;
          if (!/ 101 /.test(this.buf.slice(0, idx).toString())) { reject(new Error('WS handshake failed')); return; }
          handshook = true; this.buf = this.buf.slice(idx + 4); resolve();
        }
        this._drain();
      });
      this.sock.on('error', reject);
    });
  }
  _drain() {
    for (;;) {
      if (this.buf.length < 2) return;
      const b0 = this.buf[0], b1 = this.buf[1];
      const fin = b0 & 0x80, opcode = b0 & 0x0f, masked = b1 & 0x80;
      let len = b1 & 0x7f, off = 2;
      if (len === 126) { if (this.buf.length < 4) return; len = this.buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (this.buf.length < 10) return; len = Number(this.buf.readBigUInt64BE(2)); off = 10; }
      let mask; if (masked) { if (this.buf.length < off + 4) return; mask = this.buf.slice(off, off + 4); off += 4; }
      if (this.buf.length < off + len) return;
      let payload = this.buf.slice(off, off + len);
      if (masked) { const o = Buffer.alloc(len); for (let i = 0; i < len; i++) o[i] = payload[i] ^ mask[i % 4]; payload = o; }
      this.buf = this.buf.slice(off + len);
      if (opcode === 0x8) { try { this.sock.end(); } catch (e) {} return; }
      if (opcode === 0x9) { this._send(0xA, payload); continue; }
      if (opcode === 0xA) continue;
      this.frag = Buffer.concat([this.frag, payload]);
      if (fin) { const m = this.frag.toString('utf8'); this.frag = Buffer.alloc(0); if (this.onmessage) this.onmessage(m); }
    }
  }
  _send(opcode, payload) {
    const len = payload.length; let h;
    if (len < 126) h = Buffer.from([0x80 | opcode, 0x80 | len]);
    else if (len < 65536) { h = Buffer.alloc(4); h[0] = 0x80 | opcode; h[1] = 0x80 | 126; h.writeUInt16BE(len, 2); }
    else { h = Buffer.alloc(10); h[0] = 0x80 | opcode; h[1] = 0x80 | 127; h.writeBigUInt64BE(BigInt(len), 2); }
    const mask = crypto.randomBytes(4), out = Buffer.alloc(len);
    for (let i = 0; i < len; i++) out[i] = payload[i] ^ mask[i % 4];
    this.sock.write(Buffer.concat([h, mask, out]));
  }
  sendText(s) { this._send(0x1, Buffer.from(s, 'utf8')); }
}

// ----------------------------- HTTP discovery --------------------------------
function httpGet(port, path) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path }, (res) => {
      let b = ''; res.on('data', (c) => b += c); res.on('end', () => resolve(b));
    });
    req.on('error', reject);
    req.setTimeout(2000, () => req.destroy(new Error('timeout')));
  });
}

// ----------------------------- Page ------------------------------------------
class Page {
  constructor(ws) {
    this.ws = ws; this._id = 0; this._pending = new Map();
    this.consoleErrors = []; this.exceptions = []; this.logErrors = [];
    ws.onmessage = (m) => this._onMsg(m);
  }
  _onMsg(msg) {
    let o; try { o = JSON.parse(msg); } catch (e) { return; }
    if (o.id && this._pending.has(o.id)) {
      const p = this._pending.get(o.id); this._pending.delete(o.id);
      return o.error ? p.reject(new Error(JSON.stringify(o.error))) : p.resolve(o.result);
    }
    if (o.method === 'Runtime.consoleAPICalled') {
      if (o.params.type === 'error' || o.params.type === 'assert')
        this.consoleErrors.push((o.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' '));
    } else if (o.method === 'Runtime.exceptionThrown') {
      const d = o.params.exceptionDetails;
      this.exceptions.push(d.exception?.description || d.text || JSON.stringify(d));
    } else if (o.method === 'Log.entryAdded' && o.params.entry.level === 'error') {
      this.logErrors.push(`[${o.params.entry.source}] ${o.params.entry.text}`);
    }
  }
  cdp(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++this._id; this._pending.set(id, { resolve, reject });
      this.ws.sendText(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => { if (this._pending.has(id)) { this._pending.delete(id); reject(new Error('CDP timeout: ' + method)); } }, 15000);
    });
  }
  async enable() { await this.cdp('Runtime.enable'); await this.cdp('Page.enable'); await this.cdp('Log.enable'); }
  async navigate(url) { await this.cdp('Page.navigate', { url }); }
  async evaluate(expression) {
    const r = await this.cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('eval threw: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  /** Poll an expression until it returns truthy (or timeout). Returns the value or throws. */
  async waitFor(expression, { timeout = 15000, poll = 250 } = {}) {
    const deadline = Date.now() + timeout;
    for (;;) {
      try { const v = await this.evaluate(expression); if (v) return v; } catch (e) {}
      if (Date.now() > deadline) throw new Error('waitFor timeout: ' + expression);
      await sleep(poll);
    }
  }
  async setViewport({ width, height, mobile = false, dsf = 1 }) {
    await this.cdp('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: dsf, mobile });
  }
  /** Click via DOM (works regardless of element visibility/overlap). Returns true if found. */
  click(sel) { return this.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});if(!e)return false;e.click();return true;})()`); }
  /** Dispatch a keydown on document (the app binds its shortcuts there). */
  press(key) { return this.evaluate(`(()=>{document.dispatchEvent(new KeyboardEvent('keydown',{key:${JSON.stringify(key)},bubbles:true}));return true;})()`); }
  rect(sel) { return this.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});if(!e)return null;const r=e.getBoundingClientRect();return{x:Math.round(r.x),y:Math.round(r.y),left:Math.round(r.left),right:Math.round(r.right),w:Math.round(r.width),h:Math.round(r.height),vw:window.innerWidth,vh:window.innerHeight};})()`); }
  visible(sel) { return this.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});return !!(e&&e.offsetParent!==null);})()`); }
  /** Modal-aware "is shown": element exists and its [hidden] attribute is not set.
   *  Use for position:fixed modals where offsetParent is null even when visible. */
  open(sel) { return this.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});return !!e && e.hidden===false;})()`); }
  count(sel) { return this.evaluate(`document.querySelectorAll(${JSON.stringify(sel)}).length`); }
  text(sel) { return this.evaluate(`(document.querySelector(${JSON.stringify(sel)})||{}).textContent||''`); }
  clearLogs() { this.consoleErrors.length = 0; this.exceptions.length = 0; this.logErrors.length = 0; }
}

// ----------------------------- Browser ---------------------------------------
class Browser {
  static async launch({ port = 9222, profile = process.env.HOME + '/.cache/piscope-e2e/profile', headless = true } = {}) {
    const args = [
      headless ? '--headless=new' : '--start-maximized',
      '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
      '--remote-debugging-port=' + port, '--user-data-dir=' + profile, 'about:blank',
    ];
    const proc = spawn('chromium', args, { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = ''; proc.stderr.on('data', (d) => { stderr += d.toString(); });
    // wait for the devtools endpoint
    let ok = false;
    for (let i = 0; i < 60; i++) {
      try { if (await httpGet(port, '/json/version')) { ok = true; break; } } catch (e) {}
      await sleep(500);
    }
    if (!ok) { try { proc.kill('SIGKILL'); } catch (e) {} throw new Error('chromium devtools never came up:\n' + stderr); }
    // find the page target
    let wsUrl = null;
    for (let i = 0; i < 30; i++) {
      try {
        const page = JSON.parse(await httpGet(port, '/json')).find((t) => t.type === 'page');
        if (page && page.webSocketDebuggerUrl) { wsUrl = page.webSocketDebuggerUrl; break; }
      } catch (e) {}
      await sleep(300);
    }
    if (!wsUrl) { try { proc.kill('SIGKILL'); } catch (e) {} throw new Error('no page target'); }
    const ws = new WSClient(wsUrl); await ws.connect();
    const page = new Page(ws); await page.enable();
    const b = new Browser(); b.proc = proc; b.page = page; return b;
  }
  async close() { try { this.proc.kill('SIGKILL'); } catch (e) {} }
}

module.exports = { Browser, Page, sleep };
