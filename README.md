# mumble-agent

Lightweight FastAPI service zum Verwalten von Mumble-Servern als Docker-Container auf einem Linux-Host.

Gehört zu [Easy2-Mumble](https://github.com/nfsmw15/Easy2-Mumble) — der Webserver-Erweiterung für [Easy2-PHP8](https://github.com/nfsmw15/Easy2-PHP8) (`main-dashboard`).

## Was macht der Agent?

Auf jedem Mumble-Host (VPS, Root-Server, Proxmox-VM oder physisch) läuft eine Instanz des `mumble-agent`. Der Agent nimmt HTTPS-Requests vom Easy2-Webserver entgegen (Bearer-Token authentifiziert) und steuert lokal die Docker-Container, in denen die einzelnen Mumble-Server laufen.

```
┌───────────────────┐  HTTPS+Token  ┌─────────────────────────┐
│ Easy2-Mumble      │  ───────────► │ mumble-agent (FastAPI)  │
│ (PHP-Webserver)   │               │   ↓ Docker-API           │
└───────────────────┘               │ mumble-server containers │
                                    └─────────────────────────┘
```

## API

Alle Endpoints unter `/v1`, alle benötigen `Authorization: Bearer <token>`:

| Method | Path | Zweck |
|--------|------|-------|
| GET    | `/ping` | Liveness-Check |
| POST   | `/servers` | Neuen Mumble-Container erstellen |
| DELETE | `/servers/{cid}` | Container löschen |
| POST   | `/servers/{cid}/start` | Container starten |
| POST   | `/servers/{cid}/stop` | Container stoppen |
| POST   | `/servers/{cid}/restart` | Container neu starten |
| GET    | `/servers/{cid}/stats` | Online-User + Uptime |
| GET    | `/servers/{cid}/logs?tail=N` | Letzte N Log-Zeilen |
| PATCH  | `/servers/{cid}` | Konfiguration ändern (Restart) |

Alle Antworten haben das Format `{"ok": bool, ...}`.

## Sicherheit

* Bearer-Token, im Setup zufällig generiert (32 Byte, hex)
* Constant-time Vergleich (`secrets.compare_digest`)
* Container-Label-Guard: nur Container mit `mumble-agent.managed=1` können vom Agent angefasst werden — fremde Docker-Container auf dem Host sind sicher
* Service läuft als unprivilegierter User `mumble-agent` (in der Gruppe `docker` für API-Zugriff)
* systemd-Hardening (`ProtectSystem=strict`, `NoNewPrivileges`, etc.)
* Lauscht nur auf 127.0.0.1:8000 — TLS-Termination muss extern erfolgen

## Voraussetzungen

* Linux (Debian/Ubuntu getestet, andere Distros analog)
* Python 3.10+ (für `X | Y` Type-Syntax)
* Docker
* systemd
* TLS-Reverse-Proxy (nginx/Caddy/Traefik)

## Verzeichnisstruktur

```
mumble-agent/
├── src/
│   ├── agent.py              FastAPI-Hauptdatei
│   └── requirements.txt      Python-Dependencies
├── systemd/
│   └── mumble-agent.service  systemd-Unit (gehärtet)
├── setup.sh                  Installations-Script
├── README.md
└── LICENSE                   AGPLv3
```

## Installation

```bash
git clone https://github.com/nfsmw15/mumble-agent.git
cd mumble-agent
sudo bash setup.sh
```

Das Script:

1. installiert Pakete (`python3-venv`, `docker.io`, `iproute2`, `openssl`)
2. legt Service-User `mumble-agent` an (in der Gruppe `docker`)
3. richtet venv unter `/opt/mumble-agent/venv` ein und installiert FastAPI, uvicorn, docker-py
4. generiert einen zufälligen 64-Zeichen-Token in `/etc/mumble-agent/agent.env`
5. installiert die systemd-Unit (auto-enabled)
6. zeigt dir den Token am Ende — sicher notieren!

Danach:

```bash
sudo systemctl start mumble-agent
sudo systemctl status mumble-agent
```

## Reverse-Proxy

Der Agent lauscht intern auf `127.0.0.1:8000`. Davor muss ein TLS-fähiger Reverse-Proxy stehen.

### Caddy

```caddyfile
mumble1.example.com:8443 {
    reverse_proxy 127.0.0.1:8000
}
```

### nginx

```nginx
server {
    listen 8443 ssl http2;
    server_name mumble1.example.com;
    ssl_certificate     /etc/letsencrypt/live/mumble1.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mumble1.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }
}
```

### Traefik (mit dns-mgr)

```bash
dns-mgr add-service mumble-agent-host1 \
  --backend 127.0.0.1:8000 \
  --domain mumble1.example.com \
  --internal
```

## Test

```bash
TOKEN=$(grep MUMBLE_AGENT_TOKEN /etc/mumble-agent/agent.env | cut -d= -f2)
curl -k https://mumble1.example.com:8443/v1/ping -H "Authorization: Bearer $TOKEN"
```

Erwartete Antwort:
```json
{"ok":true,"agent":"mumble-agent","version":"1.0.0",...}
```

## Konfiguration

Alle Einstellungen in `/etc/mumble-agent/agent.env`:

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `MUMBLE_AGENT_TOKEN` | (generiert) | Bearer-Token für Auth |
| `MUMBLE_AGENT_IMAGE` | `mumblevoip/mumble-server:latest` | Docker-Image |
| `MUMBLE_AGENT_NETWORK` | `host` | Docker-Netzwerk-Mode (`host` oder Bridge) |
| `MUMBLE_AGENT_DATA` | `/var/lib/mumble-agent` | Daten-Volume Root |

Nach Änderung: `systemctl restart mumble-agent`.

## Firewall

| Port | Protokoll | Richtung | Zweck |
|------|-----------|----------|-------|
| 8443 | TCP | Inbound (vom Webserver) | Agent-API |
| 64738–64838 | TCP+UDP | Inbound (Internet) | Mumble-Clients |

Port-Range nach Bedarf in der Easy2-Mumble Host-Konfiguration anpassen.

## Logs

```bash
journalctl -u mumble-agent -f       # Live
journalctl -u mumble-agent --since "1 hour ago"
```

## Deinstallation

```bash
sudo systemctl stop mumble-agent
sudo systemctl disable mumble-agent
sudo rm /etc/systemd/system/mumble-agent.service
sudo systemctl daemon-reload

sudo userdel mumble-agent
sudo rm -rf /opt/mumble-agent /var/lib/mumble-agent /etc/mumble-agent

# Vorhandene Mumble-Container manuell prüfen und entfernen:
docker ps -a --filter "label=mumble-agent.managed=1"
docker ps -a --filter "label=mumble-agent.managed=1" -q | xargs -r docker rm -f
```

## Lizenz

AGPLv3 — siehe [LICENSE](LICENSE).

## Autor

Andreas P. — [https://nfsmw15.de](https://nfsmw15.de)
