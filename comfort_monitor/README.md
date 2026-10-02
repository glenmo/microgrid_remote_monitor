# Lodge Comfort

Public dashboard for the Moora Moora lodge aircons at
<https://monitor.mooramoora.org.au/comfort/>: live state of the Lounge and Dining Room
Daikins, temperature and heating/cooling history, and an explainer for the solar-surplus
heating automation.

## Data flow

```
Daikin units → LodgyBox (Home Assistant, lodge LAN)
             → POST every 60 s → pignus /comfort/api/push  (X-API-Key)
             → SQLite (35 days) → /comfort/  +  /comfort/api/{current,history,summary}
```

pignus can't reach the lodge LAN, so LodgyBox pushes. The Home Assistant side lives in
`glenmo/lodge_home_assistant`, `packages/comfort_push.yaml`.

**Privacy:** only `PUBLIC_ROOMS` (Lounge, Dining Room) are accepted, stored or served. The
Caretaker's Flat is someone's home: LodgyBox never sends it, and this app drops it if it
ever arrives.

## API

| Endpoint | |
| --- | --- |
| `POST /api/push` | Snapshot from LodgyBox. Header `X-API-Key: $COMFORT_API_KEY`. |
| `GET /api/current` | Latest snapshot, `age_s`, `stale` (no push for over 5 min). |
| `GET /api/history?hours=24` | Bucketed series (max 720 h, about 360 points): temps, heating/cooling fraction, Lounge compressor kW, surplus. |
| `GET /api/summary?days=7` | Per local day: hours heating/cooling, Lounge kWh, inside min/max. |
| `GET /healthz` | Liveness plus age of the last push. |

"Contribution" is measured in hours of active heating or cooling (`hvac_action`). Only the
Lounge unit reports energy (compressor kW and kWh today), so kWh is shown for it alone.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
COMFORT_DB=/tmp/comfort.db COMFORT_API_KEY=test venv/bin/python app.py --port 8125
```

## Deploy (pignus)

```bash
cd ~/microgrid_remote_monitor && git pull --ff-only
sudo bash comfort_monitor/install.sh          # venv, key in /etc/comfort-monitor.env, systemd unit
# Apache: add the /comfort/ block from server/monitor.mooramoora.org.au.conf to the live vhost
sudo apachectl configtest && sudo systemctl reload apache2
```

Updates after that: `git pull --ff-only && sudo systemctl restart comfort-monitor`.
