# Tests

```bash
# from the repo root, in the venv that has the runtime deps
pip install -r requirements-dev.txt
pytest tests/ -v
```

- **test_unit.py** — pure-logic helpers (haversine, SSRF validator, aircraft
  categorisation, dashboard summary maths, AI prompt sanitisers, `Aircraft.to_json`
  shape). No server, no DB.
- **test_api.py** — FastAPI `TestClient` over the routes: a smoke sweep of the
  parameter-free GETs, response-shape checks for the dashboard summary, the
  settings whitelist + secret redaction, and the Pydantic request-validation
  paths (422 on missing `hex` / over-long follow-up question).

`conftest.py` points the settings store at a throwaway temp DB and builds the
TestClient without the lifespan context manager, so the feed poll loop / digest
scheduler never start and no external services are hit.

On the production Pi these run against `/opt/piscope/venv` (`pip install pytest`
there), staged in a temp dir so they never touch the live `piscope.db`.

When staging onto the Pi, copy **all four** of `app/ static/ tests/ tools/` — a
couple of tests read repo files directly (`tools/claude-shim/shim.py` for the
no-`--bare` regression guard, `static/app.js` for the badge-escaping check), so a
`app/ static/ tests/`-only stage fails them with `FileNotFoundError`.
