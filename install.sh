#!/bin/bash
# ============================================================
# Solis Inverter Monitor — Raspberry Pi 5 Setup Script
# ============================================================
# Run this on the Pi:   bash install.sh
# ============================================================

set -e

echo "============================================"
echo " Solis 50kW Inverter Monitor — Setup"
echo "============================================"

# --- System packages ---
echo ""
echo "[1/4] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv

# --- Python virtual environment ---
INSTALL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="$INSTALL_DIR/venv"

echo ""
echo "[2/4] Creating Python virtual environment at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo ""
echo "[3/4] Installing Python dependencies..."
pip install --upgrade pip
pip install -r "$INSTALL_DIR/requirements.txt"

# --- Systemd service ---
echo ""
echo "[4/4] Installing systemd service..."

SERVICE_FILE="/etc/systemd/system/solis-monitor.service"
sudo tee "$SERVICE_FILE" > /dev/null <<SERVICEEOF
[Unit]
Description=Microgrid Remote Monitor (Solis + Eastron)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python app.py --solis-ip 192.168.11.214 --solis-port 502 --solis-id 1 --eastron-ip 192.168.11.214 --eastron-port 502 --eastron-id 2 --solis-poll 5 --eastron-poll 5
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable solis-monitor.service

echo ""
echo "============================================"
echo " Setup complete!"
echo "============================================"
echo ""
echo " IMPORTANT: Edit the IP addresses and device IDs in the service file:"
echo "   sudo nano $SERVICE_FILE"
echo "   (change --solis-ip to your Solis inverter IP, currently 192.168.11.214)"
echo "   (change --eastron-ip to your Eastron meter gateway IP)"
echo "   (change --solis-id / --eastron-id if your slave IDs differ)"
echo ""
echo " Then start the service:"
echo "   sudo systemctl start solis-monitor"
echo ""
echo " View the dashboard at:  http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo " Useful commands:"
echo "   sudo systemctl status solis-monitor    # Check status"
echo "   sudo journalctl -u solis-monitor -f    # View live logs"
echo "   sudo systemctl restart solis-monitor   # Restart"
echo ""
