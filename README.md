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

### Container-Verwaltung

| Method | Path | Zweck |
|--------|------|-------|
| GET    | `/v1/ping` | Liveness-Check |
| POST   | `/v1/servers` | Neuen Mumble-Container erstellen |
| DELETE | `/v1/servers/{cid}` | Container löschen |
| POST   | `/v1/servers/{cid}/start` | Container starten |
| POST   | `/v1/servers/{cid}/stop` | Container stoppen |
| POST   | `/v1/servers/{cid}/restart` | Container neu starten |
| GET    | `/v1/servers/{cid}/stats` | Online-User + Uptime (via ICE) |
| GET    | `/v1/servers/{cid}/logs?tail=N` | Letzte N Log-Zeilen |
| PATCH  | `/v1/servers/{cid}` | Konfiguration ändern (löst Container-Recreate aus) |
| GET    | `/v1/servers/{cid}/settings` | Aktuelle Einstellungen aus ENV lesen |
| GET    | `/v1/servers/{cid}/config` | Rohe INI-Konfiguration lesen |
| PUT    | `/v1/servers/{cid}/config` | Rohe INI-Konfiguration schreiben |
| GET    | `/v1/servers/{cid}/superuser` | SuperUser-Passwort aus Logs lesen |
| POST   | `/v1/servers/{cid}/superuser/reset` | SuperUser-Passwort zurücksetzen |
| PUT    | `/v1/servers/{cid}/certificate` | TLS-Zertifikat hochladen |
| DELETE | `/v1/servers/{cid}/certificate` | TLS-Zertifikat entfernen |

### ZeroC ICE — Live-Verwaltung (kein Neustart)

| Method | Path | Zweck |
|--------|------|-------|
| GET    | `/v1/servers/{cid}/viewer` | Channel-Baum + Online-User (ICE `getTree()`) |
| GET    | `/v1/servers/{cid}/users` | Verbundene User (ICE `getUsers()`) |
| POST   | `/v1/servers/{cid}/users/{session}/kick` | User kicken (ICE `kickUser()`) |
| PATCH  | `/v1/servers/{cid}/users/{session}` | Mute/Deaf setzen (ICE `setState()`) |
| GET    | `/v1/servers/{cid}/channels` | Channel-Map (ICE `getChannels()`) |
| POST   | `/v1/servers/{cid}/channels` | Channel erstellen (ICE `addChannel()`) |
| PATCH  | `/v1/servers/{cid}/channels/{id}` | Channel bearbeiten (ICE `setChannelState()`) |
| DELETE | `/v1/servers/{cid}/channels/{id}` | Channel löschen (ICE `removeChannel()`) |
| GET    | `/v1/servers/{cid}/acl` | ACL lesen (ICE `getACL()`) |
| PUT    | `/v1/servers/{cid}/acl` | ACL setzen (ICE `setACL()`) |
| GET    | `/v1/servers/{cid}/bans` | Aktive Bans (ICE `getBans()`) |
| PUT    | `/v1/servers/{cid}/bans` | Bans setzen (ICE `setBans()`) |
| PATCH  | `/v1/servers/{cid}/live` | Einstellungen live ändern (ICE `setConf()`) |

Alle Antworten haben das Format `{"ok": bool, ...}`.

## ZeroC ICE

Ab v2.0.0 kommuniziert der Agent direkt mit dem laufenden Mumble-Prozess über **ZeroC ICE**. Der ICE-Port wird automatisch aus der Container-Konfiguration gelesen:

```
MUMBLE_CONFIG_ICE=tcp -h 127.0.0.1 -p 6502
```

**Wichtig bei `--network host`**: Jeder Container muss einen eigenen ICE-Port bekommen, da sie sonst denselben Port auf dem Host belegen. Beim Erstellen `MUMBLE_CONFIG_ICE` explizit setzen (z.B. 6502, 6503, 6504…).

## Sicherheit

- Bearer-Token, im Setup zufällig generiert (32 Byte, hex)
- Constant-time Vergleich (`secrets.compare_digest`)
- Container-Label-Guard: nur Container mit `mumble-agent.managed=1` werden angefasst
- Service läuft als unprivilegierter User `mumble-agent` (in der Gruppe `docker`)
- systemd-Hardening (`ProtectSystem=strict`, `NoNewPrivileges` etc.)
- Listen-Adresse über `MUMBLE_AGENT_HOST` konfigurierbar (`0.0.0.0` = direkt per interner IP, `127.0.0.1` = nur mit Reverse-Proxy)

## Voraussetzungen

- Linux (Debian/Ubuntu getestet)
- Python 3.10+
- Docker
- systemd
- `zeroc-ice` Python-Paket (wird via `pip install zeroc-ice` installiert; benötigt `libssl-dev`, `libbz2-dev`)
- Reverse-Proxy optional — für interne Netze reicht `MUMBLE_AGENT_HOST=0.0.0.0`

Manuell vorab installieren (alles weitere übernimmt `setup.sh`):

```bash
apt-get install -y git sudo
```

> `sudo` muss auch dann installiert sein, wenn du bereits als root arbeitest — `setup.sh` nutzt `sudo -u mumble-agent` intern zum Wechsel auf den Service-User.

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
# Voraussetzungen vorab installieren
apt-get install -y git sudo

# Repo klonen und Setup starten (als root)
git clone https://github.com/nfsmw15/mumble-agent.git
cd mumble-agent
bash setup.sh
```

Das Script:

1. installiert Pakete (`python3-venv`, `python3-pip`, `docker.io`, `ca-certificates`, `iproute2`, `openssl`, `sqlite3`)
2. legt Service-User `mumble-agent` an (Gruppe `docker`)
3. richtet venv unter `/opt/mumble-agent/venv` ein
4. generiert einen zufälligen 64-Zeichen-Token in `/etc/mumble-agent/agent.env`
5. installiert die systemd-Unit (auto-enabled)
6. zeigt den Token am Ende — sicher notieren!

```bash
sudo systemctl start mumble-agent
sudo systemctl status mumble-agent
```

## Netzwerk-Zugang

### Internes Netz ohne Reverse-Proxy (empfohlen für Proxmox/LAN)

Standard nach Setup: `MUMBLE_AGENT_HOST=0.0.0.0` — der Agent ist direkt per interner IP erreichbar. Im Webinterface dann `http://192.168.x.x:8000` als Agent-URL eintragen. Der Bearer-Token schützt den Zugang.

### Mit TLS-Reverse-Proxy (für öffentliche Server)

`MUMBLE_AGENT_HOST=127.0.0.1` in `agent.env` setzen, dann einen Reverse-Proxy davor schalten:

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
| `MUMBLE_AGENT_HOST` | `0.0.0.0` | Listen-Adresse (`0.0.0.0` = alle Interfaces, `127.0.0.1` = nur lokal) |
| `MUMBLE_AGENT_PORT` | `8000` | Listen-Port |
| `MUMBLE_AGENT_IMAGE` | `mumblevoip/mumble-server:v1.5.735` | Docker-Image (gepinnte Version) |
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
