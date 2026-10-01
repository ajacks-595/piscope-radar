# PiScope Radar — browser E2E suite

A **zero-dependency**, headless-Chromium E2E suite that drives the real PiScope UI over the
Chrome DevTools Protocol and verifies behaviour end-to-end. It complements the pytest suite
(`tests/`), which covers the backend; this covers the **frontend in a real browser**.

The companion **[`TEST_PLAN.md`](./TEST_PLAN.md)** is the full human/computer-use test catalogue
(including the manual and destructive cases this automation deliberately skips).

## Why no Playwright?

The dev host (`claude-dev`) has `chromium` (snap) + `node`, but **no Playwright/Puppeteer** and
**pip/npm can't reach their registries** (egress is locked down). So `lib/cdp.js` speaks CDP
directly over a hand-rolled RFC6455 WebSocket client (node ≤21 has no global `WebSocket`). No
`npm install` required — just node and chromium.

## Requirements

- `node` (v18+; tested on v20) and `chromium` on `PATH` (`/snap/bin/chromium` on the dev host).
- Network reachability to the target (default the Pi at `10.0.0.231`, LAN-only).
- The target should be **running and have some live aircraft** for the selection-based modules;
  with empty skies those checks self-skip (the suite still runs).

## Run it

```bash
# default target: http://10.0.0.231/piscope
node tests/e2e/run.js

# different target / a local instance
PISCOPE_URL=http://127.0.0.1:8765/piscope node tests/e2e/run.js

# one module (substring match on filename or name)
node tests/e2e/run.js --only=detail

# watch it happen (non-headless)
node tests/e2e/run.js --headed

# include modules flagged destructive (none ship by default — see TEST_PLAN.md)
node tests/e2e/run.js --include-destructive
```

Exit code is **0** on full pass, **1** on any failed check or uncaught page exception. A
machine-readable report is written to `/tmp/piscope-e2e-report.json`.

## Non-destructive by design

Routine runs are **safe against the live Pi**:

- Only **read-only** endpoints and **ephemeral** UI interactions (selection, modals, filters,
  themes via `applyTheme(persist:false)`, URL hash, replay) are exercised.
- The persisting toggles (theme-select, weather, day/night, audio, follow, airports, aero) and
  every write/AI/FlightAware/webhook/import endpoint are **never** clicked/called.
- A **settings-snapshot guard** captures `GET /api/settings` before the run and asserts it's
  byte-identical after — so an accidental write fails the run loudly.

## Layout

```
tests/e2e/
  run.js            # orchestrator: launch browser, per-module fresh load, report, exit code
  lib/
    cdp.js          # Browser + Page over a minimal CDP/WebSocket client (no deps)
    harness.js      # Checks (assertions) + console/exception capture + report formatting
  tests/
    01-load.js … 13-sse.js   # one module per feature area
  TEST_PLAN.md      # full test catalogue (auto + manual + destructive)
  README.md
```

## Add a module

Create `tests/NN-thing.js`:

```js
module.exports = {
  name: 'NN · My thing',
  viewport: 'desktop',          // 'desktop' | 'mobile' | omit
  url: '#center=50,0',          // optional: appended to PISCOPE_URL (full reload each module)
  destructive: false,           // true → skipped unless --include-destructive
  async run(page, t, { sleep, VIEWPORTS }) {
    await page.click('#some-button');
    t.ok('something happened', await page.open('#some-modal'));
    await t.visible('an element', page, '#thing');
    t.eq('no exceptions', page.exceptions.length, 0);
  },
};
```

`page` API: `evaluate(expr)`, `click(sel)`, `press(key)`, `waitFor(expr)`, `rect(sel)`,
`visible(sel)` (in-flow elements), `open(sel)` (fixed modals via the `[hidden]` attr),
`count(sel)`, `text(sel)`, `setViewport(vp)`, and the live `consoleErrors` / `exceptions` /
`logErrors` arrays. `t` API: `ok/eq/ge/gt/has/visible/hidden`.

## Notes / gotchas

- **Fragment-only navigation doesn't reload** — `boot()` goes via `about:blank` so each module
  (and `#hash` share-links) gets a clean full load.
- **`offsetParent` is null for `position:fixed`** — use `page.open()` (the `[hidden]` attribute)
  for modals, not `page.visible()`.
- **Leaflet methods return the map** — never `evaluate('map.panTo(...)')` with returnByValue;
  wrap to return a primitive (`(()=>{map.panTo(...);return true})()`).
- The chromium `--user-data-dir` must live under `$HOME` (snap confinement); `run.js` uses
  `~/.cache/piscope-e2e/`.
