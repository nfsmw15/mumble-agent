# Changelog — mumble-agent

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.

## [v1.4.0] — 2026-05-15

### Hinzugefügt
- `GET /v1/servers/{cid}/settings` — aktuelle Konfiguration aus Container-ENV-Variablen lesen
- `PUT /v1/servers/{cid}/certificate` — TLS-Zertifikat (PEM) in Container schreiben + Recreate
- `DELETE /v1/servers/{cid}/certificate` — Zertifikat entfernen + Recreate
- `GET /v1/servers/{cid}/viewer` — Channel-Viewer ohne Mumble-Client-Verbindung
  - Channel-Struktur aus SQLite (`docker exec sqlite3`)
  - Online-User und Channel-Position aus Docker-Log-Parsing
  - Temporäre Channels (nicht in SQLite) aus `Added channel`-Logs
  - Channel-Wechsel via `Moved X:session to CHANNEL[id:]`-Format
  - Beim Erstellen eines Temp-Channels impliziter Eintritt (kein separater Move)
  - Default-Channel aus Container-ENV (`MUMBLE_CONFIG_DEFAULTCHANNEL`)
  - Cache-TTL: 10s
- `PATCH /v1/servers/{cid}`: alle Mumble-Config-Felder ergänzt (Bandbreite, Registrierung, AutoBan, suggestVersion, Bonjour etc.)
- `mumble-cert-deploy` Script — deployt Zertifikat auf alle verwalteten Container; Integration mit dns-mgr, certbot, acme.sh
- `setup.sh`: `sqlite3`-Paket, SSL-Verzeichnis `/etc/mumble-agent/ssl/`, `mumble-cert-deploy` installieren

### Geändert
- `/stats`: Online-Zählung via Log-Parsing statt TCP-Verbindungszählung (`ss`-Befehl) — externe Scanner werden nicht mehr mitgezählt

### Behoben
- Log-Regex für Channel-Moves korrigiert: tatsächliches Format `Moved NAME:session(uid) to CHANNEL[id:parent]`

## [v1.2.2] — 2026-05-03

### Geändert
- **PATCH-Endpoint**: Container-Recreate statt INI-Patch + Restart — das Mumble-Docker-Image generiert die `mumble_server_config.ini` bei jedem Start aus den `MUMBLE_CONFIG_*` ENV-Variablen neu, deshalb funktioniert INI-Patching nicht
- **PUT config-Endpoint**: Parst INI-Felder → ENV-Variablen-Mapping → Container-Recreate
- Gibt bei PATCH und PUT die neue `container_id` in der Antwort zurück

## [v1.2.0] — 2026-05-03

### Geändert
- Config-Zugriff (Lesen/Schreiben) komplett auf `docker exec` umgestellt statt direktem Dateisystem-Zugriff — behebt Mount-Namespace-Probleme bei systemd-verwalteten Prozessen

## [v1.1.0] — 2026-05-03

### Hinzugefügt
- `GET /v1/servers/{cid}/superuser` — SuperUser-Passwort aus Container-Logs extrahieren
- `POST /v1/servers/{cid}/superuser/reset` — SuperUser-Passwort zurücksetzen via `mumble-server -supw`
- `GET /v1/servers/{cid}/config` — `mumble_server_config.ini` roh auslesen
- `PUT /v1/servers/{cid}/config` — INI komplett überschreiben + Restart
- `PATCH /v1/servers/{cid}` — Eckdaten (Name, Welcome, MaxUsers, Passwort) aktualisieren
- Sicherheits-Checks: Port und Database-Pfad dürfen nicht geändert werden
- Versions-Konstante `AGENT_VERSION` zentral definiert

## [v1.0.0] — 2026-05-02

### Hinzugefügt
- Initiales Release
- `GET /v1/ping` — Health-Check mit Versions- und Docker-Info
- `POST /v1/servers` — Mumble-Container erstellen
- `DELETE /v1/servers/{cid}` — Container stoppen und entfernen
- `POST /v1/servers/{cid}/start|stop|restart` — Container-Steuerung
- `GET /v1/servers/{cid}/stats` — Online-User, Uptime, Status
- `GET /v1/servers/{cid}/logs` — Container-Logs abrufen
- Bearer-Token-Authentifizierung
- Container-Label-Guard (`mumble-agent.managed`)
- systemd-Unit mit Hardening-Optionen
- Setup-Script für automatische Installation
- AGPLv3-Lizenz
