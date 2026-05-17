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
if [[ ! -f "$CONFIG_DIR/agent.env" ]]; then
    TOKEN=$(openssl rand -hex 32)
    cat > "$CONFIG_DIR/agent.env" <<EOF
# mumble-agent configuration
MUMBLE_AGENT_TOKEN=$TOKEN
MUMBLE_AGENT_IMAGE=mumblevoip/mumble-server:v1.5.735
MUMBLE_AGENT_NETWORK=host
MUMBLE_AGENT_DATA=/var/lib/mumble-agent
EOF
    chmod 0640 "$CONFIG_DIR/agent.env"
    chown root:"$USER" "$CONFIG_DIR/agent.env"
    echo
    echo "  >>> Generierter Agent-Token (für Easy2-Mumble Host-Eintrag):"
    echo "  >>> $TOKEN"
    echo
else
    echo "  $CONFIG_DIR/agent.env existiert bereits, übersprungen."
    echo "  Token aus der Datei lesen mit: cat $CONFIG_DIR/agent.env"
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
echo "  1. TLS-Reverse-Proxy einrichten (nginx/Caddy/Traefik) der"
echo "     auf https://<host>:8443 hört und an http://127.0.0.1:8000"
echo "     weiterleitet."
echo
echo "  2. Service starten:    systemctl start mumble-agent"
echo "  3. Status prüfen:      systemctl status mumble-agent"
echo "  4. Logs ansehen:       journalctl -u mumble-agent -f"
echo
echo "  5. In der Easy2-Mumble Weboberfläche den Host eintragen mit:"
echo "       Agent-URL:   https://<dein-fqdn>:8443"
echo "       Agent-Token: (aus /etc/mumble-agent/agent.env)"
echo
echo "  Zertifikat-Deployment:"
echo "     Zertifikat nach $CONFIG_DIR/ssl/cert.pem + key.pem kopieren,"
echo "     dann: mumble-cert-deploy"
echo "     (dns-mgr/certbot/acme.sh können als reload_cmd genutzt werden)"
echo "=========================================================="
