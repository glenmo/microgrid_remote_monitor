# Microgrid Remote Monitor

Live monitoring dashboard for the Mooramoora off-grid microgrid. Polls a
Selectronic SP Pro and a Solis 50 kW hybrid inverter, displays a combined
dashboard on a Raspberry Pi at the site, and pushes telemetry to a public
dashboard at `monitor.mooramoora.org.au`.

**Live URLs**

- `https://monitor.mooramoora.org.au/` — simple battery SOC traffic light (public)
- `https://monitor.mooramoora.org.au/advanced/` — full combined dashboard (public)
- `https://monitor.mooramoora.org.au/flow` — live power-flow diagram (public)
- `https://monitor.mooramoora.org.au/engineer` — engineer view (public)
- `http://rubberduck.local:5000/` — full combined dashboard (LAN, primary);
  also `/flow`, `/engineer` and the `/rotate` kiosk page
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

Three tiers (plus kitty, which feeds the SP Pro into rubberduck), all running
on a single git repo. Each tier polls or serves on its own port and the data
flows in one direction:

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
│  Receives:               │         │      soc_traffic_light)    │
│   • SP Pro via kitty     │         │   /advanced/ → :8100       │
│     /api/sppro/ingest    │         │     (combined_v2.html)     │
└──────────────────────────┘         │   /api/      → :8100       │
      ▲          │                   │     (pushed data + history)│
      │ HTTP     │ LAN combined      └────────────────────────────┘
      │ POST     ▼ dashboard                        ▲
      │   http://rubberduck.local:5000              │
      │                                        Public URLs:
┌─────┴────────────────────┐           https://monitor.mooramoora.org.au/
│  kitty (Pi at site)      │              (simple traffic light)
│  kitty/sppro_pusher.py   │           https://monitor.mooramoora.org.au/advanced/
│  (sppro-pusher.service)  │              (detailed combined dashboard)
│  SP Pro over USB (selpi) │
└──────────────────────────┘

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

The SP Pro is read by **kitty**, a second Pi cabled to the SP Pro's USB
comms board. `kitty/sppro_pusher.py` runs the selpi reader over serial and
POSTs each sample to rubberduck's `/api/sppro/ingest`, and rubberduck
(`--sppro-source push`) serves it like any other reader. See
`kitty/README.md`. SwitchDin no longer monitors the SP Pro. The SwitchDin
reader is still in the code but is legacy.


## Components

| File | What it does |
| --- | --- |
| `app.py` | Flask app that runs on rubberduck. Polls Solis (Modbus TCP) and receives SP Pro data pushed by kitty. Serves `combined_v2.html` on `:5000`. |
| `solis_cloud_reader.py` | SolisCloud API reader — fallback Solis source when local Modbus is unavailable (used automatically when `--solis-cloud-*` credentials are given). Cloud data arrives every ~2–5 min with frequent upload gaps; on start it backfills yesterday's and today's samples from the day-history API. |
| `sppro_reader.py` | SP Pro Modbus TCP reader. Used when the SP Pro Modbus interface is enabled. |
| `sppro_sx_reader.py` | SP Pro selpi-protocol reader. Production reader on kitty (`transport="serial"`, USB). The TCP transport via a serial↔TCP bridge is a legacy fallback. Wraps the vendored `selpi` library in `vendor/selpi/`. |
| `sppro_push_receiver.py` | `SPProPushReceiver` — serves SP Pro data POSTed to `/api/sppro/ingest` (`--sppro-source push`). Production SP Pro source on rubberduck. |
| `kitty/` | `sppro_pusher.py` + `sppro-pusher.service` for kitty, the Pi that reads the SP Pro over USB and pushes to rubberduck. See `kitty/README.md`. |
| `vendor/selpi/` | Vendored Selectronic Sx-protocol library (auth + decode) used by `sppro_sx_reader.py`. Runtime-only copy. |
| `switchdin_reader.py` | **Legacy.** Pulled SP Pro telemetry via SwitchDin's Stormcloud cloud API. SwitchDin no longer monitors the SP Pro. |
| `eastron_reader.py` | Legacy Eastron SDM630MCT energy-meter reader. Retired in current deployment. |
| `data_pusher.py` | Runs alongside `app.py` on rubberduck. Every 60 s, fetches the local `/api/*/data` endpoints and POSTs them to the VPS at `/api/push`. |
| `server/server_app.py` | Flask app for the VPS. Receives pushes from rubberduck, retains 24 h of history in memory, serves the same `combined_v2.html` dashboard publicly. |
| `simulator.py` | Modbus TCP simulator for offline development. Serves a fake Solis (slave 1) and a fake Eastron (slave 2) on a single port. |
| `templates/combined_v2.html` | The current dashboard. Two-column SP Pro + Solis layout with Battery 1 / Battery 2 tiles for the Solis BMS, Site Totals (incl. inferred house solar), two 24 h charts, per-device staleness handling, watchdog auto-reload. Stacks to one column on narrow screens. |
| `templates/flow_diagram.html` | `/flow` — animated power-flow diagram (solar, house solar, generator, both inverters and batteries, microgrid bus, consumption). Wide layout for desktop/kiosk, stacked portrait layout for phones. |
| `templates/engineer.html` | `/engineer` — dense per-inverter readouts, energy ledgers, Solis status/fault registers and 24 h temperature / power-balance / battery I-V charts. |
| `templates/rotate.html` | `/rotate` (rubberduck only) — kiosk page that cycles full-screen iframes. |
| `server/templates/{combined_v2,flow_diagram,engineer}.html` | Symlinks to `../../templates/…`. Tracked as symlinks in git — don't replace with real files. |
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
IPs and the SP Pro source (`--sppro-source push`, optionally
`--sppro-ingest-token`), then:

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


