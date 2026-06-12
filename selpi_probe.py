#!/usr/bin/env python3
"""
selpi_probe.py — minimal isolated test of the SP Pro selpi login.

Opens a clean TCP connection through the Lantronix UDS bridge (first
draining any stale bytes), constructs the vendored selpi Statistics
client, polls one snapshot, and tears down cleanly.

Diagnostic split:

    Probe succeeds → password is correct; the bug is in the long-running
                     reader's reconnect path leaking sockets across
                     attempts. Apply the sppro_sx_reader.py patch.

    Probe fails    → password really doesn't match (or selpi can't
                     handshake at all). No reader-code patch will help.

Usage on rubberduck:
    sudo systemctl stop microgrid-monitor
    cd ~/microgrid_remote_monitor
    ./venv/bin/python selpi_probe.py --password "Selectronic SP Pro"
    sudo systemctl start microgrid-monitor
"""
import argparse
import os
import socket
import sys
import time


def drain(host, port, label, drain_ms=500):
    """Brief throwaway socket to flush the Lantronix RX buffer."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, port))
        s.settimeout(drain_ms / 1000.0)
        total = 0
        try:
            while True:
                data = s.recv(256)
                if not data:
                    break
                total += len(data)
                print(f"  [{label}] {len(data)} bytes: {data[:32].hex()}...")
        except socket.timeout:
            pass
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()
        print(f"  [{label}] drained {total} bytes")
        return total
    except Exception as e:
        print(f"  [{label}] drain attempt raised: {e}")
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.11.240")
    ap.add_argument("--port", type=int, default=10001)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    # The reader vendors selpi under microgrid_remote_monitor/vendor/selpi/
    # and configures it via environment variables.
    selpi_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "vendor", "selpi")
    if not os.path.isdir(selpi_dir):
        print(f"ERROR: cannot find vendored selpi at {selpi_dir}")
        sys.exit(2)
    sys.path.insert(0, selpi_dir)

    os.environ["SELPI_CONNECTION_TYPE"]         = "TCP"
    os.environ["SELPI_CONNECTION_TCP_HOSTNAME"] = args.host
    os.environ["SELPI_CONNECTION_TCP_PORT"]     = str(args.port)
    os.environ["SELPI_SPPRO_PASSWORD"]          = args.password

    print(f"--- Phase 1: drain pre-existing Lantronix buffer ---")
    pre = drain(args.host, args.port, "pre-login")
    print(f"  (pre-login drained {pre} bytes — anything > 0 means the "
          f"previous session left residue)")

    # Give the Lantronix a moment to release the throwaway connection.
    time.sleep(0.5)

    print(f"\n--- Phase 2: import vendored selpi ---")
    try:
        from statistics import Statistics  # noqa: E402
    except Exception as e:
        print(f"ERROR: importing vendored selpi failed: {type(e).__name__}: {e}")
        print(f"      sys.path[0] = {sys.path[0]}")
        sys.exit(3)
    print("  vendored selpi imported OK")

    print(f"\n--- Phase 3: construct Statistics() and poll once ---")
    stats = None
    try:
        stats = Statistics()
        print(f"  Statistics() constructed: {stats!r}")
        snapshot = stats.get_select_emulated()
        items = snapshot.get("items", {}) if isinstance(snapshot, dict) else {}
        device = snapshot.get("name", "?") if isinstance(snapshot, dict) else "?"
        print(f"  SUCCESS — device='{device}', {len(items)} items returned")
        # Print a few interesting fields if present
        for k in ("battery_soc", "battery_w", "grid_w", "load_w",
                  "solarinverter_w", "shunt_w"):
            if k in items:
                print(f"    {k} = {items[k]}")
    except Exception as e:
        print(f"  POLL FAILED: {type(e).__name__}: {e}")
        # Probe failure path — re-drain to see what selpi left behind
        time.sleep(0.5)
        post = drain(args.host, args.port, "post-failure")
        print(f"  (post-failure drained {post} bytes — selpi may have left"
              f" residue from its failed handshake)")
        sys.exit(4)
    finally:
        # Phase 4: clean shutdown — explicitly poke at the socket attrs
        # so we don't leave the Lantronix wedged for the service restart.
        print(f"\n--- Phase 4: clean shutdown ---")
        if stats is not None:
            for attr in ("close", "disconnect", "stop"):
                fn = getattr(stats, attr, None)
                if callable(fn):
                    try:
                        fn()
                        print(f"  stats.{attr}() ok")
                        break
                    except Exception as e:
                        print(f"  stats.{attr}() raised: {e}")
            for attr in ("_connection", "_conn", "_socket", "_sock"):
                obj = getattr(stats, attr, None)
                if obj is not None and hasattr(obj, "close"):
                    try:
                        obj.close()
                        print(f"  stats.{attr}.close() ok")
                    except Exception as e:
                        print(f"  stats.{attr}.close() raised: {e}")
            stats = None
        # Final drain to leave the Lantronix in a clean state
        time.sleep(0.5)
        drain(args.host, args.port, "post-cleanup")

    print("\nDone. If Phase 3 reported SUCCESS, the password is correct"
          " — bug is in the long-running reader's reconnect path.")


if __name__ == "__main__":
    main()
