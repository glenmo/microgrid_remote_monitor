# Microgrid Remote Monitor — Solis Inverter + Eastron Energy Meter

A Raspberry Pi 5-based monitoring dashboard for:
1. **Solis S6-EH3P 50kW Hybrid Inverter** — battery SoC, PV power, grid power, temperatures, faults
2. **Eastron SDM630MCT V2 Energy Meter** — 3-phase voltages, currents, power, energy, THD, power factor

Both devices share the same RS485 bus via a Modbus TCP gateway.

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

### 3. Configure

Edit the service file to set your gateway IP and device IDs:

```bash
sudo nano /etc/systemd/system/solis-monitor.service
```

Change `--gateway-ip`, `--solis-id`, and `--eastron-id` to match your setup.

### 4. Start

```bash
sudo systemctl start solis-monitor
```

### 5. View Dashboard

Open a browser to: `http://<pi-ip>:5000`

## Command-Line Options

```
python app.py --help

  --host              Flask listen address       (default: 0.0.0.0)
  --port              Flask listen port          (default: 5000)
  --gateway-ip        Modbus TCP gateway IP      (default: 192.168.1.100)
  --gateway-port      Modbus TCP port            (default: 502)
  --solis-id          Solis inverter slave ID    (default: 1)
  --solis-poll        Solis poll interval (sec)  (default: 5)
  --eastron-id        Eastron meter slave ID     (default: 2)
  --eastron-poll      Eastron poll interval (sec)(default: 5)
  --no-solis          Disable Solis reader
  --no-eastron        Disable Eastron reader
  --debug             Enable Flask debug mode
```

## Solis Register Map (Function Code 0x04, Integer Registers)

| Register | Name              | Type | Unit | Scale |
|----------|-------------------|------|------|-------|
| 33139    | Battery SoC       | U16  | %    | 1     |
| 33140    | Battery SoH       | U16  | %    | 1     |
| 33133    | Battery Voltage   | U16  | V    | ÷10   |
| 33134    | Battery Current   | S16  | A    | ÷10   |
| 33135    | Battery Direction  | U16  | —    | 0=chg |
| 33057-58 | PV Total Power    | U32  | W    | 1     |
| 33079-80 | Active Power      | S32  | W    | 1     |
| 33094    | Grid Frequency    | U16  | Hz   | ÷100  |

## Eastron SDM630MCT Register Map (Function Code 0x04, IEEE 754 Float)

| Register | Name              | Unit  |
|----------|-------------------|-------|
| 0x0000   | Phase 1 Voltage   | V     |
| 0x0006   | Phase 1 Current   | A     |
| 0x000C   | Phase 1 Power     | W     |
| 0x001E   | Phase 1 PF        | —     |
| 0x0034   | Total System Power| W     |
| 0x003E   | Total System PF   | —     |
| 0x0046   | Frequency         | Hz    |
| 0x0048   | Import Energy     | kWh   |
| 0x004A   | Export Energy     | kWh   |
| 0x0156   | Total Energy      | kWh   |

Each register value spans 2 consecutive registers (4 bytes, IEEE 754 big-endian float).

## API Endpoints

| Endpoint                | Description                         |
|-------------------------|-------------------------------------|
| `GET /`                 | Combined dashboard                  |
| `GET /api/data`         | Current Solis inverter data (JSON)  |
| `GET /api/history`      | Solis 24-hour history (JSON)        |
| `GET /api/status`       | Solis connection status (JSON)      |
| `GET /api/eastron/data`     | Current Eastron meter data (JSON)|
| `GET /api/eastron/history`  | Eastron 24-hour history (JSON)  |
| `GET /api/eastron/status`   | Eastron connection status (JSON)|

## Testing with Simulator

```bash
# Terminal 1 — start simulator (serves both Solis slave 1 + Eastron slave 2)
python simulator.py --port 5020

# Terminal 2 — start app
python app.py --gateway-ip 127.0.0.1 --gateway-port 5020
```

## Network Setup

The Solis inverter and Eastron meter both connect via RS485. You need a Modbus
TCP gateway (e.g. Waveshare RS485-to-Ethernet) to bridge the RS485 bus to TCP/IP.
Both devices share the bus but use different Modbus slave addresses (default: Solis=1, Eastron=2).

## File Structure

```
solis_monitor/
├── app.py              # Main Flask app + Solis reader
├── eastron_reader.py   # Eastron SDM630MCT reader
├── simulator.py        # Combined Modbus TCP simulator
├── install.sh          # Raspberry Pi setup script
├── requirements.txt    # Python dependencies
├── templates/
│   └── dashboard.html  # Combined web dashboard
└── static/             # (future: custom CSS/JS assets)
```

## Dependencies

- Python 3.9+
- Flask >= 3.0
- pymodbus >= 3.6
- Chart.js (loaded from CDN)
