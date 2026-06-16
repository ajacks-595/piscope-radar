# PiScope Radar — E2E / Computer-Use Test Plan

A full catalogue of browser-driven test cases for the PiScope web UI, written so a
**computer-use agent** (or a human) can execute each one by driving a real browser, and so the
**automated suite** (`tests/e2e/`) can regression-guard the deterministic subset.

## How to use this document

- Default target: the live Pi at **`http://10.0.0.231/piscope`** (LAN-only, no auth).
- Each case has: **ID**, **Priority** (P1 critical · P2 important · P3 nice-to-have),
  **Mode**, **Preconditions**, **Steps**, **Expected**.
- **Mode legend:**
  - **🤖 AUTO** — covered by the automated suite; the module is named in brackets, e.g. `[04]`.
  - **👁 MANUAL** — needs a human/agent to look at the screen (visual/animation/audio), non-destructive.
  - **⚠️ DESTRUCTIVE** — writes settings, sends webhooks, or spends AI/FlightAware/email budget.
    **Do not run casually against the live Pi.** Each lists a restore step. Run only with intent.

### The non-destructive rule (read before testing the live Pi)

The Pi holds **live data** and a **production config**. For routine testing, stay on 🤖/👁 cases.
These **persist settings when clicked** — treat as ⚠️ and revert after: theme picker, weather /
day-night / audio / follow toggles, airports & aero overlays, every Settings field, the receiver
location picker, bookmark/note add, webhook save/test, AI "Explain", FlightAware "Fetch", digest
"Run now", and DB import. The automated suite never touches these and verifies `GET /api/settings`
is byte-identical before/after every run.

---

## 0. Automated coverage at a glance

Run `node tests/e2e/run.js` (see `README.md`). 13 modules / 156 checks, all non-destructive:

| Module | Area | Checks |
|---|---|---|
| 01 | Page load & shell | title, map, tiles, sidebar, topbar, `/api/version`, `/health`, zero load exceptions |
| 02 | API (read-only) | 24 GET endpoints → 2xx + shape |
| 03 | Sidebar filters | search / sort / category / altitude (ephemeral) |
| 04 | Detail panel | select, ✕ close, Esc, modal-layering, mobile slide on/off-screen |
| 05 | Modals | open/✕/Esc for events·stats·views·settings; events+stats tabs; analytics ranges; `?` help |
| 06 | Keyboard | `/ e s v z r ?` and Esc (ephemeral shortcuts only) |
| 07 | Themes | all 14 themes via `applyTheme(persist:false)`; radar canvas; terminal grid |
| 08 | Overlay controls | presence + `aria-pressed` of the 8 toggles (no clicks — they persist) |
| 09 | Replay | enter via `p`, slider range, scrub, exit to live |
| 10 | URL share-state | `#center/#zoom/#theme` applied on load; pan writes the hash |
| 11 | Embed mode | `?embed=1` strips chrome; `interactive=locked` freezes the map |
| 12 | PWA | `sw.js` scope header + version-stamped cache tag; manifest scope |
| — | Non-destructive guard | settings unchanged across the whole run |

---

## 1. Page load & shell — `PS-LOAD`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-LOAD-01 | P1 | 🤖 `[01]` | Navigate to `/piscope` → title is "PiScope Radar", `#map` renders Leaflet tiles, sidebar + topbar present, no JS exceptions on load. |
| PS-LOAD-02 | P1 | 🤖 `[01]` | `GET /api/version` returns `{version}`; `GET /piscope/health` returns a `status`. |
| PS-LOAD-03 | P2 | 👁 MANUAL | First-run onboarding: with a fresh profile, the wizard (`#wizard-modal`) appears; "Skip" (`#wizard-skip`) dismisses it; it does not reappear after reload (writes a "seen" flag — mildly ⚠️). |
| PS-LOAD-04 | P2 | 👁 MANUAL | Version-bump toast: after a deploy that bumps `VERSION`, a "what's new" toast appears once. |
| PS-LOAD-05 | P3 | 👁 MANUAL | Responsive: at ≤900px the layout collapses to a full-screen map (sidebar/detail become slide-in overlays); at >900px it's the 3-column layout. |

