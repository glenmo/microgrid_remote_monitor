# Microgrid Remote Monitor

Live monitoring dashboard for the Mooramoora off-grid microgrid. Polls a
Selectronic SP Pro and a Solis 50 kW hybrid inverter, displays a combined
dashboard on a Raspberry Pi at the site, and pushes telemetry to a public
dashboard at `monitor.mooramoora.org.au`.

**Live URLs**

- `https://monitor.mooramoora.org.au/` — simple battery SOC traffic light (public)
- `https://monitor.mooramoora.org.au/advanced/` — full combined dashboard (public)
- `http://rubberduck.local:5000/` — full combined dashboard (LAN, primary)
- `http://desky.local:8765/` — simple battery SOC traffic light (LAN)

## What it monitors

- **Selectronic SP Pro** — battery state of charge, battery power, solar
  (DC shunt + AC-coupled), grid import/export, load, and lifetime energy
  totals
- **Solis S6-EH3P 50 kW Hybrid Inverter** — battery SoC for both packs
  (`battery_soc` for BMS 1, `bms2_battery_soc` for BMS 2), per-pack
  power/voltage/current, PV string voltages/currents, total PV power,
  three-phase grid voltages and frequency, battery health, faults, and DC
  bus voltage


## Architecture

Three tiers, all running on a single git repo. Each tier polls or serves
on its own port and the data flows in one direction:

```
┌──────────────────────────┐         ┌────────────────────────────┐
│  rubberduck (Pi at site) │         │  pignus (VPS)              │
│  http://rubberduck.local │         │  monitor.mooramoora.org.au │
├──────────────────────────┤  HTTPS  ├────────────────────────────┤
│  app.py             :5000│ ──────► │  server/server_app.py      │
│  (microgrid-monitor.svc) │ POST    │  :8100 (behind Apache)     │
│  + data_pusher.py        │ /api/   │                            │
│                          │  push   │  Apache routing:           │
│  Polls:                  │         │   /          → :8765       │
│   • Solis  192.168.11.214│         │     (traffic light — see   │
│   • SP Pro 192.168.11.240│         │      soc_traffic_light)    │
│      (selpi, TCP 10001)  │         │   /advanced/ → :8100       │
└──────────────────────────┘         │     (combined_v2.html)     │
         │                           │   /api/      → :8100       │
         │ LAN combined dashboard:   │     (pushed data + history)│
         ▼                           └────────────────────────────┘
http://rubberduck.local:5000                        ▲
                                                    │
                                       Public URLs:
                                       https://monitor.mooramoora.org.au/
                                          (simple traffic light)
                                       https://monitor.mooramoora.org.au/advanced/
                                          (detailed combined dashboard)

┌──────────────────────────┐
│  desky.local (LAN)       │
│  http://desky.local:8765 │
│   (simple traffic light, │
│    fetches from          │
│    rubberduck:5000)      │
└──────────────────────────┘
```

