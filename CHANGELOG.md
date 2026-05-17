# Changelog — mumble-agent

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.

## [v2.1.0] — 2026-05-17

### Hinzugefügt
- **Setup-Wizard**: `setup.sh` fragt beim ersten Ausführen welcher Netzwerk-Modus gewünscht ist (LAN direkt / Proxy auf diesem Host / Zentraler Proxy-LXC) und setzt `MUMBLE_AGENT_HOST` entsprechend
- Abschlussmeldung zeigt je nach Wahl die korrekte Agent-URL mit erkannter interner IP
- Token wird auch bei bestehender `agent.env` am Ende angezeigt

### Geändert
- `MUMBLE_AGENT_HOST` und `MUMBLE_AGENT_PORT` jetzt über `agent.env` konfigurierbar — kein manuelles systemd-Override mehr nötig
- Standard: `MUMBLE_AGENT_HOST=0.0.0.0` (direkt per interner IP erreichbar, kein Reverse-Proxy erforderlich)
- `DOCKER_IMAGE` auf `v1.5.735` gepinnt statt `latest`

### Behoben
- `setup.sh`: `PATH` explizit gesetzt — `useradd`/`usermod` wurden auf Debian 13 nicht gefunden
- `channelnestinglimit`, `allowping`, `username`, `channelname` im PATCH-Handler ergänzt — wurden bisher still ignoriert
- `_recreate_container()`: Rollback-Strategie — alter Container wird umbenannt statt sofort gelöscht; bei Fehler wird er neugestartet
- `GET/PUT /v1/servers/{cid}/config` (Raw-INI) entfernt — war nicht erreichbar und mappte nur 7 von ~30 INI-Keys
- `_patch_ini()` und `ConfigUpdateRequest` entfernt (toter Code)

### Dokumentation
- Netzwerk-Abschnitt mit allen 3 Szenarien (LAN, lokaler Proxy, Proxy-LXC) und ASCII-Diagrammen
- Traefik-Konfigurationsbeispiel ergänzt
- Konfigurationstabelle um `MUMBLE_AGENT_HOST` und `MUMBLE_AGENT_PORT` erweitert
- Voraussetzungen: `git` und `sudo` als Pflichtpakete vor `setup.sh` dokumentiert

---

## [v2.0.0] — 2026-05-16

### Neu — ZeroC ICE Integration

Vollständige Umstellung von Log-/SQLite-Parsing auf die native **ZeroC ICE**-Schnittstelle von Mumble. Der Agent kommuniziert jetzt direkt mit dem laufenden Mumble-Prozess — alle Änderungen werden sofort aktiv ohne Neustart.

### Hinzugefügt
- **ICE-Initialisierung** beim Agent-Start (`_init_ice()`) — lädt `MumbleServer.ice` einmalig
- **ICE-Port-Erkennung** pro Container (`_ice_port_for()`) — liest Port aus `/data/mumble_server_config.ini` via `docker exec grep`, Fallback auf 6502
- **ICE-Verbindung** (`_ice_connect(ice_port)`) — gibt `(comm, server_proxy)` zurück, Caller muss `comm.destroy()` aufrufen
- `GET /v1/servers/{cid}/viewer` — **komplett neu via ICE**: `srv.getTree()` liefert exakte Channel-Struktur und User-Positionen mit Mute/Deaf-Status in Echtzeit (kein Log-Parsing mehr)
- `GET /v1/servers/{cid}/users` — alle verbundenen User via ICE `getUsers()`
- `POST /v1/servers/{cid}/users/{session}/kick` — User kicken via ICE `kickUser()`
- `PATCH /v1/servers/{cid}/users/{session}` — User-Status ändern (mute/deaf) via ICE `setState()`
- `GET /v1/servers/{cid}/channels` — flache Channel-Map via ICE `getChannels()`
- `POST /v1/servers/{cid}/channels` — Channel erstellen via ICE `addChannel()`
- `PATCH /v1/servers/{cid}/channels/{channel_id}` — Channel umbenennen/Position via ICE `setChannelState()`
- `DELETE /v1/servers/{cid}/channels/{channel_id}` — Channel löschen via ICE `removeChannel()`
- `GET /v1/servers/{cid}/acl?channel_id=N` — ACL eines Channels lesen via ICE `getACL()`
- `PUT /v1/servers/{cid}/acl` — ACL setzen via ICE `setACL()` (inkl. Gruppen, inherit_acl)
- `GET /v1/servers/{cid}/bans` — aktive Bans lesen via ICE `getBans()`
- `PUT /v1/servers/{cid}/bans` — Bans komplett setzen via ICE `setBans()`
- `PATCH /v1/servers/{cid}/live` — Einstellungen live ändern via ICE `setConf()` ohne Neustart (Name, Passwort, Max-Nutzer, Begrüßungstext)
- Konverter-Hilfsfunktionen: `_tree_to_dict`, `_user_to_dict`, `_channel_to_dict`, `_acl_to_dict`, `_group_to_dict`, `_ban_to_dict`, `_ice_acl_entry`, `_ice_group`, `_addr_to_bytes`
- Pydantic-Modelle: `LiveSettingsRequest`, `AclEntryModel`, `AclGroupModel`, `SetAclRequest`, `AddChannelRequest`, `UpdateChannelRequest`, `KickRequest`, `UpdateUserRequest`, `BanEntry`, `SetBansRequest`

### Geändert
- `AGENT_VERSION` → `2.0.0`
- `/viewer`-Endpoint: nutzt jetzt ICE `getTree()` statt SQLite + Log-Parsing — exakte Daten in Echtzeit, kein Cache mehr nötig
- `/stats`-Endpoint: User-Zählung via ICE statt Log-Parsing

### Entfernt
- SQLite-basiertes Channel-Parsing (`docker exec sqlite3`)
- Log-basiertes User-Tracking (Connect/Disconnect/Move-Events parsen)

### Voraussetzungen (neu)
- `zeroc-ice` Python-Paket (`pip install zeroc-ice`)
- Mumble-Container mit aktivem ICE-Endpoint (`MUMBLE_CONFIG_ICE=tcp -h 127.0.0.1 -p 6502`)
- Bei mehreren Containern im `--network host` Modus: **jeder Container braucht einen eigenen ICE-Port**

---

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