## 2. Live map & markers — `PS-MAP`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-MAP-01 | P1 | 🤖 `[01]` | Base tiles load (`.leaflet-tile` count > 0). |
| PS-MAP-02 | P1 | 👁 MANUAL | Aircraft markers render and move over ~10s as WS updates arrive; heading rotates the icon. |
| PS-MAP-03 | P2 | 👁 MANUAL | Marker states are visually distinct: selected (scaled up), on-ground (smaller), emergency (alert colour), military. |
| PS-MAP-04 | P1 | 🤖 `[04]` | Click a marker/row → that aircraft is selected (row `.selected`, detail panel opens). |
| PS-MAP-05 | P2 | 👁 MANUAL | Right-click a marker → context menu with "Open detail panel / Follow / Copy hex / Open on adsb.fi". |
| PS-MAP-06 | P2 | 👁 MANUAL | Selecting an aircraft draws its trail (and route line if enriched); other trails dim. |
| PS-MAP-07 | P3 | 👁 MANUAL | Radar-sweep themes (`radar`/`radarModern`): the canvas sweep rotates and aircraft refresh as it passes. |
| PS-MAP-08 | P3 | 👁 MANUAL | Range rings render dashed circles centred on the receiver (if `range_rings_enabled`). |

## 3. Sidebar — list, search, filters, sort, keyboard — `PS-SIDE`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-SIDE-01 | P1 | 🤖 `[03]` | Typing in `#filter-search` narrows the list; clearing restores it. |
| PS-SIDE-02 | P2 | 🤖 `[03]` | `#filter-category` = military narrows; = all restores. |
| PS-SIDE-03 | P2 | 🤖 `[03]` | `#filter-sort` cycling (callsign/altitude/speed/distance) keeps rows rendered, no errors. |
| PS-SIDE-04 | P2 | 🤖 `[03]` | Altitude slider `#alt-min` updates its `#alt-min-val` label and filters. |
| PS-SIDE-05 | P3 | 👁 MANUAL | `#show-ground` unchecked hides on-ground aircraft; `#dist-max` shows "∞" at max. |
| PS-SIDE-06 | P2 | 🤖 `[06]` | `/` focuses the search box. |
| PS-SIDE-07 | P3 | 👁 MANUAL | `↑`/`↓` move the row cursor (`.selected`) and scroll it into view; `Enter` pans to it. |

## 4. Detail panel — `PS-DET`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-DET-01 | P1 | 🤖 `[04]` | Select → `#detail-content` shown, `#detail-close` exists, `.detail` gains `.open`. |
| PS-DET-02 | P1 | 🤖 `[04]` | `✕` (`#detail-close`) closes: content hidden, placeholder back, `.open` removed, row de-highlighted. |
| PS-DET-03 | P1 | 🤖 `[04]` | **Esc** closes the panel when no modal is open. |
| PS-DET-04 | P1 | 🤖 `[04]` | Layering: with a modal open, Esc closes the **modal** and keeps the selection; a 2nd Esc deselects. |
| PS-DET-05 | P1 | 🤖 `[04]` | **Mobile** (≤900px): select slides the panel on-screen; `✕` slides it fully off-screen. |
| PS-DET-06 | P2 | 👁 MANUAL | Panel shows callsign/badges, live data (alt/speed/track compass + RSSI bars), photo, route, quick links. |
| PS-DET-07 | P2 | 👁 MANUAL | Photo carousel: prev/next cycle multiple images when present. |
| PS-DET-08 | P3 | ⚠️ DESTRUCTIVE | Bookmark: click `#bookmark-toggle` (POST `/api/bookmarks/{hex}`). **Restore:** click again to remove, or `DELETE /api/bookmarks/{hex}`. |
| PS-DET-09 | P3 | ⚠️ DESTRUCTIVE | AI brief: `#ai-explain-btn` then a follow-up in `#ai-chat-input`/`#ai-chat-send` (POST `/api/explain*` — spends AI budget; 60/min). Verify status first via `GET /api/explain/status`. |
| PS-DET-10 | P3 | ⚠️ DESTRUCTIVE | FlightAware: `#fa-fetch-btn` (POST `/api/flightaware/{callsign}` — **spends ~5¢/call** from the monthly budget). Only with a dedicated test budget. |