## Quick start — SP Pro reader (kitty)

kitty needs `pyserial` and the SP Pro cabled by USB. Install
`kitty/sppro-pusher.service` into `/etc/systemd/system/`, check
`INGEST_URL`, `SPPRO_SERIAL` and `SPPRO_PASSWORD`, then
`sudo systemctl enable --now sppro-pusher.service`. Full details are in
`kitty/README.md`.


## Quick start — VPS (pignus)

```
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor/server
bash install_server.sh      # not with sudo — the unit runs as $USER; the script sudos where needed
```

This installs `/etc/systemd/system/microgrid-monitor.service` (the same unit
name as on the Pi), running `server/venv/bin/python server_app.py` from
`~/microgrid_remote_monitor/server`. Check `MONITOR_API_KEY` in the unit
matches the Pi's pusher, then start it. The Apache vhost in
`server/monitor.mooramoora.org.au.conf` reverse-proxies `/` to the
traffic-light app on `:8765`, `/advanced/` to the combined dashboard on
`:8100`, and `/api/` to `:8100`.


## Command-line options (`app.py`)

```
--host             Flask listen address              (default: 0.0.0.0)
--port             Flask listen port                 (default: 5000)

Solis (Modbus TCP — production source)
--solis-ip         Solis inverter IP                 (default: 192.168.11.214)
--solis-port       Solis Modbus TCP port             (default: 502)
--solis-id         Solis Modbus slave ID             (default: 1)
--solis-poll       Solis poll interval (seconds)     (default: 5; production: 10)
--no-solis         Disable the Solis reader

Solis (SolisCloud API — fallback; if key-id, key-secret and sn are all
given, the cloud is used INSTEAD of Modbus)
--solis-cloud-key-id      SolisCloud API key ID
--solis-cloud-key-secret  SolisCloud API key secret
--solis-cloud-sn          Inverter serial number
--solis-cloud-id          Inverter ID (optional)
--solis-cloud-poll        Poll interval (seconds)    (default: 60, min 30)

SP Pro
--sppro-source     auto | sx | modbus | push         (default: auto)
                   push = serve data POSTed to /api/sppro/ingest by kitty
                   (production). sx = selpi over a serial<->TCP bridge
                   (legacy). auto = sx if a password is set, else modbus.
--sppro-ingest-token  If set, /api/sppro/ingest requires X-Ingest-Token
--sppro-ip         SP Pro / bridge IP (sx, modbus)   (default: 192.168.11.240)
--sppro-port       SP Pro / bridge TCP port          (default: 502)
--sppro-password   selpi password (REQUIRED for sx)  (default: none)
--sppro-poll       SP Pro poll interval (seconds)    (default: 5)
--no-sppro         Disable the SP Pro reader

SwitchDin (Stormcloud cloud API — LEGACY, SwitchDin no longer monitors the SP Pro)
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
| `GET /flow` | Live power-flow diagram (`flow_diagram.html`) |
| `GET /engineer` | Engineer view (`engineer.html`) |
| `GET /rotate` | Kiosk page: cycles full-screen between `/flow` (10 s) and noisy's EV status page (`http://192.168.55.6:8090/?theme=dark`, 20 s). Override with `?url=A&secs=10&url=B&secs=20` (one `secs` applies to all). rubberduck's kiosk Chromium (`~/.config/labwc/autostart`) opens this page. |
| `GET /api/data` | Latest Solis data (legacy alias) |
| `GET /api/history` | Solis 24 h history (legacy alias) |
| `GET /api/status` | Solis connection status |
| `GET /api/solis/data` | Latest Solis data (both rubberduck and pignus) |
| `GET /api/solis/history` | Solis 24 h history (column format — includes `timestamps`, `battery_soc`, `pv_total_power`, `battery_power`, `active_power`, `grid_frequency`, `pv1_power`..`pv4_power`) |
| `GET /api/solis/status` | Solis connection status |
| `GET /api/sppro/data` | Latest SP Pro data |
| `GET /api/sppro/history` | SP Pro 24 h history |
| `GET /api/sppro/status` | SP Pro connection status |
| `POST /api/sppro/ingest` | (rubberduck, push mode) Receive SP Pro samples from kitty — `X-Ingest-Token` if `--sppro-ingest-token` is set |
| `GET /api/switchdin/data` | (legacy) SwitchDin cloud data |
| `GET /api/switchdin/history` | (legacy) SwitchDin 24 h history |
| `GET /api/switchdin/status` | (legacy) SwitchDin connection status |
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


