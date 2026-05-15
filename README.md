# mumble-agent

Leichtgewichtiger FastAPI-Service zum Verwalten von Mumble-Servern als Docker-Container auf einem Linux-Host.

Gehört zu [Easy2-Mumble](https://github.com/nfsmw15/easy2-mumble) — der Webserver-Erweiterung für [Easy2-PHP8](https://github.com/nfsmw15/Easy2-PHP8) (`main-dashboard`).

## Was macht der Agent?

Auf jedem Mumble-Host läuft eine Instanz des `mumble-agent`. Er nimmt HTTPS-Requests vom Easy2-Webserver entgegen (Bearer-Token authentifiziert) und steuert lokal die Docker-Container der einzelnen Mumble-Server.

```
┌───────────────────┐  HTTPS+Token  ┌─────────────────────────┐
│ Easy2-Mumble      │  ───────────► │ mumble-agent (FastAPI)  │
│ (PHP-Webserver)   │               │   ↓ Docker-API           │
└───────────────────┘               │ mumble-server containers │
                                    └─────────────────────────┘
```

## API

Alle Endpoints unter `/v1/`, alle benötigen `Authorization: Bearer <token>`:

| Method | Path | Zweck |
|--------|------|-------|
| GET    | `/v1/ping` | Liveness-Check |
| POST   | `/v1/servers` | Neuen Mumble-Container erstellen |
| DELETE | `/v1/servers/{cid}` | Container löschen |
| POST   | `/v1/servers/{cid}/start` | Container starten |
| POST   | `/v1/servers/{cid}/stop` | Container stoppen |
| POST   | `/v1/servers/{cid}/restart` | Container neu starten |
| GET    | `/v1/servers/{cid}/stats` | Online-User + Uptime |
| GET    | `/v1/servers/{cid}/logs?tail=N` | Letzte N Log-Zeilen |
| PATCH  | `/v1/servers/{cid}` | Konfiguration ändern (löst Restart aus) |
| GET    | `/v1/servers/{cid}/config` | Rohe INI-Konfiguration lesen |
| PUT    | `/v1/servers/{cid}/config` | Rohe INI-Konfiguration schreiben |
| GET    | `/v1/servers/{cid}/superuser` | SuperUser-Passwort aus Logs lesen |
| POST   | `/v1/servers/{cid}/superuser/reset` | SuperUser-Passwort zurücksetzen |
| GET    | `/v1/servers/{cid}/viewer` | Channel-Struktur + Online-User |

Alle Antworten haben das Format `{"ok": bool, ...}`.

## Channel-Viewer

Der `/viewer`-Endpoint liest Channel-Daten und Online-User **ohne** sich als Mumble-Client einzuloggen — verbundene Nutzer sehen keine Ankündigungen:

- **Channel-Struktur** → direkt aus der SQLite-Datenbank des Containers (`docker exec sqlite3`)
- **Online-User** → aus den Docker-Logs des Containers (parst `Authenticated`/`Connection closed`-Events)

Ergebnis wird 30 Sekunden gecacht.

## Sicherheit

- Bearer-Token, im Setup zufällig generiert (32 Byte, hex)
- Constant-time Vergleich (`secrets.compare_digest`)
- Container-Label-Guard: nur Container mit `mumble-agent.managed=1` werden angefasst
- Service läuft als unprivilegierter User `mumble-agent` (in der Gruppe `docker`)
- systemd-Hardening (`ProtectSystem=strict`, `NoNewPrivileges` etc.)
- Lauscht nur auf `127.0.0.1:8000` — TLS-Termination erfolgt extern

## Voraussetzungen

- Linux (Debian/Ubuntu getestet)
- Python 3.10+
- Docker
- systemd
- TLS-Reverse-Proxy (nginx, Caddy, Traefik)

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
└── LICENSE.md                AGPLv3
```

## Installation

```bash
git clone https://github.com/nfsmw15/mumble-agent.git
cd mumble-agent
sudo bash setup.sh
```

Das Script:

1. installiert Pakete (`python3-venv`, `docker.io`, `iproute2`, `sqlite3`)
2. legt Service-User `mumble-agent` an (Gruppe `docker`)
3. richtet venv unter `/opt/mumble-agent/venv` ein
4. generiert einen zufälligen 64-Zeichen-Token in `/etc/mumble-agent/agent.env`
5. installiert die systemd-Unit (auto-enabled)
6. zeigt den Token am Ende — sicher notieren!

```bash
sudo systemctl start mumble-agent
sudo systemctl status mumble-agent
```

## Reverse-Proxy

Der Agent lauscht auf `127.0.0.1:8000`. Davor muss ein TLS-Reverse-Proxy stehen.

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

## Test

```bash
TOKEN=$(grep MUMBLE_AGENT_TOKEN /etc/mumble-agent/agent.env | cut -d= -f2)
curl -k https://mumble1.example.com:8443/v1/ping -H "Authorization: Bearer $TOKEN"
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

## Logs

```bash
journalctl -u mumble-agent -f
journalctl -u mumble-agent --since "1 hour ago"
```

## Deinstallation

```bash
sudo systemctl stop mumble-agent && sudo systemctl disable mumble-agent
sudo rm /etc/systemd/system/mumble-agent.service && sudo systemctl daemon-reload
sudo userdel mumble-agent
sudo rm -rf /opt/mumble-agent /var/lib/mumble-agent /etc/mumble-agent

# Vorhandene Mumble-Container prüfen:
docker ps -a --filter "label=mumble-agent.managed=1"
docker ps -a --filter "label=mumble-agent.managed=1" -q | xargs -r docker rm -f
```

## Lizenz

AGPLv3 — siehe [LICENSE.md](LICENSE.md)

## Autor

Andreas P. — [https://nfsmw15.de](https://nfsmw15.de)