## 5. Modals — `PS-MOD`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-MOD-01 | P1 | 🤖 `[05]` | Events / Stats / Views / Settings each open from their button and close via ✕ and Esc. |
| PS-MOD-02 | P2 | 🤖 `[05]` | Events tabs (`all/emergency/military/watchlist`) switch; rare via `?kind=rare`. |
| PS-MOD-03 | P2 | 🤖 `[05]` | Stats tabs (today/daily/coverage/leaderboard/records/bookmarks/analytics/health) switch. |
| PS-MOD-04 | P2 | 🤖 `[05]` | Analytics range picker (`24h/7d/30d/all`) switches and re-renders. |
| PS-MOD-05 | P2 | 🤖 `[05]` | `?` opens the keyboard-help modal; Esc closes it. |
| PS-MOD-06 | P2 | 👁 MANUAL | Stats → Coverage renders the polar diagram (`#polar-canvas`); Analytics renders the hour×weekday heatmap. |
| PS-MOD-07 | P3 | 👁 MANUAL | Operator profile: click an airline name in the detail panel → `#operator-modal` lists that operator's live fleet. |
| PS-MOD-08 | P3 | 👁 MANUAL | Modal shade click also closes (click `.modal-shade`). |

## 6. Themes & appearance — `PS-THEME`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-THEME-01 | P1 | 🤖 `[07]` | All 14 themes apply to `<html data-theme>` (driven via `persist:false` so no write). |
| PS-THEME-02 | P2 | 🤖 `[07]` | Radar themes render `#radar-canvas`; terminal theme draws the coordinate grid. |
| PS-THEME-03 | P2 | ⚠️ DESTRUCTIVE | Pick a theme from `#theme-select` (persists `theme`). **Restore:** re-select the original theme. |
| PS-THEME-04 | P3 | 👁 MANUAL | Dark-filter tile presets invert correctly (`#map[data-tile-filter="dark"]`); marker colours track altitude bands. |

## 7. Overlays & modes — `PS-OVL`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-OVL-01 | P1 | 🤖 `[08]` | The 8 toggle controls exist with a valid `aria-pressed` (read-only — they persist on click). |
| PS-OVL-02 | P1 | 🤖 `[09]` | Replay (`p`/`#toggle-replay`) is ephemeral: bar appears, slider scrubs, `#replay-live` exits. |
| PS-OVL-03 | P2 | ⚠️ DESTRUCTIVE | Weather (`#toggle-weather`) adds a RainViewer layer (persists `weather_overlay_enabled`). **Restore:** toggle off. |
| PS-OVL-04 | P2 | ⚠️ DESTRUCTIVE | Day/night (`#toggle-day-night`) draws the terminator (persists). **Restore:** toggle off. |
| PS-OVL-05 | P2 | ⚠️ DESTRUCTIVE | Heatmap (`#toggle-heatmap`) renders the activity layer; Airports (`#toggle-airports`) shows zoom-aware markers. **Restore:** toggle off. |
| PS-OVL-06 | P3 | ⚠️ DESTRUCTIVE | Audio (`#toggle-audio` / `a`) plays a confirmation blip + alert chimes (persists). **Restore:** toggle off. Needs speakers. |
| PS-OVL-07 | P3 | ⚠️ DESTRUCTIVE | Follow (`#toggle-follow` / `f`) keeps the map centred on the selection (persists). **Restore:** toggle off. |
| PS-OVL-08 | P3 | ⚠️ DESTRUCTIVE | Aviation overlay (`#toggle-aero`) needs an OpenAIP key in Settings; verifies the tile proxy. |

