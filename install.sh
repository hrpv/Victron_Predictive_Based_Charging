#!/bin/bash
# Solar Batterie Manager - Installations-Script
# Ausfuehren mit: bash install.sh

set -e
INSTALL_DIR="/home/pi/solar_battery"

echo "=== Solar Batterie Manager Installation ==="

# Virtual Environment anlegen
echo "[1/4] Virtual Environment anlegen..."
python3 -m venv "$INSTALL_DIR/venv"

# Pakete installieren
echo "[2/4] Python-Pakete installieren..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# Systemd Service installieren
echo "[3/4] Systemd Service installieren..."
sudo cp "$INSTALL_DIR/solar-battery.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable solar-battery

echo "[4/4] Fertig!"
echo ""
echo "Naechste Schritte:"
echo "  1. IP-Adressen in config.yaml anpassen"
echo "  2. sudo systemctl start solar-battery"
echo "  3. Dashboard: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Logs:   journalctl -u solar-battery -f"
echo "Status: sudo systemctl status solar-battery"


# Installation nach geändertem Service File, mit journal logging statt logfile
# sudo cp solar-battery.service /etc/systemd/system/
# sudo systemctl daemon-reload
# sudo systemctl enable solar-battery
# sudo systemctl start solar-battery

# # Abfragen
# sudo systemctl status solar-battery
# sudo journalctl -u solar-battery -f        # Live-Log
# sudo journalctl -u solar-battery --since today