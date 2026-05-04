# Microgrid Remote Monitor

Live monitoring dashboard for the Mooramoora off-grid microgrid. Polls a Selectronic SP Pro and a Solis 50 kW hybrid inverter, displays a combined dashboard on a Raspberry Pi at the site, and pushes telemetry to a public dashboard at `monitor.mooramoora.org.au`.

## What it monitors

- **Selectronic SP Pro** — battery state of charge, battery power, solar (DC shunt + AC-coupled), grid import/export, load, and lifetime energy totals
- **Solis S6-EH3P 50 kW Hybrid Inverter** — battery SoC, PV string voltages/currents, total PV power, three-phase grid voltages and frequency, battery health, faults, and DC bus voltage

## Architecture

Three tiers, all running on a single git repo. Each tier polls or serves on its own port and the data flows in one direction:

```
   ┌──────────────────────────┐         ┌──────────────────────────┐
   │  rubberduck (Pi at site) │         │  pignus (VPS)            │
   │  192.168.x — LAN         │         │  monitor.mooramoora.org  │
   ├──────────────────────────┤  HTTPS  ├──────────────────────────┤
   │  app.py            :5000 │ ──────► │  server/server_app.py    │
   │  (microgrid-monitor.svc) │ POST    │  :8100 (behind Apache)   │
   │  + data_pusher.py        │ /api/   │                          │
   │                          │  push   │  Public dashboard:       │
   │  Polls:                  │         │  https://monitor.mooram… │
   │   • Solis  192.168.11.214│         │  oora.org.au/sppro/      │
   │   • SP Pro 192.168.11.240│         │                          │
   └──────────────────────────┘         └──────────────────────────┘
            │                                        ▲
            │ Local LAN dashboard:                   │
            ▼                                        │
   http://rubberduck.local:5000 ◄───────────────────┘
   (combined_v2.html)            (also served on the VPS)
```

The same `combined_v2.html` template is served on both rubberduck and the VPS — the only difference is whether the Flask backend is polling Modbus directly (rubberduck) or replaying push payloads from the Pi (VPS).

## Components

| File | What it does |
|------|--------------|
| `app.py` | Flask app that runs on rubberduck. Polls Solis (Modbus TCP), SP Pro, and optionally SwitchDin Stormcloud. Serves `combined_v2.html` on `:5000`. |
| `sppro_reader.py` | SP Pro Modbus TCP reader. Used when the SP Pro Modbus interface is enabled. |
| `switchdin_reader.py` | Pulls SP Pro telemetry via SwitchDin's Stormcloud cloud API. Optional — needs username + password. |
| `eastron_reader.py` | Legacy Eastron SDM630MCT energy-meter reader. Retired in current deployment. |
| `data_pusher.py` | Runs alongside `app.py` on rubberduck. Every 60 s, fetches the local `/api/*/data` endpoints and POSTs them to the VPS at `/api/push`. |
| `server/server_app.py` | Flask app for the VPS. Receives pushes from rubberduck, retains 24 h of history in memory, serves the same `combined_v2.html` dashboard publicly. |
| `simulator.py` | Modbus TCP simulator for offline development. Serves a fake Solis (slave 1) and a fake Eastron (slave 2) on a single port. |
| `templates/combined_v2.html` | The current dashboard. Two-column SP Pro + Solis layout, two 24 h charts, per-device staleness handling, watchdog auto-reload. |
| `install.sh` | Pi setup: venv, deps, systemd unit. |
| `install_pusher.sh` | Pi setup for the `data_pusher.py` service. |
| `server/install_server.sh` | VPS setup (systemd unit + Apache vhost). |

## Quick start — Raspberry Pi (rubberduck)

```bash
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor
bash install.sh
```

Edit `/etc/systemd/system/microgrid-monitor.service` to set the inverter IPs and any SwitchDin credentials, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now microgrid-monitor.service
```

The dashboard is then at `http://rubberduck.local:5000`.

For pushing to the VPS:

```bash
bash install_pusher.sh
sudo systemctl edit microgrid-pusher.service   # set MONITOR_API_KEY and --server-url
sudo systemctl enable --now microgrid-pusher.service
```

## Quick start — VPS (pignus)

```bash
git clone https://github.com/glenmo/microgrid_remote_monitor /opt/microgrid_remote_monitor
cd /opt/microgrid_remote_monitor/server
sudo bash install_server.sh
```

Edit `/etc/systemd/system/microgrid-server.service` to set `MONITOR_API_KEY` (must match the Pi), then start it. The Apache vhost in `server/monitor.mooramoora.org.au.conf` reverse-proxies `/sppro/` to the Flask app on `:8100`.

## Command-line options (`app.py`)

