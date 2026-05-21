# claude-shim

A tiny HTTP daemon that wraps `claude -p` so a remote service can use your
Claude Code subscription without itself running Claude Code.

Built for PiScope Radar (and reused by the SOC dashboard) — but the contract
is generic, so any LAN client can call it.

## Why

PiScope's `/api/explain` endpoint can talk to:

1. **Ollama** (local LLM)
2. **Cloud APIs** (Anthropic / OpenAI / Google — bring your own key)
3. **claude-shim** — this — to piggyback on an already-running Claude Code seat

Option 3 is the cheapest path *if* you already pay for Claude Code, since
your existing subscription covers the requests instead of burning API credits.

The shim is intentionally minimal: it doesn't try to be a generic
multi-tenant API. It's one host, one Claude account, one or a few LAN
clients.

## Contract

`POST /generate`

```
Headers: Authorization: Bearer <SHIM_BEARER_TOKEN>   (if configured)
Body:    {"prompt": "...", "num_predict"?: 360, "temperature"?: 0.5}
200:     {"text": "..."}
4xx/5xx: {"detail": "..."}
```

`GET /health`

```
200: {"ok": true, "version": "...", "model": "..."}
```

## Install

Run from this directory on the host where Claude Code is installed
(typically a dev machine, NOT the Pi):

```bash
sudo ./install.sh
```

That:

1. Copies `shim.py` into `/opt/claude-shim/` and builds a venv there.
2. Writes `/etc/claude-shim.env` with a freshly generated bearer token
   (only if the file doesn't already exist).
3. Installs `claude-shim.service` under systemd.

Then:

1. **As the service user** (default: whoever ran sudo), run:
   ```bash
   claude setup-token
   ```
   This is interactive — it provisions a long-lived auth token in
   `~/.claude/`. The shim runs as the same user so it picks up that token.

2. Verify the binary is reachable headlessly:
   ```bash
   claude --bare --print "Say hi in 4 words"
   ```
   If you get a response, the shim will work too.

3. Edit `/etc/claude-shim.env`:
   - `SHIM_ALLOW_IPS=10.0.0.231` — pin the Pi (or your client) IP.
   - `SHIM_CLAUDE_MODEL=claude-haiku-4-5` — optionally pin a cheaper model.

4. Start it:
   ```bash
   sudo systemctl start claude-shim
   curl -sH "Authorization: Bearer $(grep SHIM_BEARER_TOKEN /etc/claude-shim.env | cut -d= -f2)" \
        http://localhost:8090/health
   ```

5. In the PiScope UI → Settings → AI: switch provider to **Claude CLI**,
   paste `http://<dev-host-ip>:8090` and the bearer token, hit *Test*.

## Security

The shim has the operator's full Claude account behind it. Defence in depth:

- **Bearer token** — `SHIM_BEARER_TOKEN` is mandatory in any deployment that
  isn't bound to `127.0.0.1`. Treat it like a password.
- **IP allow-list** — set `SHIM_ALLOW_IPS` to the exact LAN IPs of clients.
- **Bind interface** — `SHIM_BIND_HOST=0.0.0.0` is the default; tighten to
  a single interface if your host is multi-homed.
- **Prompt size cap** — `SHIM_MAX_PROMPT_BYTES` rejects anything over 32 KiB
  by default. Adjust if you start using larger prompts.
- **systemd hardening** — the unit uses `ProtectSystem=strict`,
  `ProtectHome=read-only`, etc. Tweak `ReadWritePaths=` if claude needs to
  write outside `/opt/claude-shim`.

## Troubleshooting

- **`claude binary not on PATH`** in `/health`: systemd doesn't source
  `~/.bashrc`. Set `SHIM_CLAUDE_BIN=/home/<user>/.npm-global/bin/claude`
  (absolute path) in `/etc/claude-shim.env`.
- **`claude exited 1: Not logged in`**: the service user hasn't run
  `claude setup-token` yet, or did so as a different user.
- **`504 timed out`**: cold-start can be slow if the model isn't pinned.
  Bump `SHIM_TIMEOUT_SECONDS` or set `SHIM_CLAUDE_MODEL` to keep the same
  model across calls.

## Uninstall

```bash
sudo systemctl disable --now claude-shim
sudo rm -rf /opt/claude-shim /etc/claude-shim.env /etc/systemd/system/claude-shim.service
sudo systemctl daemon-reload
```
