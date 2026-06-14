# kitty — direct-USB SP Pro reader

`kitty` (a Raspberry Pi cabled by USB into the SP Pro's **Advanced Comms Board**,
CDC-ACM at `/dev/ttyACM0`) reads the SP Pro locally and **pushes** the telemetry
to `rubberduck`, which stays the source of truth (Solis polling, dashboard, VPS
push). This replaces the flaky Lantronix Ethernet-to-serial bridge: only the
resulting JSON crosses the network, over the fast local LAN.

```
[SP Pro] --USB--> kitty: sppro_pusher.py (SPProSxReader, serial)
                              |  HTTP POST /api/sppro/ingest
                              v
                         rubberduck: app.py --sppro-source push
                              |  (SPProPushReceiver)
                              v
                    /api/sppro/{data,history,status}  ->  dashboard + flow
```

## Why not a TCP serial bridge (ser2net / `selpi proxy`)?

The SP Pro reads perfectly over **local** USB serial, but bridging that serial
stream over TCP failed: `selpi proxy` is broken upstream (calls a non-existent
`Protocol.send`), and a raw bridge (ser2net) opens the CDC-ACM port without the
line state the board needs (reads hang). Reading locally on kitty and pushing
JSON sidesteps all of it. (selpi's serial read is the proven-good path —
`./selpi stat` works; `SPProSxReader(transport="serial")` reuses it.)

## Deploy on kitty

```bash
# repo checked out at /home/glen/microgrid_remote_monitor (this branch)
sudo cp /home/glen/microgrid_remote_monitor/kitty/sppro-pusher.service \
        /etc/systemd/system/sppro-pusher.service
sudo systemctl daemon-reload
sudo systemctl enable --now sppro-pusher.service
journalctl -u sppro-pusher.service -f      # expect "pushed (connected=True, soc=..)"
```

Needs `python3-serial` (system python). Optional: a udev rule for a stable
`/dev/sppro` symlink (see HANDOFF) — not required, cdc_acm self-binds ttyACM0.

## Deploy on rubberduck

Run `app.py` with `--sppro-source push` (drop the old `--sppro-ip/-port/-password`
Lantronix args). `sx` mode remains available as an instant fallback. Optionally
set `--sppro-ingest-token <secret>` and `INGEST_TOKEN` on kitty to authenticate
the push.
