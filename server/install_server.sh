#!/bin/bash
# ============================================================
# Microgrid Monitor — Server Setup (pignus.arachnoid.net.au)
# ============================================================
# Run on the server:  bash install_server.sh
# ============================================================

set -e

echo "============================================"
echo " Microgrid Monitor — Server Setup"
echo "============================================"

# --- Check we're on the server ---
INSTALL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# --- Generate API key if not set ---
if [ -z "$MONITOR_API_KEY" ]; then
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo ""
    echo "Generated API key: $API_KEY"
    echo ""
    echo "SAVE THIS KEY — you'll need it on the Pi too."
    echo ""
else
    API_KEY="$MONITOR_API_KEY"
fi

# --- Python virtual environment ---
VENV_DIR="$INSTALL_DIR/venv"

echo "[1/5] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "[2/5] Installing Python dependencies..."
pip install --upgrade pip
pip install flask

# --- Systemd service ---
echo "[3/5] Installing systemd service..."

SERVICE_FILE="/etc/systemd/system/microgrid-monitor.service"
sudo tee "$SERVICE_FILE" > /dev/null <<SERVICEEOF
[Unit]
Description=Microgrid Remote Monitor Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
Environment=MONITOR_API_KEY=$API_KEY
ExecStart=$VENV_DIR/bin/python server_app.py --host 127.0.0.1 --port 8100
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable microgrid-monitor.service

# --- Apache setup ---
echo "[4/5] Configuring Apache..."

# Enable required modules
sudo a2enmod proxy proxy_http rewrite ssl

# Copy vhost config
sudo cp "$INSTALL_DIR/monitor.mooramoora.org.au.conf" /etc/apache2/sites-available/

# Enable the site (HTTP only for now — SSL added after certbot)
# Create a temporary HTTP-only config for certbot
sudo tee /etc/apache2/sites-available/monitor.mooramoora.org.au-temp.conf > /dev/null <<TEMPEOF
<VirtualHost *:80>
    ServerName monitor.mooramoora.org.au
    DocumentRoot /var/www/html

    # Needed for certbot challenge
    <Directory /var/www/html>
        AllowOverride All
    </Directory>
</VirtualHost>
TEMPEOF

sudo a2ensite monitor.mooramoora.org.au-temp.conf
sudo systemctl reload apache2

# --- SSL Certificate ---
echo "[5/5] Setting up SSL certificate..."
echo ""
echo "Run certbot to get the SSL certificate:"
echo "  sudo certbot --apache -d monitor.mooramoora.org.au"
echo ""
echo "After certbot succeeds:"
echo "  sudo a2dissite monitor.mooramoora.org.au-temp.conf"
echo "  sudo a2ensite monitor.mooramoora.org.au.conf"
echo "  sudo systemctl reload apache2"
echo ""

# --- Start the service ---
sudo systemctl start microgrid-monitor.service

echo "============================================"
echo " Server setup complete!"
echo "============================================"
echo ""
echo " API key:  $API_KEY"
echo " Service:  sudo systemctl status microgrid-monitor"
echo " Logs:     sudo journalctl -u microgrid-monitor -f"
echo ""
echo " Next steps:"
echo "  1. Point DNS for monitor.mooramoora.org.au to this server"
echo "  2. Run: sudo certbot --apache -d monitor.mooramoora.org.au"
echo "  3. Swap Apache configs (see above)"
echo "  4. On the Pi, set the same API key and start the data pusher"
echo ""
