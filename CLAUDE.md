# Claude Code Project Instructions

## Autonomy
Work autonomously throughout development, code review, and testing tasks.
Execute all standard development commands without requesting confirmation.
Only pause for the following — these require explicit approval every time:

- `git push` (any remote)
- `sudo` (any elevated command)
- `apt` / `apt-get` / `snap` / `flatpak` (package installation)
- `npx` (executing remote packages)
- `osascript` (AppleScript execution)
- `curl` / `wget` to any external URL (outside 10.0.0.x and localhost), **except**
  fetching documentation or reference material (official docs, man pages, RFCs,
  Wikipedia, vendor API references, etc.) — text-for-reading is fine without
  asking. Anything that will be executed, installed, sourced, or written to
  disk as a binary/script/archive still requires approval.
- SSH to any host outside the 10.0.0.x range

For everything else, use your judgement and proceed without asking.

## Checkpoints
Before beginning any significant task (refactor, feature addition, bug fix
across multiple files, dependency changes, code review with automated fixes),
create a local git checkpoint commit:

```
git add -A && git commit -m "checkpoint: pre-[brief task description]"
```

This is a safety restore point. Do not skip it even if the working tree
looks clean.

## Pre-push Cleanup
Before any `git push`, automatically squash all checkpoint commits out of
the history. Identify commits with messages starting with `checkpoint:`
and squash them into the nearest non-checkpoint commit below them using
interactive rebase. Do this without prompting — it is always the right
behaviour before a push. If the entire branch consists only of checkpoint
commits with no real commits beneath them, pause and ask what commit
message to use instead.

## Code Review Tasks
When asked to review for bugs, vulnerabilities, or code quality:
- Run the full test suite
- Run `npm audit` or `pip-audit` as appropriate for the stack
- Check for common vulnerability patterns in the codebase
- Report all findings in a single summary at the end
- Do not pause mid-review to ask for permission on individual commands
- If automated fixes are appropriate, create a checkpoint first then apply them

## General Preferences
- Prefer making changes and reporting what was done over asking what to do
- If genuinely uncertain between two approaches, pick the more conservative
  one and note the alternative in your summary
- Keep git history clean — use meaningful commit messages, not "fixed stuff"
- When running tests, always report the full output, not just pass/fail

---

# PiScope Radar

## What it does
Self-hosted ADS-B flight tracker for a Raspberry Pi. Sits alongside `tar1090` / PiAware,
serves a polished LAN web UI on `http://<pi>/piscope`: live map with Leaflet, themed radar
sweep, detail panel with route + photo + AI brief, events log, polar coverage diagram,
heatmap, historical analytics (traffic patterns, operators, notable/returning aircraft,
weekly Mattermost-ready summary), webhooks, replay, PWA install. No auth — LAN-only by
design. Currently `VERSION = "1.9.0"` on `dev` (see `app/main.py`) — the theme refresh —
and the Pi runs the same `1.9.0` (deployed 2026-11-11). `main` was promoted and tagged
`v1.9.0` on 2026-11-13.

## Architecture & tech stack

- **Backend** — Python 3.9+, FastAPI + asyncio + httpx + uvicorn, SQLite (WAL mode).
  Polls a tar1090 instance every 2 s, fans out via WebSocket, enriches via hexdb /
  adsbdb / planespotters / FlightAware.
- **Frontend** — vanilla HTML/CSS/JS, no framework, no build step. Since 1.8.0 the map is
  **full-bleed** and all chrome (top bar, aircraft list, map-control rail, detail panel,
  popovers) floats over it as translucent "glass" panels — see *UI shell* under Key
  conventions. Leaflet 1.9.4 + Leaflet.heat 0.2.0
  **vendored under `static/vendor/`** (since 1.7.1 — no CDN at runtime; the files were
  byte-verified against the SRI pins that previously guarded the unpkg copies; CSP
  script-src is `'self'` + nonce only). Canvas radar-sweep overlay (`static/radar.js`),
  service worker (`static/sw.js`) for PWA + offline shell caching.