## 8. Replay · URL share · Embed · PWA — `PS-PLAT`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-PLAT-01 | P2 | 🤖 `[09]` | Replay timeline (`GET /api/replay/timeline`) returns points; scrubbing shows the snapshot clock. |
| PS-PLAT-02 | P2 | 🤖 `[10]` | Share link `#center=…&zoom=…&theme=…` is applied on load (theme without persisting). |
| PS-PLAT-03 | P2 | 🤖 `[10]` | Panning/selecting writes `location.hash` (debounced). |
| PS-PLAT-04 | P2 | 🤖 `[11]` | `?embed=1` hides sidebar/detail, keeps the map; `interactive=locked` disables dragging; `#embed-expand-link` present. |
| PS-PLAT-05 | P3 | 👁 MANUAL | Embed `interactive=view` opens marker clicks in a new tab; theme/colour URL tokens apply. |
| PS-PLAT-06 | P1 | 🤖 `[12]` | `sw.js` served with `Service-Worker-Allowed: /piscope` and a `piscope-shell-<ver>-<hash>` cache tag matching the running version; manifest scoped to `/piscope/`. |
| PS-PLAT-07 | P2 | 👁 MANUAL | Offline shell: load once, go offline (DevTools), reload → shell + map chrome render from cache (live data unavailable, handled gracefully). |
| PS-PLAT-08 | P3 | 👁 MANUAL | Install-to-homescreen prompt works; SW scope is `/piscope/` (not `/`). |

## 9. Events & notifications — `PS-EVT`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-EVT-01 | P2 | 🤖 `[02]` | `GET /api/events` and `?kind=military|emergency|watchlist|rare` return event arrays. |
| PS-EVT-02 | P2 | 👁 MANUAL | Event log lists recent events with kind/hex/callsign/distance; the topbar `#events-badge` reflects the count. |
| PS-EVT-03 | P3 | 👁 MANUAL | Emergency squawk (7500/7600/7700) raises a banner + log entry (observe opportunistically; cannot force). |

## 10. Analytics, records, bookmarks, notes — `PS-ANL`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-ANL-01 | P2 | 🤖 `[02]` | `GET /api/analytics?range=…`, `/notable`, `/returning`, `/leaderboard`, `/coverage`, `/heatmap`, `/records`, `/bookmarks`, `/notes` all 2xx with shape. |
| PS-ANL-02 | P3 | 👁 MANUAL | Analytics tab renders charts and the hour×weekday heatmap (UTC-labelled); range picker changes them. |
| PS-ANL-03 | P3 | ⚠️ DESTRUCTIVE | CSV export buttons (`#analytics-export-daily`/`-sightings`) download files (read-only data, but triggers a download). |
| PS-ANL-04 | P3 | ⚠️ DESTRUCTIVE | Note: type in the detail-panel note field (PUT `/api/notes/{hex}`). **Restore:** clear the note. |

## 11. Settings (all write paths) — `PS-SET` ⚠️

