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
heatmap, webhooks, replay, PWA install. No auth — LAN-only by design. Currently
`VERSION = "1.5.0"` (see `app/main.py`), tagged `v1.5.0`.

## Architecture & tech stack

- **Backend** — Python 3.9+, FastAPI + asyncio + httpx + uvicorn, SQLite (WAL mode).
  Polls a tar1090 instance every 2 s, fans out via WebSocket, enriches via hexdb /
  adsbdb / planespotters / FlightAware.
- **Frontend** — vanilla HTML/CSS/JS, no framework. Leaflet 1.9 + Leaflet.heat,
  canvas radar-sweep overlay (`static/radar.js`), service worker (`static/sw.js`)
  for PWA + offline shell caching.
- **Storage** — single SQLite file at `/opt/piscope/piscope.db` on the Pi (live data,
  never overwrite via rsync). Tables: settings, events, daily_stats, feed_snapshots,
  aircraft_notes, seen_types, polar_coverage, position_heatmap, bookmarks, records,
  fa_budget. Migrations live in `app/services/settings.py:_migrate` keyed off
  `PRAGMA user_version`.
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
    digest.py              # Daily digest scheduler + AI commentary
    events.py              # Event log (military/emergency/watchlist/rare)
    insights.py            # Polar coverage, heatmap, leaderboard, notes
    records.py             # All-time records + bookmarks
    webhooks.py            # Discord/Slack/ntfy/generic fan-out
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
  index.html               # Single-page shell
  app.js / app.css         # All client logic + styles
  radar.js                 # Canvas radar-sweep overlay
  sw.js                    # Service worker — CACHE auto-derived on the wire (iter 9.4)
  themes.css               # 11 themes
  data/airports.json       # Bundled airport overlay
docs/screenshots/          # README screenshots
tools/
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

3. **Pip can't reach pypi.** The egress proxy whitelists github.com but not pypi.
   Don't reach for pip on this host — either rewrite to stdlib (claude-shim) or
   use the Pi's `/opt/piscope/venv` over SSH for verification.

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

## Open items / future work

- **claude-shim is ad-hoc, not systemd-managed.** Currently runs as a backgrounded
  Python process under `dev` on the dev host (port 8090, token in
  `/tmp/claude-shim-token`). Survives until reboot. To finish: run
  `sudo /home/dev/projects/piscope-radar/tools/claude-shim/install.sh` on the dev host
  — installs the systemd unit and writes `/etc/claude-shim.env` with its own
  generated token (you'd then need to point PiScope at the new token).
- **Generic `unavailable` error envelopes.** Both `/api/explain` and
  `/api/explain/followup` collapse upstream errors into `"no response from <provider>"`.
  Plumbing the shim's `error` field (and the cloud API vendor's error message)
  through to the frontend would make debugging much easier — known polish opportunity.
- **GitHub Release page for v1.4.0+ is deferred** until a larger milestone. Latest
  tag is still `v1.4.0` even though `main` is at v1.8.0.
- **Co-authored-by footer convention** isn't established in this repo. If you start
  adding it, document it here and in project memory.