- **Storage** — single SQLite file at `/opt/piscope/piscope.db` on the Pi (live data,
  never overwrite via rsync). Tables: settings, events, daily_stats, feed_snapshots,
  aircraft_notes, seen_types, polar_coverage, position_heatmap, bookmarks, records,
  fa_budget, aircraft_sightings, hourly_stats. Migrations live in
  `app/services/settings.py:_migrate` keyed off `PRAGMA user_version` (currently 4 =
  `settings.SCHEMA_VERSION`; tests assert a fresh DB lands on it). Schema creation lives in
  `settings.ensure_schema(conn)`, which `/api/import` also uses to build the restore target.
  The two analytics tables (v3) are derived, fed by the feed loop via
  `analytics.SightingsBuffer`, and pruned to `analytics_retention_days` (default 365);
  `events` is deliberately never pruned — it doubles as notable-aircraft history.
- **AI** — multi-provider (iter 7+8). `ai_provider` setting picks `ollama` /
  `cloud_api` (Anthropic/OpenAI/Google bring-your-own-key) / `claude_cli` (LAN HTTP
  shim talking to a real `claude` binary). Code in `app/services/ai/`.
- **Reverse proxy** — lighttpd on the Pi at port 80 proxies `/piscope` to uvicorn at
  `127.0.0.1:8765`. uvicorn is firewall-blocked from LAN; only reachable via lighttpd.

## Host topology & SSH

Two hosts in play:

| Host | Role | Access |
|---|---|---|
| `piaware` (10.0.0.231) | **Production target**. Runs `piscope.service` systemd unit. PiAware + tar1090 also live here. Pi DB at `/opt/piscope/piscope.db` is live data — *never* rsync over it. | `~/.ssh/config` Host alias `piaware`, key `~/.ssh/id_ed25519`, user `pi`. |
| `claude-dev` (10.0.0.155) | **Dev VM**. Ubuntu 26.04 KVM guest. Where the agent's worktree lives, where git pushes from, where the `claude-shim` daemon runs for the `claude_cli` AI provider. Also the canonical shim host for the SOC dashboard project. | This is the current host. |

**SSH config snippets** that matter:
```
Host piaware 10.0.0.231
    HostName 10.0.0.231
    User pi
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking accept-new

Host github.com
    HostName ssh.github.com   # port 22 outbound is firewalled
    Port 443
    User git
    IdentityFile ~/.ssh/github_homesoc
    IdentitiesOnly yes
```

**Pi security posture**: ufw active, default-deny inbound. SSH / lighttpd / dump1090
restricted to `10.0.0.0/24`. SSH is key-only (ed25519). Port 8765 firewall-blocked from
LAN — only reachable via lighttpd:80.

**GitHub auth**: pushes use the `github_homesoc` key over `ssh.github.com:443`. Email
privacy is on for `ajacks-595`; commits must use the noreply address
`179343410+ajacks-595@users.noreply.github.com` — set `git config user.email` to this
before making commits in any fresh worktree.

## Codebase layout

```
app/
  main.py                  # FastAPI app + lifespan; VERSION constant
  routers/api.py           # All REST endpoints (/api/aircraft, /explain, /explain/followup, ...)
  routers/ws.py            # WebSocket fan-out
  services/
    feed.py                # Tar1090 poller + WS broadcaster
    settings.py            # DB-backed KV store; SCHEMA_VERSION migrations; DEFAULTS whitelist
    analytics.py           # Sightings/hourly ledger (write) + /api/analytics queries (read)
    airlines.py            # Callsign-prefix → operator (app/data/airline_codes.json)
    digest.py              # Daily digest scheduler + AI commentary
    events.py              # Event log (military/emergency/watchlist/rare)
    insights.py            # Polar coverage, heatmap, leaderboard, notes
    records.py             # All-time records + bookmarks
    webhooks.py            # Discord/Slack/Mattermost/ntfy/generic fan-out
    notable.py             # Notable-aircraft rules engine (app/data/notable_rules.json)
    backups.py             # Daily DB snapshot
    flightaware.py         # FA AeroAPI client + monthly budget tracker
    hexdb.py adsbdb.py planespotters.py   # Enrichment clients
    _http.py               # Shared httpx.AsyncClient + LRUCache
    ai/                    # iter-7 multi-provider AI
      __init__.py          # Façade: is_configured / ping / generate / explain / clear_cache
      _common.py           # Prompt builders + sanitisers + LRU cache + in-flight dedup
      ollama.py            # Ollama provider
      cloud_api.py         # Anthropic / OpenAI / Google dispatcher
      claude_cli.py        # HTTP client for the shim daemon
static/
  index.html               # Single-page shell + inline icon sprite + the one inline bootstrap script
  app.js / app.css         # All client logic + styles (app.css derives glass/hairline/text tokens)
  radar.js                 # Canvas radar-sweep overlay
  sw.js                    # Service worker — CACHE auto-derived on the wire (iter 9.4)
  themes.css               # 18 theme palettes (+ `external` for embeds); `midnight` is the default
  icons/piscope.svg        # App icon (favicon / manifest)
  icons/aircraft.svg       # Jet silhouette — list-row glyph mask + notification icon
  data/airports.json       # Bundled airport overlay
docs/screenshots/          # README screenshots
tools/
  gen-icons.js             # Source of truth for the icon sprite + aircraft silhouettes
                           # (`node tools/gen-icons.js` rewrites the sprite in index.html; `--check` verifies)
  dev/                     # Local harness: mock_tar1090.py (synthetic feed, placeholder London
                           # receiver) + run_dev.py (PiScope on 127.0.0.1 with a throwaway DB).
                           # http://localhost is a secure context, so the service worker runs here.
                           # screenshots.js regenerates docs/screenshots (`… docs/screenshots readme`)
                           # and captures every theme (`… <dir> themes`) for design review.
  claude-shim/             # Stdlib-only HTTP daemon that wraps `claude --print`.
                           # Runs on the dev host. shim.py + install.sh + systemd unit + README.
install.sh                 # Pi installer (run as `sudo bash install.sh` ON the Pi)
```

