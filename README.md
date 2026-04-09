# Solis 50kW Hybrid Inverter — Modbus TCP Monitor

A Raspberry Pi 5-based monitoring dashboard for the Solis S6-EH3P 50kW hybrid inverter.
Reads registers via Modbus TCP/IP and displays a real-time web dashboard.

## What it does

- Reads 40+ registers from the Solis inverter every 5 seconds via Modbus TCP
- Displays a full dashboard with:
  - Battery SoC gauge (colour-coded red/orange/yellow/green)
  - PV power, grid power, battery power metrics
  - Grid frequency, temperatures, fault codes
  - PV string details (4 strings: voltage, current, power)
  - AC grid phase voltages and currents
  - Battery BMS details
  - Historical charts (SoC and power flow over 24 hours)
- Auto-refreshes every 5 seconds
- Runs as a systemd service for auto-start on boot

## Key Register Addresses (Solis Protocol v3.1)

| Register | Name              | Type | Unit   | Scale |
|----------|-------------------|------|--------|-------|
| 33139    | Battery SoC       | U16  | %      | 1     |
| 33140    | Battery SoH       | U16  | %      | 1     |
| 33133    | Battery Voltage   | U16  | V      | ÷10   |
| 33134    | Battery Current   | S16  | A      | ÷10   |
| 33135    | Battery Direction  | U16  | —      | 0=chg |
| 33057-58 | PV Total Power    | U32  | W      | 1     |
| 33079-80 | Active Power      | S32  | W      | 1     |
| 33094    | Grid Frequency    | U16  | Hz     | ÷100  |
| 33073    | Grid V (A-B)      | U16  | V      | ÷10   |

All registers use function code 0x04 (Read Input Registers).

## Quick Start

### 1. Copy to Raspberry Pi

```bash
scp -r solis_monitor/ pi@<pi-ip>:~/solis_monitor/
```

### 2. Install

```bash
ssh pi@<pi-ip>
cd ~/solis_monitor
bash install.sh
```

### 3. Configure the inverter IP

Edit the service file to set your inverter/gateway IP address:

```bash
sudo nano /etc/systemd/system/solis-monitor.service
```

Change `--inverter-ip 192.168.1.100` to your inverter's IP.

### 4. Start

```bash
sudo systemctl start solis-monitor
```

### 5. View Dashboard

Open a browser to: `http://<pi-ip>:5000`

## Command-Line Options

```
python app.py --help

  --host             Flask listen address    (default: 0.0.0.0)
  --port             Flask listen port       (default: 5000)
  --inverter-ip      Inverter/gateway IP     (default: 192.168.1.100)
  --inverter-port    Modbus TCP port         (default: 502)
  --slave-id         Modbus slave/unit ID    (default: 1)
  --poll-interval    Seconds between polls   (default: 5)
  --debug            Enable Flask debug mode
```

## Testing with Simulator

Run the simulator on the same machine to test without a real inverter:

```bash
# Terminal 1 — start simulator
python simulator.py --port 5020

# Terminal 2 — start app pointing at simulator
python app.py --inverter-ip 127.0.0.1 --inverter-port 5020
```

Note: The simulator has known threading race conditions with U32 register reads.
The real inverter hardware provides atomic register access.

## API Endpoints

| Endpoint       | Description                              |
|----------------|------------------------------------------|
| `GET /`        | Dashboard web page                       |
| `GET /api/data`    | Current inverter readings (JSON)     |
| `GET /api/history` | 24-hour history for charts (JSON)    |
| `GET /api/status`  | Connection/polling status (JSON)     |

## Network Setup

The Solis inverter uses RS485 Modbus RTU natively. To use Modbus TCP, you need
either:

1. **Modbus TCP Gateway** — A device like a Waveshare RS485-to-Ethernet module
   that bridges RS485 RTU to TCP/IP
2. **Solis data logger with LAN** — Some Solis loggers expose Modbus TCP on port 502

Connect the gateway/logger to your local network and note its IP address.

## Useful Commands

```bash
sudo systemctl status solis-monitor     # Check if running
sudo journalctl -u solis-monitor -f     # Live logs
sudo systemctl restart solis-monitor    # Restart after config change
sudo systemctl stop solis-monitor       # Stop
```

## Dependencies

- Python 3.9+
- Flask >= 3.0
- pymodbus >= 3.6
- Chart.js (loaded from CDN)
