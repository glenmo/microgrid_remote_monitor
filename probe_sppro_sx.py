#!/usr/bin/env python3
"""End-to-end smoke test for the SP Pro Sx reader."""
import json, logging, os, sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

HOST = os.environ.get("SP_HOST", "192.168.11.240")
PORT = int(os.environ.get("SP_PORT", "10001"))
PWD  = os.environ.get("SELPI_SPPRO_PASSWORD", "")

print(f"\nSP Pro Sx end-to-end smoke test")
print(f"  Target: {HOST}:{PORT}")
print(f"  Password: {'(empty)' if not PWD else f'(set, {len(PWD)} chars)'}")
print("=" * 60)

os.environ["SELPI_CONNECTION_TYPE"]         = "TCP"
os.environ["SELPI_CONNECTION_TCP_HOSTNAME"] = HOST
os.environ["SELPI_CONNECTION_TCP_PORT"]     = str(PORT)
os.environ["SELPI_SPPRO_PASSWORD"]          = PWD

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "vendor", "selpi"))

try:
    from statistics import Statistics
except ImportError as e:
    print(f"ERROR: cannot import vendored selpi - {e}")
    sys.exit(2)

print("[1] Constructing selpi Statistics ...")
try:
    stats = Statistics()
    print("    OK")
except Exception as e:
    print(f"    FAILED: {type(e).__name__}: {e}")
    sys.exit(3)

print("[2] Calling get_select_emulated() (TCP connect + auth + reads) ...")
try:
    data = stats.get_select_emulated()
except Exception as e:
    print(f"    FAILED: {type(e).__name__}: {e}")
    print()
    print("    'Login failed' / 'multiple logins' -> wrong password.")
    print("    'Expected N bytes, only able to read M' -> stale Lantronix")
    print("       buffer, wait 60s and retry, or power-cycle the Lantronix.")
    sys.exit(4)

print(f"    OK - got {len(data)} fields")
print()
print("[3] Decoded data:")
print(json.dumps(data, indent=2, default=str))

print()
print("[4] Sanity check:")
checks = [
    ("battery_soc",     lambda v: 0 <= v <= 100,        "% should be 0-100"),
    ("battery_w",       lambda v: -50000 < v < 50000,   "W reasonable"),
    ("grid_w",          lambda v: -50000 < v < 50000,   "W reasonable"),
    ("load_w",          lambda v: -1000 < v < 50000,    "W reasonable"),
    ("solarinverter_w", lambda v: -100 < v < 50000,     "W reasonable"),
]
all_ok = True
for key, validator, hint in checks:
    v = data.get(key)
    ok = v is not None and validator(v)
    mark = "OK " if ok else "BAD"
    print(f"    [{mark}] {key:20s} = {v}    ({hint})")
    if not ok: all_ok = False

print()
if all_ok:
    print("VERDICT: SP Pro Sx reader works end-to-end.")
    print("         Ready to wire into app.py and build the dashboard.")
else:
    print("VERDICT: Got data but values look suspect.")
