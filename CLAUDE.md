# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the detailed operator doc (register map, API table, CLI flags, network/host layout, known issues). Read it before touching reader or deployment code. This file covers what you need to navigate the code.

## Commands

No build step, linter, or top-level test suite. Plain Python 3 + Flask, deps in `requirements.txt` (`flask`, `pymodbus`, `requests`).

```bash
# Offline dev: fake Solis (slave 1) + Eastron (slave 2) Modbus server
python simulator.py --port 5020
python app.py --solis-ip 127.0.0.1 --solis-port 5020 --no-sppro --no-switchdin   # http://localhost:5000

# Vendored selpi tests (unittest; needs pyserial installed)
cd vendor/selpi && python -m unittest discover -s tests -t .
cd vendor/selpi && python -m unittest tests.test_crc            # single module

# Check an SP Pro selpi password against real hardware
./venv/bin/python selpi_probe.py --password "..."
```

Deployment happens by `git pull` + `systemctl restart` on the hosts (rubberduck, kitty, pignus). There's no CI. Verify with `curl` against the `/api/*` endpoints (see README "Useful commands").

## Architecture

Data flows one way: **devices → readers (rubberduck `app.py`) → dashboard + `data_pusher.py` → VPS `server/server_app.py`**.

- **`app.py` (rubberduck, :5000)** is the hub. `main()` builds one reader object per device and starts it, then Flask serves `/api/<device>/{data,history,status}` straight from those objects. Pages: `/` (`combined_v2.html`), `/flow` (`flow_diagram.html`), `/engineer` (`engineer.html`).
- **Reader contract**: every reader (`SolisModbusReader` in `app.py`, `SolisCloudReader`, `SPProSxReader`, `SPProModbusReader`, `SPProPushReceiver`, `SwitchDinReader`, `EastronModbusReader`) exposes the same `start/stop/get_data/get_history/get_status` interface. Each runs its own poll thread and keeps a bounded in-memory history. You can swap sources without touching routes or templates. New sources must follow this interface.
- **Source selection** happens in `app.py main()` from CLI flags:
  - Solis: SolisCloud API if `--solis-cloud-key-id/-secret/-sn` are all given, otherwise local Modbus TCP.
  - SP Pro: `--sppro-source {auto,sx,modbus,push}`. `push` is the current production path. `sx` uses selpi over the Lantronix TCP bridge (legacy fallback). `auto` picks `sx` if a password is set, otherwise `modbus`.
  - Solis and Eastron can share one `ModbusTcpClient` plus lock when they're on the same gateway.
- **kitty path (SP Pro over USB)**: `kitty/sppro_pusher.py` runs on a separate Pi cabled by USB to the SP Pro. It reuses `SPProSxReader(transport="serial")` and POSTs `{"data":…, "status":…}` to rubberduck `/api/sppro/ingest`, which feeds `SPProPushReceiver` (optional `X-Ingest-Token`). See `kitty/README.md` for why a TCP serial bridge was abandoned.
- **selpi**: `vendor/selpi/` is a vendored copy of the Selectronic Sx-protocol library. `sppro_sx_reader.py` adds it to `sys.path`. It's edited locally (e.g. `statistics.py` emits extra fields), so treat it as project code, not pristine upstream.
- **VPS (`server/server_app.py`, :8100 behind Apache)**: doesn't poll anything. It accepts `POST /api/push` (`X-API-Key` / `MONITOR_API_KEY`), appends to in-memory deques, and exposes the same `/api/...` shapes so the same templates work unchanged.

## Conventions and gotchas

- **Shared templates are symlinks.** `server/templates/combined_v2.html` and `server/templates/flow_diagram.html` are git-tracked symlinks into `templates/`. Edit the file in `templates/` and never replace a symlink with a copy. Any data-key change to a template has to work with both the rubberduck and VPS backends.
- **Additive payload keys.** Dashboards, the flow diagram, the engineer page, and the VPS all read the same JSON dicts. Add new keys and leave existing ones in place.
- **Sign convention**: battery power/current is positive when charging. Solis `active_power` is positive for export and negative for import.
- Readers use `self.data.update(new)` rather than replacing the dict, so one failed read leaves the last good values in place. Keep it that way.
- Reader resilience rules (reconnect on `isError()`, batched Modbus reads with ~300 ms gaps, staleness watchdog, 30 s heartbeat log) are documented in README "Solis reader reliability". Keep them in place when you edit readers.
- selpi's "Attempted to start multiple logins" error means *the SP Pro returned no data*, not a password or session clash. The Lantronix bridge and selpi port 10001 each accept only one TCP client at a time.
- `message.txt` is the editable banner served by `/api/message` (separate copies for rubberduck and the server).
- Unrelated side projects live in `sigenergy_monitor/` (home Sigenergy system) and `tracker_analysis/` (bifacial tracker study). Each is a self-contained Flask app with its own README, requirements and install script. `combined_app.py`, `eastron_reader.py` and `server_combined_dashboard.html` are legacy.