The public URL at the root now serves the simple traffic-light view
(green/orange/red battery SOC indicator) from the
[soc_traffic_light](https://github.com/glenmo/soc_traffic_light) repo. The
detailed combined dashboard moved to `/advanced/`. The traffic-light
service also runs on `desky.local` for the LAN.

The same `combined_v2.html` template is served on both rubberduck and the
VPS — the only difference is whether the Flask backend is polling Modbus
directly (rubberduck) or replaying push payloads from the Pi (VPS).


## Components

| File | What it does |
| --- | --- |
| `app.py` | Flask app that runs on rubberduck. Polls Solis (Modbus TCP), SP Pro, and optionally SwitchDin Stormcloud. Serves `combined_v2.html` on `:5000`. |
| `sppro_reader.py` | SP Pro Modbus TCP reader. Used when the SP Pro Modbus interface is enabled. |
| `sppro_sx_reader.py` | SP Pro selpi-protocol reader (TCP 10001 with password). Production reader on rubberduck. Local-only, not yet committed to `main`. |
| `switchdin_reader.py` | Pulls SP Pro telemetry via SwitchDin's Stormcloud cloud API. Optional — needs username + password. |
| `eastron_reader.py` | Legacy Eastron SDM630MCT energy-meter reader. Retired in current deployment. |
| `data_pusher.py` | Runs alongside `app.py` on rubberduck. Every 60 s, fetches the local `/api/*/data` endpoints and POSTs them to the VPS at `/api/push`. |
| `server/server_app.py` | Flask app for the VPS. Receives pushes from rubberduck, retains 24 h of history in memory, serves the same `combined_v2.html` dashboard publicly. |
| `simulator.py` | Modbus TCP simulator for offline development. Serves a fake Solis (slave 1) and a fake Eastron (slave 2) on a single port. |
| `templates/combined_v2.html` | The current dashboard. Two-column SP Pro + Solis layout with Battery 1 / Battery 2 tiles for the Solis BMS, two 24 h charts, per-device staleness handling, watchdog auto-reload. |
| `server/templates/combined_v2.html` | Symlink to `../../templates/combined_v2.html`. Tracked as a symlink in git — don't replace with a real file. |
| `install.sh` | Pi setup: venv, deps, systemd unit. |
| `install_pusher.sh` | Pi setup for the `data_pusher.py` service. |
| `server/install_server.sh` | VPS setup (systemd unit + Apache vhost). |


## Quick start — Raspberry Pi (rubberduck)

```
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor
bash install.sh
```

Edit `/etc/systemd/system/microgrid-monitor.service` to set the inverter
IPs and any SwitchDin credentials, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now microgrid-monitor.service
```

The dashboard is then at `http://rubberduck.local:5000`.

For pushing to the VPS:

```
bash install_pusher.sh
sudo systemctl edit microgrid-pusher.service   # set MONITOR_API_KEY and --server-url
sudo systemctl enable --now microgrid-pusher.service
```


## Quick start — VPS (pignus)

```
git clone https://github.com/glenmo/microgrid_remote_monitor /opt/microgrid_remote_monitor
cd /opt/microgrid_remote_monitor/server
sudo bash install_server.sh
```

Edit `/etc/systemd/system/microgrid-server.service` to set
`MONITOR_API_KEY` (must match the Pi), then start it. The Apache vhost in
`server/monitor.mooramoora.org.au.conf` reverse-proxies `/` to the
traffic-light app on `:8765`, `/advanced/` to the combined dashboard on
`:8100`, and `/api/` to `:8100`.


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

SP Pro (selpi or Modbus TCP)
--sppro-ip         SP Pro IP                         (default: 192.168.11.240)
--sppro-port       SP Pro selpi TCP port             (default: 10001)
--sppro-password   selpi password                    (default: selectronic)
--sppro-poll       SP Pro poll interval (seconds)    (default: 10)
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
| --- | --- |
| `GET /` | Combined dashboard (`combined_v2.html`) |
| `GET /api/data` | Latest Solis data (legacy alias) |
| `GET /api/history` | Solis 24 h history (legacy alias) |
| `GET /api/status` | Solis connection status |
| `GET /api/solis/data` | Latest Solis data (both rubberduck and pignus) |
| `GET /api/solis/history` | Solis 24 h history (column format — includes `timestamps`, `battery_soc`, `pv_total_power`, `battery_power`, `active_power`, `grid_frequency`, `pv1_power`..`pv4_power`) |
| `GET /api/solis/status` | Solis connection status |
| `GET /api/sppro/data` | Latest SP Pro data |
| `GET /api/sppro/history` | SP Pro 24 h history |
| `GET /api/sppro/status` | SP Pro connection status |
| `GET /api/switchdin/data` | SwitchDin cloud data |
| `GET /api/switchdin/history` | SwitchDin 24 h history |
| `GET /api/switchdin/status` | SwitchDin connection status |
| `GET /api/message` | Editable banner text from `message.txt` |
| `POST /api/push` | (VPS only) Receive Pi pushes — requires `X-API-Key` header |


Cache-busting is done client-side: every fetch appends
`?_=<Date.now()>` and sends `cache: 'no-store'`. The Flask endpoints
themselves don't set `Cache-Control` headers — defeating browser cache
from the request side has been sufficient.


## Dashboard behaviour

The dashboard polls `/api/sppro/{data,status}` and `/api/solis/{data,status}`
every 5 s, and `/api/*/history` every 60 s. To survive Chromium-on-Pi
quirks and Flask-JSON caching it has several layers of self-defence:

- **Cache-busting** — every fetch goes through `noCacheFetch()`, which
  appends `?_=<timestamp>` and sets `cache: 'no-store'`.
- **Global staleness indicator** — the header shows
  `Last: HH:MM:SS · Xs ago` driven by the latest of SP Pro's and
  Solis's server-side `last_read` timestamps. The "Xs ago" suffix is
  recomputed every 1 s from `Date.now()` so staleness is visible even
  between fetches. Goes orange at 30 s, red at 60 s.
- **Stale-value preservation** — `self.data.update(new_data)` is used
  (not `self.data = new_data`), so a single failed Modbus batch on
  the server doesn't wipe the dashboard. Previous values stay visible
  until a fresh read replaces them.
- **Meta-refresh backstop** — `<meta http-equiv="refresh" content="600">`
  hard-reloads every 10 minutes regardless of JS state.


## Solis register map (Modbus FC 0x04)

| Register | Name | Type | Unit | Scale |
| --- | --- | --- | --- | --- |
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
| 33139 | Battery SoC (BMS 1) | U16 | % | 1 |
| 33140 | Battery SoH | U16 | % | 1 |

Full map in `app.py` `REGISTER_MAP`. BMS 2 fields (`bms2_battery_soc`,
`battery2_voltage`, `battery2_current`, `battery2_power`) are polled by
the local production reader on rubberduck.


## Network setup

- **Solis** — Ethernet on the LAN at 192.168.11.214:502 (Modbus TCP,
  slave ID 1).
- **SP Pro** — Ethernet on the LAN at 192.168.11.240. The site uses the
  proprietary Selectronic *selpi* protocol on TCP 10001 with a password;
  this is what the production `microgrid-monitor.service` ExecStart uses.
  The `sppro_reader.py` in this repo is a fall-back that uses Modbus TCP
  on the standard 502.
- **rubberduck** — Raspberry Pi 5 at the site, hostname
  `rubberduck.local`. Runs `microgrid-monitor.service` and
  `microgrid-pusher.service`.
- **desky** — Linux box on the LAN, hostname `desky.local`. Runs
  `soc-traffic-light.service` on port 8765.
- **pignus** — VPS hosting `monitor.mooramoora.org.au`, runs
  `microgrid-monitor.service` (server_app on :8100) and
  `soc-traffic-light.service` (:8765) behind Apache.


## Local development

```
# Terminal 1 — Modbus simulator (fake Solis on slave 1, Eastron on slave 2)
python simulator.py --port 5020

# Terminal 2 — app pointed at the simulator
python app.py --solis-ip 127.0.0.1 --solis-port 5020 --no-sppro --no-switchdin

# Open http://localhost:5000
```


## Editing workflow

The Pi (rubberduck) clones into `/home/glen/microgrid_remote_monitor/`
and may carry uncommitted local changes — for example, the production
SP Pro reader uses the Selectronic selpi protocol and lives in
`sppro_sx_reader.py`, which isn't yet on `main`. The intended flow is:

```
Edit on Mac (Dropbox)  →  git push  →  git pull on Pi  →  systemctl restart

```

Before pulling, stash any Pi-side changes:

```
cd ~/microgrid_remote_monitor
git stash && git pull && git stash pop
sudo systemctl restart microgrid-monitor.service
```


## Operational notes / known issues

A few things to remember if SP Pro data goes silent:

### SwitchDin Droplet steals the selpi socket

The Selectronic SP Pro's selpi protocol on TCP `10001` allows **only one
client at a time**. If the SwitchDin Droplet (or any other selpi consumer)
is plugged in, it will hold the socket exclusively and rubberduck's
`sppro_sx_reader.py` will fail with `ConnectionRefusedError: [Errno 111]
Connection refused` even though `ping` to the SP Pro succeeds.

If `curl http://localhost:5000/api/sppro/data` returns `{}`, the first
thing to check is whether the Droplet (or another reader) is on the LAN.
Leave the Droplet unplugged, or — if you need its features back — switch
this app to the SwitchDin Stormcloud cloud API (`switchdin_reader.py`)
instead of direct selpi.

### SP Pro reader exits the whole app on disconnect

When the SP Pro reader loses its selpi connection mid-stream (e.g. after
a brief network blip), the current code logs `Disconnected from inverter`
+ `forcing reconnect (stop())` and the Flask process exits with
`status=1`. systemd restarts it 10 s later, but if anything is still
holding port `5000` (an orphaned earlier instance, or a manual
`python app.py` test), the restart loops indefinitely with
`Address already in use`.

Workaround: `sudo lsof -i :5000` and kill stray python processes before
restarting.

Long-term fix: the reader's stop() handler should catch the disconnect
inside the thread and reconnect, rather than letting the exception
propagate to main.

### Two copies of `combined_v2.html`

The dashboard template lives at both `templates/combined_v2.html` and
`server/templates/combined_v2.html` because `app.py` (rubberduck) and
`server_app.py` (pignus) look in different folders. To prevent silent
divergence, `server/templates/combined_v2.html` is now a symlink to
`../../templates/combined_v2.html` and is tracked as a symlink in git.
Don't replace it with a real file copy.


## Solis reader reliability

The Solis Modbus stack on the H3 has two failure modes that are easy to
hit and slow to recover from. Both are now handled inside `app.py`:

- **`transaction_id` desync.** When the inverter responds late to a
  timed-out request, pymodbus matches the late response against the
  next request's ID and returns an `isError()`. Without intervention
  the socket stays poisoned forever — every subsequent read fails the
  same way. The reader now sets `self.connected = False` whenever
  `result.isError()` fires, and the next call to
  `_read_registers_batch()` forces a fresh connect (closing the old
  client first to avoid socket leaks).

- **Slow polls hiding the watchdog.** Reading ~50 single-register
  Modbus frames at ~700 ms each meant a single `poll_once()` took
  ~30 s — and during that window the `_poll_loop()` couldn't reach
  the watchdog check. Two changes solved this:
  - Adjacent registers are pre-grouped into ~4 batches of up to 50
    registers each (`_build_batches()`), so each `poll_once()` issues
    ~4 Modbus frames instead of ~50, completing in ~3 s. The Solis
    spec's recommended 300 ms gap between frames is honoured.
  - Per-register loop bails on the first failed read (`break`) rather
    than chaining ~50 timeouts. The next 5 s poll cycle reconnects
    cleanly.

- **Staleness watchdog.** Inside `_poll_loop()`, if `last_read_time`
  hasn't advanced for `max(30s, 3 × poll_interval)` and the cooldown
  has elapsed, the reader forces a `disconnect() → connect()`. This
  catches silent failure modes that survive the per-read error
  handling.

- **Heartbeat log.** Every 30 s the reader logs
  `Solis heartbeat: connected=…, last_read_age=…, total_reads=…,
  read_errors=…`. Tail with `journalctl -u microgrid-monitor.service -f`
  to confirm the poll thread is alive and reading.

The same hardening (batched-where-applicable, isError-triggered
reconnect, watchdog, heartbeat) is applied to `sppro_reader.py`.


## Dependencies

- Python 3.9+
- `flask >= 3.0`
- `pymodbus >= 3.6`
- `requests >= 2.31` (for `data_pusher.py` and `switchdin_reader.py`)
- Chart.js (loaded from CDN by the dashboard)


See `requirements.txt`.


## Useful commands

```
# Service control on the Pi
sudo systemctl status microgrid-monitor.service
sudo journalctl -u microgrid-monitor.service -f
sudo systemctl restart microgrid-monitor.service

# Confirm what's listening on :5000
sudo lsof -i :5000

# Sanity-check the API directly
curl -s http://localhost:5000/api/sppro/data | python3 -m json.tool
curl -s http://localhost:5000/api/solis/status

# Public mirror
curl -s https://monitor.mooramoora.org.au/api/sppro/data | python3 -m json.tool
curl -sI https://monitor.mooramoora.org.au/advanced/
```


## License

GPL-2.0
