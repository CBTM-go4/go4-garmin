# Garmin Coach

A local running coach & health dashboard. It reads your Garmin Connect data
**through the Garmin MCP server** and turns it into training-science metrics and
plain-language coaching.

Everything runs on localhost. Your data and credentials never leave your machine.

```
Garmin Connect ── MCP ──▶ FastAPI backend ──▶ SQLite cache ──▶ Dashboard (localhost:8765)
   (Taxuspt/garmin_mcp)      (metrics + coach)                    Claude Code ◀── same MCP
```

## What you'll see

- **Coach headline + next session** — a recommendation that factors in both your
  training load *and* recovery (sleep, HRV, Body Battery), so a fresh TSB won't tell
  you to smash intervals on a wrecked night's sleep.
- **Training Load & Form** — CTL / ATL / TSB over time, with a click-to-toggle legend.
- **Injury-risk ACWR**, weekly/monthly volume, and an 80/20 intensity mix.
- **Recovery** — last night's sleep (score + stages), HRV vs baseline, Body Battery.
- **Fitness Trend** — your VO₂max trajectory (backfilled from history) and race-time
  predictions accumulated over time.
- **Race Predictions from *your own runs*** — Daniels VDOT anchored on your real
  efforts at each distance, an HR-based estimate, and Garmin's own number side by side
  (Garmin's tends optimistic; this shows a realistic range).
- **Recent Runs** table with per-run aerobic **decoupling** (colour-coded) and
  **temperature**; click any run for splits, HR zones, decoupling detail and weather.

## Architecture

| Piece | What it does |
|-------|--------------|
| **Garmin MCP** ([Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)) | Data layer. 110+ tools over Garmin Connect. Launched automatically as a stdio subprocess. |
| `server/mcp_client.py` | MCP client — one long-lived session in a worker task; the *only* path to Garmin. |
| `server/metrics.py` | Training load (Banister TRIMP → CTL/ATL/TSB), ACWR, mileage, aerobic efficiency, HR-zone & decoupling math, VDOT/Riegel race prediction, sleep/HRV/weather normalisers. |
| `server/coach.py` | Transparent rule-based coach: insights + a recovery-aware next-session recommendation. |
| `server/app.py` | FastAPI JSON API + serves the dashboard. |
| `server/cache.py` | SQLite TTL cache (Garmin rate-limits hard — HTTP 429). |
| `server/history.py` | Persists VO₂max + race-prediction snapshots for the fitness trend. |
| `web/` | Dependency-free dashboard: hand-rolled SVG charts, light/dark, hover tooltips. |

> **Auth note (2026):** In March 2026 Garmin added Cloudflare TLS fingerprinting
> that broke the old `garth`-based login. This project pins
> `garminconnect >= 0.3.6`, whose native `curl_cffi` engine works around it.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
```

### 1. Try it now (no Garmin account)

```powershell
.\scripts\run.ps1 -Demo          # Windows
./scripts/run.sh --demo          # macOS / Linux
```
Open <http://127.0.0.1:8765>. The dashboard runs on realistic synthetic data.

### 2. Connect your real Garmin data

Authenticate once (handles MFA; saves a token to `~/.garminconnect`):

```powershell
.\scripts\run.ps1 -Auth
```
```bash
./scripts/run.sh --auth
# or directly:
uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
```

Then start the app (it launches the MCP for you):

```powershell
.\scripts\run.ps1
```

The token lasts ~6 months. Re-auth with `garmin-mcp-auth --force-reauth`.

## Conversational coaching in Claude Code

The same Garmin MCP is wired into Claude Code via [`.mcp.json`](.mcp.json).
Open Claude Code in this folder, approve the `garmin` server, and ask things like:

- *"Analyse my long run yesterday — was I aerobically decoupled?"*
- *"Given my last 4 weeks, am I ramping too fast?"*
- *"Plan my next 7 days around Saturday's race."*

(If it hasn't picked up the config, run `claude mcp add garmin -- uvx --python 3.12
--from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp`.)

## API

| Endpoint | Returns |
|----------|---------|
| `GET /api/status` | auth / demo state, MCP tool list |
| `GET /api/overview?days=90` | runs + all metrics + recovery + predictions + fitness trend + coach output |
| `GET /api/run/{id}` | activity detail, splits, decoupling, HR-zone distribution, weather |
| `GET /api/run/{id}/summary` | lightweight decoupling % + temperature (used to fill the runs table) |
| `POST /api/refresh` | clear the cache |

## Tuning

Set `GARMIN_COACH_HR_MAX` / `GARMIN_COACH_HR_REST` in `.env` for accurate load &
zones (otherwise HR max is inferred from your data, rest defaults to 48). See
[`.env.example`](.env.example).

## Notes & limitations

- The **Intensity Mix** is *estimated from each run's average HR* (cheap, no extra
  API calls) — a run's per-second zone breakdown is only fetched on the single-run
  view. It's directional, not exact.
- **Race predictions** are only as good as your data: the "Realistic" model anchors on
  your best *actual* effort at each distance, so if you only run easy it reflects that.
  Distances longer than your longest run are extrapolated and flagged. Log a hard
  effort (parkrun / time-trial) and the estimate sharpens immediately.
- **Recovery** panels (sleep, HRV, Body Battery) and VO₂max depend on your device
  recording them — some Garmin watches don't, in which case those tiles are hidden.
- **Temperature** comes from Garmin's weather service, which returns °F despite its
  field name; the app converts to °C.
- The **fitness trend** starts sparse and fills in: VO₂max history is backfilled once
  in the background, and race-prediction points accumulate a day at a time.
- Coaching is rule-based and explainable, not medical advice.
