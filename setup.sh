#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# mumble-agent Setup-Script
# Aufruf: sudo bash setup.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo)."
    exit 1
fi

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

INSTALL_DIR=/opt/mumble-agent
DATA_DIR=/var/lib/mumble-agent
CONFIG_DIR=/etc/mumble-agent
USER=mumble-agent
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/7] System-Pakete installieren..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip docker.io ca-certificates iproute2 openssl sqlite3

echo "[2/7] Service-User anlegen..."
if ! id "$USER" >/dev/null 2>&1; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$USER"
fi
usermod -aG docker "$USER"

echo "[3/7] Verzeichnisse anlegen..."
install -d -m 0750 -o "$USER" -g "$USER" "$INSTALL_DIR"
install -d -m 0750 -o "$USER" -g "$USER" "$DATA_DIR"
install -d -m 0750 -o root    -g "$USER" "$CONFIG_DIR"

echo "[4/7] Code kopieren..."
cp "$SCRIPT_DIR/src/agent.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/src/requirements.txt" "$INSTALL_DIR/"
chown -R "$USER:$USER" "$INSTALL_DIR"

echo "[4b] Zertifikat-Verzeichnis anlegen..."
install -d -m 0750 -o root -g "$USER" "$CONFIG_DIR/ssl"

echo "[4c] mumble-cert-deploy installieren..."
install -m 0755 "$SCRIPT_DIR/src/mumble-cert-deploy" /usr/local/bin/mumble-cert-deploy

echo "[5/7] Python venv aufsetzen..."
sudo -u "$USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "[6/7] Konfiguration anlegen..."

# Netzwerk-Setup abfragen
INTERNAL_IP=$(hostname -I | awk '{print $1}')
echo
echo "  Wie wird der Agent erreichbar sein?"
echo "  1) Internes Netz / LAN  (direkt per IP, kein Proxy)"
echo "     → Agent-URL im Webinterface: http://${INTERNAL_IP}:8000"
echo "  2) Reverse-Proxy auf diesem Host  (nginx/Caddy/Traefik lokal)"
echo "     → Agent-URL im Webinterface: https://<domain>:8443"
echo "  3) Zentraler Proxy-LXC oder -VM  (separater Proxy leitet weiter)"
echo "     → Agent-URL im Webinterface: https://<domain>"
echo
read -rp "  Auswahl [1/2/3, Standard: 1]: " NET_CHOICE
NET_CHOICE="${NET_CHOICE:-1}"

case "$NET_CHOICE" in
    2|3) AGENT_HOST="127.0.0.1" ;;
    *)   AGENT_HOST="0.0.0.0" ;;
esac

if [[ ! -f "$CONFIG_DIR/agent.env" ]]; then
    TOKEN=$(openssl rand -hex 32)
    cat > "$CONFIG_DIR/agent.env" <<EOF
# mumble-agent configuration
MUMBLE_AGENT_TOKEN=$TOKEN
MUMBLE_AGENT_IMAGE=mumblevoip/mumble-server:v1.5.735
MUMBLE_AGENT_NETWORK=host
MUMBLE_AGENT_DATA=/var/lib/mumble-agent
# Netzwerk: 0.0.0.0 = alle Interfaces, 127.0.0.1 = nur lokal (mit Reverse-Proxy)
MUMBLE_AGENT_HOST=$AGENT_HOST
MUMBLE_AGENT_PORT=8000
EOF
    chmod 0640 "$CONFIG_DIR/agent.env"
    chown root:"$USER" "$CONFIG_DIR/agent.env"
    echo
    echo "  >>> Generierter Agent-Token (für Easy2-Mumble Host-Eintrag):"
    echo "  >>> $TOKEN"
    echo
else
    echo "  $CONFIG_DIR/agent.env existiert bereits, übersprungen."
    TOKEN=$(grep MUMBLE_AGENT_TOKEN "$CONFIG_DIR/agent.env" | cut -d= -f2)
    echo "  Token: $TOKEN"
fi

echo "[7/8] systemd-Unit installieren..."
cp "$SCRIPT_DIR/systemd/mumble-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable mumble-agent.service

echo "[8/8] Fertig."
echo
echo "=========================================================="
echo "Agent installiert. Nächste Schritte:"
echo
echo "  1. Service starten:  systemctl start mumble-agent"
echo "  2. Status prüfen:    systemctl status mumble-agent"
echo "  3. Logs ansehen:     journalctl -u mumble-agent -f"
echo

case "$NET_CHOICE" in
1)
    echo "  Netzwerk-Modus: Internes Netz / LAN (kein Proxy)"
    echo
    echo "  In der Easy2-Mumble Weboberfläche eintragen:"
    echo "    Agent-URL:   http://${INTERNAL_IP}:8000"
    echo "    Agent-Token: $TOKEN"
    ;;
2)
    echo "  Netzwerk-Modus: Reverse-Proxy auf diesem Host"
    echo
    echo "  Reverse-Proxy (nginx/Caddy/Traefik) einrichten der"
    echo "  auf Port 8443 hört und an http://127.0.0.1:8000 weiterleitet."
    echo "  Beispiele: siehe README.md"
    echo
    echo "  In der Easy2-Mumble Weboberfläche eintragen:"
    echo "    Agent-URL:   https://<dein-hostname>:8443"
    echo "    Agent-Token: $TOKEN"
    ;;
3)
    echo "  Netzwerk-Modus: Zentraler Proxy-LXC / -VM"
    echo
    echo "  Proxy so konfigurieren dass er an http://${INTERNAL_IP}:8000"
    echo "  weiterleitet. Beispiele: siehe README.md"
    echo
    echo "  In der Easy2-Mumble Weboberfläche eintragen:"
    echo "    Agent-URL:   https://<dein-hostname>"
    echo "    Agent-Token: $TOKEN"
    ;;
esac

echo
echo "  Zertifikat-Deployment (falls Proxy mit eigenem Cert):"
echo "    Zertifikat nach $CONFIG_DIR/ssl/cert.pem + key.pem kopieren,"
echo "    dann: mumble-cert-deploy"
echo "=========================================================="