### House solar (inferred)

The houses on the microgrid have their own rooftop solar, which isn't
metered. It shows up only as a shortfall in the bus balance: the Solis
and SP Pro both report **net** power to the AC bus (charging negative),
so when their sum goes below zero — typically the SP Pro charging harder
than the Solis is exporting — the difference can only be house-solar
surplus flowing directly into the microgrid:

```
solis_to_bus  = inverter_ac_power            (local Modbus, measured)
              | pv_total_power − battery_power(s)   (SolisCloud fallback)
sppro_to_bus  = −battery_w + generator supply (−grid_w when grid_w < −200 W)
house_solar   = max(0, −(solis_to_bus + sppro_to_bus))
```

It's computed client-side (no new API keys) and shown as the **House
Solar** tile on `/flow`, the **House solar now** Site Totals tile on the
dashboard, and on `/engineer` as a live row plus a "House solar
(inferred)" series on the 24 h power-balance chart (each SP Pro history
sample paired with the nearest Solis sample within 2 min). It's blank
unless both inverters are connected, and values under 50 W read as 0.
It only captures surplus that reaches the bus — house solar consumed by
other houses directly is invisible without metering.

### Mobile layouts

- **`/flow`** has two layouts over the same nodes: the original wide one
  (1200×700 design) and a stacked portrait one (560×840). `fitStage()`
  picks whichever scales larger for the viewport, so an upright phone
  gets roughly double the size, while desktops, the kiosk and sideways
  phones keep the wide layout. Short screens (< 500 px tall) get a
  compact header and legend.
- **Dashboard** (`combined_v2.html`) stacks the SP Pro and Solis columns
  below 900 px; below 600 px the header stacks, each SoC gauge sits
  above its power-flow card and Site Totals wraps to two per row.
- **`/engineer`** is one column below 860 px; below 600 px card notes
  drop to their own line, charts are taller with smaller legends and
  6-hourly time ticks (chosen at page load).
- **`/rotate`** just gives each page the full viewport, so `/flow`
  picks its portrait layout on a phone. noisy's EV page isn't in this
  repo and is LAN-only.

Headless Chromium won't make a viewport narrower than 500 px, so to
check a true phone width, load the page in a 390 px-wide `<iframe>`.


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