```
  --host             Flask listen address              (default: 0.0.0.0)
  --port             Flask listen port                 (default: 5000)

  Solis (Modbus TCP)
  --solis-ip         Solis inverter IP                 (default: 192.168.11.214)
  --solis-port       Solis Modbus TCP port             (default: 502)
  --solis-id         Solis Modbus slave ID             (default: 1)
  --solis-poll       Solis poll interval (seconds)     (default: 5)
  --no-solis         Disable the Solis reader

  SP Pro (Modbus TCP)
  --sppro-ip         SP Pro IP                         (default: 192.168.11.240)
  --sppro-port       SP Pro Modbus TCP port            (default: 502)
  --sppro-poll       SP Pro poll interval (seconds)    (default: 5)
  --no-sppro         Disable the SP Pro reader

  SwitchDin (Stormcloud cloud API — optional)
  --switchdin-user   SwitchDin login email
  --switchdin-pass   SwitchDin password
  --switchdin-uuid   Unit UUID                         (default set in source)
  --switchdin-poll   Poll interval (seconds)           (default: 60)
  --no-switchdin     Disable the SwitchDin reader

  --debug            Flask debug mode
```

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Combined dashboard (`combined_v2.html`) |
| `GET /api/data` | Latest Solis data (legacy alias) |
| `GET /api/history` | Solis 24 h history (legacy alias) |
| `GET /api/status` | Solis connection status |
| `GET /api/solis/data` | Latest Solis data |
| `GET /api/solis/history` | Solis 24 h history |
| `GET /api/solis/status` | Solis connection status |
| `GET /api/sppro/data` | Latest SP Pro data |
| `GET /api/sppro/history` | SP Pro 24 h history |
| `GET /api/sppro/status` | SP Pro connection status |
| `GET /api/switchdin/data` | SwitchDin cloud data |
| `GET /api/switchdin/history` | SwitchDin 24 h history |
| `GET /api/switchdin/status` | SwitchDin connection status |
| `GET /api/message` | Editable banner text from `message.txt` |
| `POST /api/push` | (VPS only) Receive Pi pushes — requires `X-API-Key` header |

All `/api/*` responses set `Cache-Control: no-store` so neither browsers nor proxies serve stale JSON.

## Dashboard behaviour

The dashboard polls `/api/sppro/{data,status}` and `/api/solis/{data,status}` every 5 s, and `/api/*/history` every 60 s. To survive Chromium-on-Pi quirks it has several layers of self-defence:

- **Cache-busting** — every fetch appends `?_=<timestamp>` and sends `cache: 'no-store'`.
- **Per-device age indicator** — `(Xs ago)` next to each device name in the header, driven by the local clock. Turns orange after 30 s, red after 2 min.
- **Stale-value preservation** — when a field is missing in the latest response, the previously rendered value is kept on screen (dimmed) instead of falling to `0` or `—`. Whole columns dim when the device disconnects.
- **Watchdog auto-reload** — if no successful fetch arrives for 60 s, `location.reload()` fires.
- **Meta-refresh backstop** — `<meta http-equiv="refresh" content="600">` hard-reloads every 10 minutes regardless of JS state.

## Solis register map (Modbus FC 0x04)

| Register | Name | Type | Unit | Scale |
|----------|------|------|------|-------|
| 33000 | Inverter model | U16 | — | 1 |
| 33035 | PV today energy | U16 | kWh | ÷10 |
| 33049–56 | PV1–PV4 V/I | U16 | V/A | ÷10 |
| 33057–58 | PV total power | U32 | W | 1 |
| 33073–75 | Grid V (A-B, B-C, C-A) | U16 | V | ÷10 |
| 33076–78 | Grid I (A, B, C) | U16 | A | ÷10 |
| 33079–80 | Active power (+ export / − import) | S32 | W | 1 |
| 33094 | Grid frequency | U16 | Hz | ÷100 |
| 33133 | Battery voltage | U16 | V | ÷10 |
| 33134 | Battery current | S16 | A | ÷10 |
| 33139 | Battery SoC | U16 | % | 1 |
| 33140 | Battery SoH | U16 | % | 1 |

Full map in `app.py` `REGISTER_MAP`.

## Network setup

- **Solis** — Ethernet on the LAN at 192.168.11.214:502 (Modbus TCP, slave ID 1).
- **SP Pro** — Ethernet on the LAN at 192.168.11.240. The site uses the proprietary Selectronic *selpi* protocol on TCP 10001 with a password; this is what the production `microgrid-monitor.service` ExecStart uses. The `sppro_reader.py` in this repo is a fall-back that uses Modbus TCP on the standard 502.
- **rubberduck** — Raspberry Pi 5 at the site, hostname `rubberduck.local`. Runs `microgrid-monitor.service` and `microgrid-pusher.service`. Old service name `solis-monitor` is retired.
- **pignus** — VPS hosting `monitor.mooramoora.org.au`, runs `microgrid-server.service` behind Apache.

## Local development

```bash
# Terminal 1 — Modbus simulator (fake Solis on slave 1, Eastron on slave 2)
python simulator.py --port 5020

# Terminal 2 — app pointed at the simulator
python app.py --solis-ip 127.0.0.1 --solis-port 5020 --no-sppro --no-switchdin

# Open http://localhost:5000
```

## Editing workflow

The Pi (rubberduck) clones into `/home/glen/microgrid_remote_monitor/` and may carry uncommitted local changes — for example, the production SP Pro reader uses the Selectronic selpi protocol and lives in `sppro_sx_reader.py`, which isn't yet on `main`. The intended flow is:

```
Edit on Mac (Dropbox)  →  git push  →  git pull on Pi  →  systemctl restart
```

Before pulling, stash any Pi-side changes:

```bash
cd ~/microgrid_remote_monitor
git stash && git pull && git stash pop
sudo systemctl restart microgrid-monitor.service
```

## Dependencies

- Python 3.9+
- `flask >= 3.0`
- `pymodbus >= 3.6`
- `requests >= 2.31` (for `data_pusher.py` and `switchdin_reader.py`)
- Chart.js (loaded from CDN by the dashboard)

See `requirements.txt`.

## Useful commands

```bash
# Service control on the Pi
sudo systemctl status microgrid-monitor.service
sudo journalctl -u microgrid-monitor.service -f
sudo systemctl restart microgrid-monitor.service

# Confirm what's listening on :5000
sudo lsof -i :5000

# Sanity-check the API directly
curl -s http://localhost:5000/api/sppro/data | python3 -m json.tool
curl -s http://localhost:5000/api/solis/status
```
