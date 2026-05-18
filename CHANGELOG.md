# Changelog — mumble-agent

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.

## [v2.11.0] — 2026-05-18

### Behoben
- `create_server`: Image wird vor `containers.run()` per `run_in_executor` gepullt wenn nicht gecacht — verhindert dass der blockierende Pull die MySQL-Verbindung auf PHP-Seite durch `wait_timeout` tötet

---

## [v2.10.3] — 2026-05-18

### Behoben
- `_recreate_container`: Container-Stop-Timeout 10s → 5s — verhindert dass Upgrade länger als MySQL `wait_timeout` (10s) dauert
- `setup.sh`: `/etc/mumble-agent` und `agent.env` gehören jetzt Gruppe `docker` statt `mumble-agent` — der Prozess läuft via `Group=docker` in der systemd-Unit

---

## [v2.10.2] — 2026-05-18

### Behoben
- `agent.env` Gruppe auf `docker` gesetzt (manuell für bestehende Installationen nötig)

---

## [v2.10.1] — 2026-05-18

### Performance
- `/v1/servers/{cid}/dashboard`: `container.stats()` entfernt — war Hauptbottleneck (2s/Container, serialisiert im Docker-Daemon). CPU/RAM-Werte werden im Dashboard-Überblick nicht live benötigt (Cron liefert historische Daten)
- `_dashboard_sync()` läuft via `run_in_executor` im Thread-Pool — mehrere Container pro Host werden jetzt parallel abgefragt
- `_ice_port_for()` liest ICE-Port direkt aus Container-Env-Var statt via `exec_run` (grep im Container)
- ICE-Verbindungs-Timeout 5000ms → 1500ms

---

## [v2.10.0] — 2026-05-18

### Behoben
- **Mumble 1.6 SuperUser-Passwort**: In Mumble 1.6 heißt der Flag `--set-su-pw` statt `--supw`, und `exec_run` blockiert wegen SQLite-Lock wenn der Server läuft — Lösung: One-Shot-Container (`docker run --rm`) mit überschriebenem Entrypoint setzt das Passwort ohne Konflikt
- `resetSuperUserPassword` nutzt dieselbe Methode und funktioniert damit auch für Mumble 1.6

---

## [v2.9.0] — 2026-05-17

### Behoben
- **ICE-Port-Konflikt**: Jeder Container bekommt einen eindeutigen ICE-Port (`6502 + (mumble_port - 64738)`) statt alle fest auf 6502 — behebt "Address already in use" bei mehreren Servern pro Host
- **Mumble 1.6 SuperUser-Passwort**: `_extract_superuser` Timeout von 30s auf 5s reduziert; wenn kein Passwort in den Logs gefunden wird (Mumble 1.6 loggt es nicht mehr), wird ein zufälliges Passwort generiert und per `mumble-server --supw` gesetzt — behebt "creating"-Status nach PHP-Timeout

---

## [v2.8.0] — 2026-05-17

### Behoben
- `POST /upgrade`: SQLite-DB wird vor dem Recreate auf NULL-Werte in `channel_info` geprüft und bereinigt — behebt Mumble 1.5→1.6 Migrationsfehler (`NOT NULL constraint failed: channel_properties.property_value`)
- Image-Pull wird übersprungen wenn das Image bereits lokal vorhanden ist

---

## [v2.7.0] — 2026-05-17

### Geändert
- `/v1/servers/{cid}/stats` gibt `image` (laufendes Container-Image) zurück

---

## [v2.6.0] — 2026-05-17

### Hinzugefügt
- `POST /v1/image` — schreibt neues `MUMBLE_AGENT_IMAGE` in `agent.env` und startet den Agent neu (Restart=always in systemd)

### Geändert
- systemd-Unit: `Restart=on-failure` → `Restart=always`, `/etc/mumble-agent` zu `ReadWritePaths` ergänzt
- `setup.sh`: `agent.env` auf 0660 — Agent-User darf Datei schreiben

---

## [v2.5.0] — 2026-05-17

### Hinzugefügt
- `POST /v1/servers/{cid}/upgrade` — Container mit aktuellem `DOCKER_IMAGE` neu erstellen (Image-Upgrade mit Rollback)

### Geändert
- Standard-Image auf `mumblevoip/mumble-server:v1.6.870` (Mumble 1.6) aktualisiert

---

## [v2.4.0] — 2026-05-17

### Hinzugefügt
- **Automatischer Update-Check**: Agent prüft beim Start und danach einmal täglich Docker Hub nach dem neuesten `mumblevoip/mumble-server`-Tag
- `/v1/ping` gibt jetzt `latest_image` (neuester verfügbarer Tag) und `update_available` (bool) zurück
- Webinterface zeigt in der Host-Übersicht ein gelbes Badge mit der neuen Version wenn ein Update verfügbar ist

---

## [v2.3.0] — 2026-05-17

### Hinzugefügt
- Setup-Schritt `[7/8]`: Docker-Image wird jetzt bereits beim `setup.sh` gezogen — verhindert HTTP-Timeout beim ersten Server-Anlegen
- `/v1/ping` gibt `mumble_image` zurück — das Webinterface zeigt das aktive Image in der Host-Übersicht an

---

## [v2.2.0] — 2026-05-17

### Behoben
- `setup.sh` Szenario 3 (Proxy-LXC) setzte fälschlich `127.0.0.1` — korrigiert auf `0.0.0.0`
- `setup.sh` Upgrade-Falle: fehlende `MUMBLE_AGENT_HOST`/`MUMBLE_AGENT_PORT` werden bei bestehender `agent.env` automatisch nachgetragen
- Schrittezähler in `setup.sh` war inkonsistent (`[1/7]`…`[7/8]`) — durchgehend `[x/8]`
- `zeroc-ice` fehlte in `requirements.txt` — ICE-Funktionen wurden installiert aber nicht als Abhängigkeit deklariert
- `libssl-dev`, `libbz2-dev` fehlten in `setup.sh` — `zeroc-ice` konnte ohne passendes Wheel nicht gebaut werden
- `MumbleServer.ice` wurde nicht installiert — `setup.sh` lädt sie jetzt von GitHub, mit Fallback-Warnung bei Verbindungsfehler

### Dokumentation
- README: „HTTPS-Requests" → „HTTP/HTTPS-Requests" (internes HTTP offiziell erlaubt)
- README Sicherheit: ehrlicher Hinweis dass `docker`-Gruppe praktisch host-root-fähig ist
- README Firewall-Tabelle: Port 8000 für Szenarien 1 und 3 ergänzt, Szenario-Spalte hinzugefügt

---

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