- **Solis** — read locally over Modbus TCP at 192.168.11.214:502 (slave
  ID 1), polled every 10 s by rubberduck. The Modbus server is the Solis
  **S2-WL-ST datalogger stick** (S/N 7A124B120CB0700E, firmware
  100141D9 type WL — updated 2026-09-27) plugged into the
  inverter's COM port — specifically its **Ethernet port** (MAC
  `ec:c9:ff:97:47:d8`). The same stick uploads the SolisCloud data. If its
  Ethernet cable is unplugged, .214 disappears ("No route to host") and
  Solis data stops — fall back to SolisCloud with the `--solis-cloud-*`
  args if needed. The stick also services its own cloud uploads, so
  occasional late replies (`transaction_id` mismatch in the log) are
  expected; the reader reconnects automatically. (192.168.11.81 is an
  unidentified ESP32 device that accepts TCP on 502 but never answers
  Modbus — not the Solis.)
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
- **pignus** — VPS `pignus.arachnoid.net.au` (110.173.134.67) hosting
  `monitor.mooramoora.org.au`. Repo at `/home/glen/microgrid_remote_monitor`;
  runs `microgrid-monitor.service` (server_app on :8100, from `server/`) and
  `soc-traffic-light.service` (:8765, from `~/soc_traffic_light`) behind
  Apache. SSH is guarded by fail2ban — repeated failed connections (e.g.
  unknown host key under `BatchMode`) ban the client IP; clear with
  `sudo fail2ban-client set sshd unbanip <ip>`.


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
and may carry uncommitted local changes. The intended flow is:

```
Edit on Mac (Dropbox)  →  git push  →  git pull on Pi  →  systemctl restart

```

Before pulling, stash any Pi-side changes:

```
cd ~/microgrid_remote_monitor
git stash && git pull && git stash pop
sudo systemctl restart microgrid-monitor.service
```

The VPS is deployed the same way — same repo path and unit name:

```
ssh pignus 'cd ~/microgrid_remote_monitor && git pull --ff-only && sudo systemctl restart microgrid-monitor.service'
```

Changes to shared templates or API shapes need both hosts updated together:
pignus serves the same `combined_v2.html` from its own checkout.


## Operational notes / known issues

A few things to remember if SP Pro data goes silent:

### selpi "Attempted to start multiple logins" is misleading

This `ValidationException` (raised in `vendor/selpi/memory/protocol.py`)
does **not** mean a second client holds the session. It fires when a query
gets **zero bytes back**, so `query()` retries via `login()`, whose own
challenge-query also gets nothing, tripping a re-entrancy guard. So the
real meaning is **"the SP Pro returned no valid Sx data."** Don't chase
password variants when you see it — check the serial link instead. A
*passing* login that then says `Login failed` (status ≠ 1) is the genuine
wrong-password signal.

### SP Pro data reads require an authenticated session

Data-region reads (e.g. `0xa028`) are silently ignored until selpi has
logged in; only the login region (`0x1f0000`) answers without auth. The
correct selpi password is the SP Pro's **Sx access password** (the
Selectronic Sx default, as used in the upstream selpi tests) — *not*
`selectronic`. The unit ran the wrong password for months and only worked
because a **USB SP-LINK PC held an authenticated session** that selpi rode
on; unplugging SP-LINK exposed the bad password and SP Pro data stopped.
Pass the real password via `--sppro-password` in the systemd unit. To
verify a candidate password live: `./venv/bin/python selpi_probe.py
--password "..."`.

### Lantronix xDirect bridge (single TCP client)

The SP Pro reaches the LAN through a Lantronix xDirect232 serial bridge
(57600 8N1, no flow control) on TCP `10001`. It allows **one TCP client
at a time** and needs a few seconds to release the slot between
connections — back-to-back connects get `Connection refused`. If reads
stall after a power blip, confirm the bridge's serial settings still match
the SP Pro, or power-cycle the xDirect to clear a wedged serial buffer.

### SwitchDin Droplet steals the selpi socket

The Selectronic SP Pro's selpi protocol on TCP `10001` allows **only one
client at a time**. If the SwitchDin Droplet (or any other selpi consumer)
is plugged in, it will hold the socket exclusively and rubberduck's
`sppro_sx_reader.py` will fail with `ConnectionRefusedError: [Errno 111]
Connection refused` even though `ping` to the SP Pro succeeds.

This only affects the legacy `sx` (TCP) path. If
`curl http://localhost:5000/api/sppro/data` returns `{}` in that mode,
check whether the Droplet (or another reader) is on the LAN and leave it
unplugged. SwitchDin no longer monitors the SP Pro, and production reads it
over USB from kitty instead.

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
Don't replace it with a real file copy. The same applies to
`flow_diagram.html` and `engineer.html`.


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
- `requests >= 2.31` (for `data_pusher.py`, `kitty/sppro_pusher.py` and the legacy `switchdin_reader.py`)
- `pyserial` (kitty only, for the selpi USB transport)
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