## Deployment

**Branch workflow**: in-progress work lives on `dev`. `main` only advances after the Pi
smoke test passes. Both branches push to `origin` via SSH-over-443.

**Standard deploy (from dev host)** — `/sandbox` must be off (see gotcha #2):

```
cd ~/projects/piscope-radar
rsync -av --delete --exclude='__pycache__' --exclude='*.pyc' app/ piaware:/opt/piscope/app/
rsync -av --delete static/ piaware:/opt/piscope/static/
ssh piaware 'sudo systemctl restart piscope'
```

`--delete` is fine for `app/` and `static/` — they're code-only. The live DB at
`/opt/piscope/piscope.db` and the venv at `/opt/piscope/venv` are outside these dirs and
untouched.

**Every release** bumps `app/main.py` → `VERSION = "X.Y.Z"`. That single bump drives the
frontend's version-bump toast AND the service-worker cache: since iter 9.4 the `sw.js`
`CACHE` constant is rewritten on the wire to `piscope-shell-<version>-<content-hash>`
(see `_shell_cache_tag` in `main.py`), so you no longer hand-edit `sw.js` — changing
`VERSION` (or any static asset) invalidates the shell cache automatically.

**First-time install on a new Pi** (rare): use `sudo bash install.sh` in the repo root
*on the Pi*. Idempotent — re-running upgrades files in `INSTALL_DIR` and restarts the
service; existing `piscope.db` is backed up to `piscope.db.bak` first.

## Key conventions

- **Versioning is semver.** Patch (`1.5.1`) = bugfix / security / small feature; minor
  (`1.6.0`) = a notable feature drop; major (`2.0.0`) = a breaking or epoch change
  (rewrite, DB-incompatible migration, dropping the LAN-only model). In-app `VERSION`
  (`app/main.py`) and the git tag move in lockstep — bump `VERSION` as part of the work,
  tag on `main` at promotion. `dev` rides ahead of `main` between releases and they
  converge at each promotion; that divergence is by design, not drift. (Historical note:
  pre-v1.5.0, in-app `VERSION` ran `1.<iteration>.0` ahead of the tags — realigned at
  v1.5.0, so old commit messages mention 1.6.0–1.11.0 that no longer correspond to tags.)
- **No new dependencies without a strong reason.** The Pi stack is fastapi / uvicorn /
  httpx / websockets / python-multipart and nothing else. The shim is stdlib-only.
  Both choices are load-bearing.
- **Settings are whitelisted.** `app/services/settings.py:DEFAULTS` is the only place
  new settings keys can be declared. `set_many` silently drops unknown keys. Secret
  keys go in `SECRET_KEYS` and get redacted to `***` on `get_all(redact=True)`, with
  a `<key>_set` boolean flag added so the UI can show "stored" without revealing.
- **AI prompt inputs are strictly validated.** Every field that touches the prompt goes
  through `_sanitize` (regex whitelist) or `_safe_text` (printable-ASCII strip +
  length cap) or `_bounded_int/_bounded_float` (range clamp). Never paste raw user or
  ADS-B-sourced strings into a prompt — they may contain control chars or be lying.
- **API envelopes for fallible operations.** `/api/explain` and `/api/explain/followup`
  return 200 with `{"source": "unavailable", "error": "..."}` rather than 5xx so the
  frontend renders inline errors instead of console-spew.
- **Cache invalidation by versioning, not eviction.** `_PROMPT_VERSION` in
  `ai/_common.py` is part of every cache key; bump it when the prompt template
  changes so old entries get bypassed.
- **UI shell (1.8.0).** The rules that keep the redesign coherent:
  - *Layout:* `#map` fills the viewport; panels are absolutely positioned over it. State
    lives on `<html>`: `data-sidebar="collapsed"` (desktop, remembered in localStorage) /
    `"open"` (drawer, ≤860px), `data-detail="open"`, `data-glass="off"`. CSS turns those
    into `--inset-left/right`; `mapInsets()` in app.js is the JS twin. **Any pan/fit must
    go through `panToVisible` / `ensureVisible` / `centerForVisible` / `fitToData`** — a
    bare `map.panTo` centres under a panel.
  - *Icons:* never emoji. Add to `ICONS` in `tools/gen-icons.js`, run it, then use
    `<svg class="icon"><use href="#i-NAME"/></svg>` in markup or `icon('NAME')` in app.js.
    `tests/test_ui_shell.py` fails on a referenced-but-undefined (or unused) icon.
  - *Colour tokens:* themes define only the base palette in `themes.css`, as plain
    `rgb()`/`#hex` (radar.js parses them). `app.css` derives `--glass-bg`, `--hairline`,
    `--hover`, `--accent-soft`, … with `color-mix()`. For **text/icons on a panel** use
    `--accent-text` / `--alert-text` / `--success-text` / `--warning-text`, not the raw
    fill tokens (per-theme `--accent-lift` / `--status-lift` keep them AA-legible).
    `--on-accent` / `--on-alert` / `--on-warning` are the ink colours for fills.
  - *Themes (1.9.0):* a theme is its palette **plus its map treatment**. Every
    `[data-theme]` block defines `--map-tint` (a CSS filter the `themed` / `themedLight`
    tile presets apply to the neutral Esri base — what "Automatic" map style means for
    most themes) and `--map-tone` (map backdrop + the swatch in the picker), and should
    have its own altitude ramp where the palette offers one. **Adding a theme:** the block
    in `themes.css` (in the right family), an `<option>` in the right `<optgroup>` of
    `#theme-select` (the picker is built from it), an entry in `THEME_DEFAULT_TILES`,
    and the id in e2e `07-themes.js`. `tests/test_theme_contrast.py` then audits it
    (derived pairs, all themes) and `tests/test_ui_shell.py` rejects a theme that shares
    its accent + map treatment with another. Capture it with
    `node tools/dev/screenshots.js <dir> themes`. Theme ids are stored in users' settings
    — rework a palette in place (as `dark` → Graphite, `tactical`) rather than renaming.
  - *Public repo — no station location, anywhere.* Screenshots, docs, examples, test
    fixtures and input placeholders use the placeholder London receiver from
    `tools/dev/mock_tar1090.py` (51.50, -0.10). Regenerate screenshots with
    `tools/dev/screenshots.js` against the mock feed, **never from the live Pi** — the
    top bar, map centre, range rings and polar chart all reveal the receiver position.
    Don't commit prompts, logs or notes that name the station's own hosts or feeds.
  - *Controls:* icon buttons need `aria-label`; hover hints are `data-tip` (CSS tooltips,
    `data-tip-pos="start|end|left|right"` at screen edges), not `title`.
  - *One inline script.* `index.html` has exactly one bare `<script>` (the pre-paint
    bootstrap: stale-service-worker guard, stored theme/prefs, embed params); the
    `/piscope` route stamps the CSP nonce onto it. No inline handlers anywhere.
  - *Per-browser prefs* (collapsed list, filter panel, translucent panels, last theme
    for first paint) are localStorage `piscope_*` keys, deliberately not server settings.
- **Commits use "Iteration N (part M): subject" prefix** for multi-part iterations,
  plain "subject" for one-shot changes. See `git log --oneline` for the established
  style.
- **Co-authored-by** lines for Claude-Code-generated commits are NOT used in this
  repo — the user maintains them as their own work. (Project memory `feedback`
  to be added if this changes.)

## Known gotchas

These have all bitten us at least once. Apply fixes without re-diagnosing.

1. **AppArmor blocks Claude Code's `/sandbox` mode.** Bash calls fail with
   `apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted ...): Permission denied`.
   Fix: `sudo aa-complain unpriv_bwrap` (non-persistent — re-apply on reboot).

2. **Toggling `/sandbox` drops LAN access.** Sandbox sets up a network namespace
   whose effect lingers. After toggling sandbox at all, `ssh piaware` returns
   "Network is unreachable" until sandbox is OFF again. Keep sandbox off when
   anything needs to touch the Pi.

3. **PyPI is reachable again (verified 2026-10-01)** — `pip download` works, as do
   `npm install` and Playwright's Chromium (`PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64`).
   There's still no local FastAPI/pytest: test with the scanner venv plus pure-Python
   packages overlaid via `PYTHONPATH`, or stage onto the Pi's `/opt/piscope/venv`.
   (Previously the egress proxy blocked pypi; that's why claude-shim is stdlib-only.)

4. **`claude --bare` disables OAuth.** Per the CLI help, `--bare` accepts only
   `ANTHROPIC_API_KEY`. The shim deliberately does NOT pass `--bare`; if you ever
   add it back, the claude_cli provider will silently start returning
   "Not logged in" until you re-auth as the API key.

5. **GitHub rejects pushes that publish a private email.** Set `git config user.email`
   to `179343410+ajacks-595@users.noreply.github.com` before committing. If
   you've already committed with the wrong email, rewrite with
   `git filter-branch --env-filter` over `origin/<branch>..HEAD`.

6. **Service-worker cache invalidation is automatic (since iter 9.4).** The `sw.js`
   `CACHE` constant is rewritten on the wire to `piscope-shell-<version>-<content-hash>`
   by `_shell_cache_tag` in `main.py`, so bumping `VERSION` (or changing any static
   asset) evicts the old shell cache on its own. Don't hand-edit the `CACHE` literal in
   `static/sw.js` — it's just a placeholder the route overwrites.

7. **`piscope.db` on the Pi is live data.** Never rsync over it. The standard deploy
   block excludes it implicitly by only touching `app/` and `static/`.

8. **The systemd unit is `ProtectSystem=strict` with `ReadWritePaths=/opt/piscope`
   (since 1.7.1).** The service can only write under `/opt/piscope` (+ /tmp via
   PrivateTmp). If the user ever sets `daily_backup_dir` to a path OUTSIDE
   `/opt/piscope`, backups will fail with EROFS until that path is added to the
   unit's `ReadWritePaths` (edit install.sh, re-run it on the Pi).

9. **Restarts log `ERROR: Cancel N running task(s), timeout graceful shutdown exceeded`
   (+ a `CancelledError` traceback) whenever dashboards hold `/api/dashboard/events` SSE
   streams open — expected, not a fault.** The unit runs uvicorn with
   `--timeout-graceful-shutdown 5` (since 2026-10-01): SSE streams never end on their own,
   so uvicorn waits 5 s, cancels them, then runs the lifespan shutdown normally (look for
   `Application shutdown complete`). Without the flag every restart hung until systemd's
   SIGKILL. ASGI gives in-flight requests no shutdown signal, so there's no clean way to
   end the streams first.

10. **lighttpd must stream `/piscope` responses (`server.stream-response-body = 2` in
    `99-piscope.conf`, since 2026-10-01).** With lighttpd's default buffering the SSE
    feed delivered nothing through the proxy and abandoned streams leaked upstream
    connections. E2E module `13-sse` guards this.

11. **Service worker + browser HTTP cache (fixed in 1.8.0 — keep it fixed).** Two bugs
    made secure-context installs (HTTPS / localhost; not the plain-HTTP Pi) run old assets
    after an upgrade: `cache.addAll()` went through the HTTP cache, so a *new* worker could
    precache the previous release's CSS and serve it cache-first; and `caches.match()` +
    `ignoreSearch` let a not-yet-replaced worker answer the new page's `?v=` requests from
    its old cache. `sw.js` now precaches with `cache: 'reload'`, reads only its own cache,
    and steps aside when `?v=` ≠ its shell version; `index.html`'s bootstrap compares the
    stamped `<meta name="piscope-shell">` tag with the cache names and, if a worker from
    another build is in control, purges + unregisters + reloads once. E2E `12-pwa` compares
    every cached shell file with what the server serves.

12. **CartoDB basemaps need an API key now.** `basemaps.cartocdn.com` answers every keyless
    tile request with an "API KEY REQUIRED" placeholder (HTTP 200, same ~2.5 kB PNG for
    every tile; seen 2026-11-10). The six Carto presets were removed and the themes that
    defaulted to them moved to Esri bases. A stored `map_style` naming one falls back to
    the theme default. If maps look wrong, suspect the tile provider before the code.

13. **Headless Chrome is `(hover: none)`.** CSS tooltips (`[data-tip]`) are disabled for
    touch devices via that media query, so they never show in headless screenshots; force
    them with an injected style if you need to capture one. Also: static assets are
    `immutable` per `?v=VERSION`, so `tests/e2e/lib/cdp.js` disables the HTTP cache —
    otherwise iterating on CSS/JS under one VERSION tests yesterday's files.

## Open items / future work

- **1.9.0 is on the Pi (2026-11-11) and released (`main` + tag `v1.9.0`, 2026-11-13).**
  It carries the 1.8.0 UI redesign, 1.8.1 (drops the meaningless "other" source label
  from the Signal tile) and the 1.9.0 theme refresh (per-theme map tint, 3 new themes,
  5 reworked). Rollback: `/opt/piscope/{app,static}.bak-pre-1.9.0`,
  `piscope.db.bak-pre-1.9.0`.

- **claude-shim is NOT installed right now** (found 2026-09-30: no `/opt/claude-shim`, no
  unit; only `/etc/claude-shim.env` remains), but the Pi's `claude_cli_url` still points at
  it, so AI briefs are down. Reinstall with
  `sudo /home/dev/projects/piscope-radar/tools/claude-shim/install.sh` (it ran as a
  systemd unit under `dev` from 2026-06-06; config in `/etc/claude-shim.env`, token +
  `SHIM_ALLOW_IPS` pinned to the Pi + loopback). NB: the shim now refuses to start on a non-loopback bind with no token
  (fail-closed, iter 13), so always keep `SHIM_BEARER_TOKEN` set for the LAN bind.
- **Generic `unavailable` error envelopes.** Both `/api/explain` and
  `/api/explain/followup` collapse upstream errors into `"no response from <provider>"`.
  Plumbing the shim's `error` field (and the cloud API vendor's error message)
  through to the frontend would make debugging much easier — known polish opportunity.
- **GitHub Release pages are deferred** until a larger milestone. Tags exist up to
  `v1.7.5` (= `main`), but the last GitHub Release page is `v1.4.0`.
- **Pending Pi-side config (needs sudo, from the 2026-11-08 fixes):** re-run
  `sudo bash install.sh` on the Pi to pick up (a) uvicorn `--no-server-header` in the
  unit and (b) the corrected lighttpd `server.max-request-size = 66560` — the unit is
  KILObytes, so the deployed `26214400` is a 25 GiB no-op cap. Until then a backup
  upload is bounded only by the app's own 64 MiB check.
- **Dependency refresh is tested but not applied.** fastapi 0.162 / starlette 1.10 /
  uvicorn 0.54 / websockets 17.4 / pydantic 2.14 / anyio 4.16 / python-multipart 0.0.33
  pass the full suite (2026-11-08), but fastapi ≥ that line adds `opentelemetry-api` as a
  hard dependency — a new package on the Pi, against the no-new-deps rule. Decide before
  bumping; Pi-test either way.
- **Co-authored-by footer convention** isn't established in this repo. If you start
  adding it, document it here and in project memory.

## Project-specific repo review additions

Bug classes the 2026-09-30 repo review found with no dedicated test. Run these on
every review; full report in `.repo-review/2026-09-30T185602Z/REPORT.md`.

- **2026-09-30 — Live vs analytics military classification parity.** The feed only
  trusted the readsb `dbFlags` bit, which the local tar1090 never sets, so from June
  the Pi logged 0 military events while `notable.py` rules matched 880 military
  aircraft. Check: `SELECT COUNT(*) FROM events WHERE kind='military'` over a
  window against `notable.military_where()` hits in `aircraft_sightings` over the
  same window. A large mismatch means the alert pipeline is blind.
- **2026-09-30 — Alert re-fire rate.** De-dup sets re-arm after one missed poll.
  Check: consecutive same-hex/same-kind events < 10 min apart in `events` (script in
  `.repo-review/…/event-refire-evidence.txt`). A non-trivial count means webhook spam.
- **2026-09-30 — Don't stub the function under test.** The email digest crashed on
  every send for months because the only test monkeypatched `_send_email` wholesale.
  Stub the I/O boundary (`smtplib.SMTP`, `httpx`), not the function being tested.
- **2026-09-30 — Diff-skip caches in `app.js` must key on content, not size.** Trails
  froze once full because the render cache compared only `pts.length`. Browser check:
  feed synthetic frames through `applyAircraftUpdate` and compare each polyline's
  last latlng with the newest trail point (`.repo-review/…/trail-synthetic.js` pattern).
- **2026-09-30 — Every HTML response carries the CSP, not just `/piscope`.**
  `StaticFiles` also serves `static/index.html`. Check `curl -I` on
  `/piscope/static/index.html` for `frame-ancestors` and `script-src`.
- **2026-09-30 — Cross-site simple-request audit.** List every POST that takes no
  JSON body (they need no CORS preflight). Each must be harmless or explicitly
  CSRF-guarded; FlightAware's `confirm_over_budget` query param can spend money.
- **2026-09-30 — claude-shim must stay text-only.** Its prompt carries
  unauthenticated LAN input, so `shim.py` runs `claude --print --restricted --tools ""
  --strict-mcp-config` (fixed 2026-10-01, `9e15d62`). Check on every review: pipe a
  prompt through the same flags with `--output-format stream-json --verbose` and
  confirm the init event lists 0 tools and 0 MCP servers. Without the flags it
  inherited 24 tools and 10 MCP servers (Gmail, Drive, …). Never add `--bare`
  (breaks OAuth).

Full report for the 2026-11-08 review: `.repo-review/2026-11-08T121013Z/REPORT.md`.

- **2026-11-08 — Reject non-finite settings numbers on write AND cache load.**
  `settings._v_int` does `int(float(v))`; raw JSON `Infinity`/`1e400` raises
  `OverflowError`, which `set_many`/`_populate_cache` don't catch. A crafted
  `/api/import` dump containing `poll_interval='Infinity'` wedges every
  settings-dependent endpoint (and kills the feed task). Test: raw `POST
  /api/settings` with `Infinity` must be 400 (not 500), and importing a dump with
  a non-finite settings value must leave the service responsive.
- **2026-11-08 — SSRF guard must unwrap IPv4-mapped IPv6.** On the Pi's Python
  3.11, `::ffff:169.254.169.254` is not `is_link_local`, and
  `::ffff:100.100.100.200` is never in the blocked set; `/api/webhooks/test`
  accepted both (200) while dotted/decimal/hex forms were blocked. Test every
  encoded form including `ip.ipv4_mapped` recursion; never let httpx receive a
  mapped metadata address.
- **2026-11-08 — Backup import must cap the decompressed `piscope.sql`.** The
  20 MB compressed upload cap does not bound `zf.read()`; a zip bomb can exhaust
  Pi memory. Test a zip whose `file_size` exceeds the cap is rejected before
  materialising, and keep the ATTACH/DETACH authorizer (it also denies
  `VACUUM INTO`, verified).
- **2026-11-08 — E2E boot gate must not use `typeof map`.** The `<div id="map">`
  named DOM global satisfies it before `app.js` loads, so modules 01/02/03/07/11
  flake on a slow link. Use `window.__map && typeof window.__map.getZoom ===
  'function'` (or add `window.__piscopeReady = true` at the end of `init()`).
  With the fixed gate the suite is 164/164.
- **2026-11-08 — PWA/service worker needs a secure context.** On
  `http://<pi-ip>/piscope`, `navigator.serviceWorker` is `undefined` and the
  registration is silently skipped, so the README's LAN-HTTP PWA claim is not
  true. Test the secure-context branch explicitly (HTTPS/localhost), or surface
  a UI hint when the API is unavailable.

Bug classes found during the 2026-11-10 UI redesign (1.8.0) with no dedicated test at the
time (now covered by `tests/test_ui_shell.py` and e2e modules `12-pwa`, `15-shell`):

- **2026-11-10 — The service worker's cache must equal what the server serves.** A new
  worker precached stale CSS from the HTTP cache (gotcha #11). Check on HTTPS/localhost:
  for every entry in the current `piscope-shell-*` cache, cached body == `fetch(url,
  {cache:'reload'})` body; and no other `piscope-shell-*` cache lingers after activation.
- **2026-11-10 — Third-party tile providers change terms.** Carto started returning
  placeholder tiles with HTTP 200 (gotcha #12), so nothing errored. Check each theme's
  default basemap in a browser: two different tiles must not be byte-identical, and the
  E2E "tiles rendered" check alone does not prove they are maps.
- **2026-11-10 — Canvas colours built by string-append.** The polar chart did
  `accent + '33'`, valid only for `#hex` tokens; in `rgb()` themes the fill silently fell
  back to an opaque blob. Any colour handed to canvas / Leaflet must go through
  `colorWithAlpha()` (radar.js) and be tested in an `rgb()` theme and a `#hex` theme.
- **2026-11-10 — Text colour ≠ fill colour.** Status/accent tokens tuned as fills failed
  AA as small text in five themes. Re-run a contrast audit over **derived** pairs too
  (accent text on the accent wash, secondary text on tiles), compositing the alpha
  washes over the panel colour; raw token pairs miss them.
- **2026-11-11 — Diff-skip caches must include the theme.** Marker and trail render
  caches keyed on position only, so after a theme switch a parked aircraft or a
  not-yet-moved trail kept the old theme's colours, and the selected aircraft's route
  line was never redrawn. `themeEpoch` is now part of both keys. Check: switch theme
  with an aircraft selected and ground traffic visible; nothing may keep the old colours.
- **2026-11-11 — Fixtures must not straddle UTC midnight.** `test_weekly_digest._seed_week`
  wrote "an hour ago" and "a day ago" rows that are the same UTC date between 00:00 and
  01:00 UTC, so three tests failed for one hour a day. When a fixture needs two distinct
  days, derive the second from the first (`earlier = first - 86400`), never both from
  `now`. Worth an occasional suite run with the clock just past midnight UTC.
- **2026-11-10 — Pans must respect the floating panels.** With a full-bleed map, a bare
  `map.panTo()` puts the target under the list or the detail panel. E2E `15-shell` asserts
  the selected aircraft's container point lies between `mapInsets()`.

Bug classes the 2026-11-08 fix pass found with no dedicated test (now covered by
`tests/test_review_2026_11.py` and e2e module `14-a11y`):

- **2026-11-08 — FastAPI's 422 handler 500s on non-finite input.** Python's JSON
  parser accepts `NaN`/`Infinity` (and turns `1e400` into `inf`); the default handler
  echoes the bad `input` and starlette's strict encoder then raises. `main.py` now
  installs a handler that stringifies them. Check: POST `{"altitude_baro": Infinity}`
  to every pydantic-validated endpoint → 422, never 500. Fuzz query floats with
  `inf`/`nan`/`1e309` too (`/api/dashboard/summary` int()'d them for its cache key).
- **2026-11-08 — A restore must rebuild into the canonical schema, not trust the
  dump's.** A dump redefining `settings` without its PRIMARY KEY broke every upsert.
  `services/restore.py` copies rows into `ensure_schema()` tables under a whitelist
  authorizer (no SELECT/functions/triggers/views/PRAGMA, UPDATE only on
  sqlite_master). Re-derive the whitelist from a real export if iterdump changes:
  log authorizer actions while restoring (see the review's probe script pattern).
- **2026-11-08 — Size backup caps from the live DB, not a guess.** The live export was
  already 15.9 MB against a 20 MB upload cap (≈ +3 MB/month toward 365-day
  retention). Check `zip size / cap` each review; restore must stay streaming (Pi
  3 = 921 MB RAM, no swap; measured 65 s / 64 MB RSS for 50 MB of SQL).
- **2026-11-08 — lighttpd size directives are in KB.** `server.max-request-size`
  counts kilobytes; verify units for every proxy limit against the lighttpd docs.
- **2026-11-08 — requirements.txt must equal the Pi freeze.** Three transitive pins
  (annotated-doc, pydantic-core, typing-inspection) were missing. Check: fresh venv
  from requirements.txt, `pip freeze` == the pin list (modulo `_`/`-`).
- **2026-11-08 — Contrast across ALL themes, not the one Lighthouse saw.** Lighthouse
  only audits the active theme; compute WCAG ratios for every `[data-theme]` token
  pair a component uses (state variants too — the pill's error fill failed in 13/15
  themes while the audited state passed).