> Entire section is ⚠️ DESTRUCTIVE — every Save POSTs `/api/settings`. To test safely, snapshot
> first (`GET /api/settings`), make the change, verify, then restore each field to its prior value
> (secrets show `***`; a `<key>_set` flag indicates a stored secret — don't blank it accidentally).

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-SET-01 | P2 | ⚠️ | Connection tab: change `#setting-poll` / `#setting-tar-url`, Save → `GET /api/settings` reflects it; feed keeps polling. **Restore** prior values. |
| PS-SET-02 | P2 | ⚠️ | Appearance/Map: trail mode, labels, range rings persist and re-render. **Restore.** |
| PS-SET-03 | P2 | ⚠️ | Receiver location via `#pick-on-map` (right-click map → "Set receiver here"); affects distances. **Restore** prior lat/lon. |
| PS-SET-04 | P2 | ⚠️ | AI provider config + `#test-ollama-btn`/`#test-cloud-api-btn`/`#test-claude-cli-btn` (test endpoints are side-effect-free; saving keys persists secrets). |
| PS-SET-05 | P2 | ⚠️ | FlightAware key (`#save-fa-key`) and OpenAIP key (`#save-openaip-key`) set secrets (redacted). |
| PS-SET-06 | P3 | ⚠️ | Settings whitelist: an unknown key in a POST body is silently dropped; a bad `poll_interval` is clamped, not fatal. |

## 12. Webhooks & digests — `PS-HOOK` ⚠️

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-HOOK-01 | P2 | 🤖 `[02]` | `GET /api/webhooks` and `GET /api/digest` are read-only and 2xx. |
| PS-HOOK-02 | P3 | ⚠️ | Webhook builder saves a webhook (POST `/api/webhooks`) and "Test" sends a sample to the URL (POST `/api/webhooks/test` — **fires a real HTTP POST**). Use a throwaway endpoint (e.g. webhook.site). **Restore:** remove the webhook. |
| PS-HOOK-03 | P3 | ⚠️ | "Send weekly summary now" (`#run-weekly-digest`) / daily `POST /api/digest/run` — sends to subscribed webhooks/email and may spend AI budget. Run only with intent. |

## 13. Backup / export / import — `PS-BAK` ⚠️

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-BAK-01 | P2 | 👁 MANUAL | `GET /api/export` downloads a ZIP with secrets stripped (read-only of live DB). |
| PS-BAK-02 | P1 | ⚠️ | `POST /api/import` (`#import-db`, requires `X-PiScope-Import: 1`) **replaces the entire DB**. NEVER against the live Pi — use a disposable instance. |

## 14. Security & hardening — `PS-SEC`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-SEC-01 | P1 | 🤖 (pytest `test_main_http`) | Response headers: `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, enforced CSP (`script-src 'self' 'nonce-…'`, `frame-ancestors`), `Permissions-Policy`. |
| PS-SEC-02 | P1 | 👁 MANUAL | OSM tiles still load despite `no-referrer` (tile layer sets `referrerPolicy: strict-origin-when-cross-origin`). |
| PS-SEC-03 | P2 | 👁 MANUAL | Host guard (if enabled): a request with a disallowed `Host` header gets 421; the allow-listed hostname works. |
| PS-SEC-04 | P2 | 👁 MANUAL | Embedding from a disallowed origin is blocked by CSP `frame-ancestors` (default `'self'`). |
| PS-SEC-05 | P2 | 🤖 `[02]` | Invalid inputs are handled: bad hex → 400; AI/test endpoints return 200 envelopes (`source:"unavailable"`) not 5xx. |

## 15. Cross-cutting — `PS-X`

| ID | Pri | Mode | Case |
|---|---|---|---|
| PS-X-01 | P1 | 🤖 (all) | No uncaught JS exceptions or `console.error` during any interaction (asserted per-module). |
| PS-X-02 | P2 | 🤖 (guard) | A full non-destructive pass leaves `GET /api/settings` byte-identical. |
| PS-X-03 | P2 | 👁 MANUAL | WebSocket reconnects after a transient drop (kill/restore network briefly; markers resume). |
| PS-X-04 | P3 | 👁 MANUAL | Cross-browser smoke: Chromium (automated) + at least one of Firefox/WebKit loads and selects an aircraft. |
| PS-X-05 | P3 | 👁 MANUAL | Basic a11y: modals are reachable/closable by keyboard; controls have titles/aria labels. |

---

## Appendix — endpoints the automated suite must never call

POST/PUT/DELETE that mutate state or spend budget: `POST /api/settings`, `/settings/fa-key`,
`/settings/openaip-key`, `/api/explain`, `/api/explain/followup`, `POST /api/flightaware/{cs}`,
`PUT /api/notes/{hex}`, `POST|DELETE /api/bookmarks/{hex}`, `POST /api/webhooks`,
`/api/webhooks/test`, `POST /api/views`, `/api/digest/run`, `/api/digest/weekly/run`,
`POST /api/import`. (`/api/*/test` connectivity checks and `/api/test-connection` are side-effect-free.)
