# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Andreas P. <https://nfsmw15.de>
"""
mumble-agent v1.3.0

FastAPI-Service zum Verwalten von Mumble-Servern als Docker-Container.

Aenderung v1.2.2: Config/SuperUser-Zugriff ueber docker exec statt
direktem Dateisystem-Zugriff (loest Mount-Namespace-Probleme).
Aenderung v1.3.0: Channel-Viewer via minimalem Mumble-Protokoll-Client.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import docker
from docker.errors import APIError, NotFound
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

AGENT_VERSION = "1.3.0"

AGENT_TOKEN = os.environ.get("MUMBLE_AGENT_TOKEN", "")
DOCKER_IMAGE = os.environ.get("MUMBLE_AGENT_IMAGE", "mumblevoip/mumble-server:latest")
DOCKER_NETWORK = os.environ.get("MUMBLE_AGENT_NETWORK", "host")
DATA_ROOT = os.environ.get("MUMBLE_AGENT_DATA", "/var/lib/mumble-agent")
LABEL_KEY = "mumble-agent.managed"
INI_PATH_IN_CONTAINER = "/data/mumble_server_config.ini"

if not AGENT_TOKEN:
    raise RuntimeError("MUMBLE_AGENT_TOKEN nicht gesetzt.")

docker_client: docker.DockerClient | None = None

@asynccontextmanager
async def lifespan(_: FastAPI):
    global docker_client
    docker_client = docker.from_env()
    os.makedirs(DATA_ROOT, mode=0o750, exist_ok=True)
    yield
    if docker_client is not None:
        docker_client.close()

app = FastAPI(title="mumble-agent", version=AGENT_VERSION, lifespan=lifespan)

def check_token(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(401, detail="missing authorization")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="invalid auth scheme")
    if not secrets.compare_digest(authorization[7:].strip(), AGENT_TOKEN):
        raise HTTPException(403, detail="invalid token")

# --- Modelle ---
class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    port: int = Field(ge=1024, le=65535)
    password: str = Field(default="", max_length=128)
    max_users: int = Field(default=10, ge=1, le=500)
    welcome_text: str = Field(default="", max_length=2000)
    external_id: int = Field(default=0)

class UpdateServerRequest(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=128)
    max_users: int | None = Field(default=None, ge=1, le=500)
    welcome_text: str | None = Field(default=None, max_length=2000)

class SuperUserResetRequest(BaseModel):
    password: str = Field(default="", max_length=128)

class ConfigUpdateRequest(BaseModel):
    content: str = Field(min_length=10, max_length=64000)

# --- Helpers ---
def _container_name(external_id: int, port: int) -> str:
    return f"mumble-{external_id}-{port}" if external_id else f"mumble-{port}"

def _data_dir(name: str) -> str:
    return os.path.join(DATA_ROOT, name.lstrip("/"))

def _config_for(req: CreateServerRequest) -> dict[str, str]:
    cfg = {
        "MUMBLE_CONFIG_REGISTER_NAME": req.name,
        "MUMBLE_CONFIG_USERS": str(req.max_users),
        "MUMBLE_CONFIG_WELCOMETEXT": req.welcome_text or "Willkommen!",
        "MUMBLE_CONFIG_PORT": str(req.port),
    }
    if req.password:
        cfg["MUMBLE_CONFIG_SERVERPASSWORD"] = req.password
    return cfg

def _require_managed(c) -> None:
    if c.labels.get(LABEL_KEY) != "1":
        raise HTTPException(403, detail="container is not managed by mumble-agent")

def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

_SUPERUSER_PW_RE = re.compile(r"Password for 'SuperUser' set to '([^']+)'")

def _search_superuser_in_text(text: str) -> str | None:
    m = _SUPERUSER_PW_RE.search(text)
    return m.group(1) if m else None

def _extract_superuser_from_logs(c, timeout: int = 30) -> str | None:
    """Polls Docker logs for the SuperUser password log line.

    Mumble logs this only on the very first start. After the polling window
    expires, falls back to reading the log file from inside the container in
    case Mumble is writing logs to a file instead of stdout/stderr.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            logs = c.logs(tail=500, timestamps=False).decode("utf-8", errors="replace")
            pw = _search_superuser_in_text(logs)
            if pw:
                return pw
        except Exception:
            pass
        time.sleep(1)
    # Fallback: Mumble may write logs to a file rather than stdout/stderr.
    for log_path in ("/data/mumble-server.log", "/data/murmur.log"):
        try:
            res = c.exec_run(["cat", log_path], demux=False)
            if res.exit_code == 0 and res.output:
                pw = _search_superuser_in_text(res.output.decode("utf-8", errors="replace"))
                if pw:
                    return pw
        except Exception:
            continue
    return None

def _patch_ini(ini_text: str, updates: dict[str, str]) -> str:
    lines = ini_text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    return "\n".join(out) + "\n"

# --- Docker-exec basierte Config/SuperUser Helpers ---
def _read_config_from_container(c) -> str:
    """Liest die INI-Datei direkt aus dem Container via docker exec."""
    try:
        res = c.exec_run(["cat", INI_PATH_IN_CONTAINER], demux=False)
        if res.exit_code != 0:
            raise HTTPException(500, detail=f"cannot read config from container (exit {res.exit_code})")
        return res.output.decode("utf-8", errors="replace")
    except APIError as e:
        raise HTTPException(500, detail=f"docker exec failed: {e}")

def _write_config_to_container(c, content: str) -> None:
    """Schreibt die INI-Datei in den Container via Docker put_archive API.
    
    Verwendet tar-Archiv statt Shell-Escaping — funktioniert zuverlässig
    mit allen Sonderzeichen, Newlines und Umlauten.
    """
    import io
    import tarfile
    data = content.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name="mumble_server_config.ini")
        info.size = len(data)
        info.uid = 10000
        info.gid = 10000
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    try:
        c.put_archive("/data", tar_stream)
    except APIError as e:
        raise HTTPException(500, detail=f"put_archive failed: {e}")

def _read_superuser_from_container(c) -> str | None:
    """Reads the SuperUser password from the full container log history.

    Unlike _extract_superuser_from_logs, this does not poll — it searches all
    Docker logs at once (no tail limit), because the password was only logged
    once at the very first start and may be hundreds of lines back in history.
    Falls back to the log file inside the container.
    """
    try:
        logs = c.logs(timestamps=False).decode("utf-8", errors="replace")
        pw = _search_superuser_in_text(logs)
        if pw:
            return pw
    except Exception:
        pass
    for log_path in ("/data/mumble-server.log", "/data/murmur.log"):
        try:
            res = c.exec_run(["cat", log_path], demux=False)
            if res.exit_code == 0 and res.output:
                pw = _search_superuser_in_text(res.output.decode("utf-8", errors="replace"))
                if pw:
                    return pw
        except Exception:
            continue
    return None

# --- Mumble Channel-Viewer (ohne Client-Connect — SQLite + Log-Parsing) ---
# Channels: direkt aus der SQLite-DB via docker exec (kein Netzwerk-Connect)
# Online-User: aus Docker-Logs parsen (Authenticated/Connection-closed Events)

# Cache: {container_id -> (timestamp, result)}
_viewer_cache: dict[str, tuple[float, dict]] = {}
_VIEWER_CACHE_TTL = 30  # Sekunden

# Log-Regex für User-Events — kein Mumble-Client-Connect nötig
_LOG_AUTH_RE = re.compile(r'=> <(\d+):([^(]+)\(\d+\)> Authenticated')
_LOG_DISC_RE = re.compile(r'=> <(\d+):[^(]+\(\d+\)> (?:Connection closed|Timeout|Disconnecting)')
_LOG_BOOT_RE = re.compile(r'Booting servers|Generating new tables')

def _get_channels_from_db(c) -> dict[int, dict]:
    """Liest Channel-Struktur direkt aus der SQLite-DB — kein Netzwerk-Connect."""
    try:
        res = c.exec_run(
            ["sqlite3", "-readonly", "/data/mumble-server.sqlite",
             "SELECT channel_id, parent_id, name FROM channels WHERE server_id=1;"],
            demux=False
        )
        if res.exit_code != 0 or not res.output:
            return {}
        channels: dict[int, dict] = {}
        for line in res.output.decode("utf-8", errors="replace").strip().splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            cid = int(parts[0])
            parent = int(parts[1]) if parts[1].strip() else None
            name = parts[2]
            channels[cid] = {"id": cid, "parent": parent, "name": name}
        return channels
    except Exception:
        return {}

def _get_online_users_from_logs(c) -> list[str]:
    """Parst Docker-Logs auf Authenticated/Disconnect-Events — kein Netzwerk-Connect."""
    try:
        logs = c.logs(timestamps=False).decode("utf-8", errors="replace")
    except Exception:
        return []
    lines = logs.splitlines()
    # Ab dem letzten Server-Start lesen (davor sind alle User weg)
    last_boot = 0
    for i, line in enumerate(lines):
        if _LOG_BOOT_RE.search(line):
            last_boot = i
    online: dict[int, str] = {}
    for line in lines[last_boot:]:
        m = _LOG_AUTH_RE.search(line)
        if m:
            online[int(m.group(1))] = m.group(2).strip()
            continue
        m = _LOG_DISC_RE.search(line)
        if m:
            online.pop(int(m.group(1)), None)
    return list(online.values())

def get_mumble_viewer(c) -> dict:
    """Channel-Baum + Online-User — vollständig ohne Mumble-Client-Connect."""
    now = time.time()
    if c.id in _viewer_cache:
        ts, cached = _viewer_cache[c.id]
        if now - ts < _VIEWER_CACHE_TTL:
            return cached

    channels = _get_channels_from_db(c)
    online_users = _get_online_users_from_logs(c)

    def build_tree(parent_id: int | None) -> list:
        children = []
        for ch in sorted(channels.values(), key=lambda x: x["name"].lower()):
            if ch["parent"] == parent_id and ch["id"] != 0:
                children.append({
                    "id": ch["id"],
                    "name": ch["name"],
                    "users": [],
                    "children": build_tree(ch["id"]),
                })
        return children

    root_name = channels.get(0, {}).get("name") or "Root"
    root = {"id": 0, "name": root_name, "users": online_users, "children": build_tree(0)}
    result = {"ok": True, "channels": root, "user_count": len(online_users)}
    _viewer_cache[c.id] = (now, result)
    return result

# --- Endpoints ---
@app.get("/v1/ping")
async def ping(authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    return {
        "ok": True, "agent": "mumble-agent", "version": AGENT_VERSION,
        "time": int(time.time()),
        "docker_version": docker_client.version().get("Version") if docker_client else None,
    }

@app.post("/v1/servers")
async def create_server(req: CreateServerRequest, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    name = _container_name(req.external_id, req.port)
    data_dir = _data_dir(name)
    os.makedirs(data_dir, mode=0o750, exist_ok=True)
    try:
        c = docker_client.containers.run(
            image=DOCKER_IMAGE, name=name, detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=_config_for(req),
            volumes={data_dir: {"bind": "/data", "mode": "rw"}},
            ports=(None if DOCKER_NETWORK == "host"
                   else {f"{req.port}/tcp": req.port, f"{req.port}/udp": req.port}),
            network_mode=DOCKER_NETWORK if DOCKER_NETWORK == "host" else None,
            labels={
                LABEL_KEY: "1",
                "mumble-agent.external_id": str(req.external_id),
                "mumble-agent.port": str(req.port),
                "mumble-agent.name": req.name,
            },
        )
    except APIError as e:
        raise HTTPException(500, detail=f"docker error: {e.explanation or str(e)}")
    superuser_pw = _extract_superuser_from_logs(c, timeout=30)
    return {"ok": True, "container_id": c.id, "name": c.name, "superuser_password": superuser_pw}

@app.delete("/v1/servers/{cid}")
async def delete_server(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        c.stop(timeout=10)
    except NotFound:
        return {"ok": True, "note": "container already gone"}
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    try:
        c.remove(force=True)
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True}

@app.post("/v1/servers/{cid}/start")
async def start_server(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        c.start()
    except NotFound:
        raise HTTPException(404, detail="container not found")
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True, "status": c.status}

@app.post("/v1/servers/{cid}/stop")
async def stop_server(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        c.stop(timeout=10)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True}

@app.post("/v1/servers/{cid}/restart")
async def restart_server(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        c.restart(timeout=10)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True}

@app.get("/v1/servers/{cid}/stats")
async def server_stats(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    started_at = c.attrs.get("State", {}).get("StartedAt", "")
    uptime = 0
    if started_at and c.status == "running":
        try:
            ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            uptime = 0
    online = 0
    try:
        port = c.labels.get("mumble-agent.port", "")
        if port.isdigit():
            ss = subprocess.run(["ss", "-tn", f"sport = :{port}"],
                                capture_output=True, text=True, timeout=3)
            if ss.returncode == 0:
                online = max(0, len(ss.stdout.strip().splitlines()) - 1)
    except Exception:
        pass
    return {"ok": True, "online": online, "uptime": uptime,
            "status": c.status, "started_at": started_at}

@app.get("/v1/servers/{cid}/logs")
async def server_logs(cid: str, tail: int = 200, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    tail = max(10, min(2000, int(tail)))
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        log = c.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except NotFound:
        raise HTTPException(404, detail="container not found")
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True, "log": log, "tail": tail}

@app.patch("/v1/servers/{cid}")
async def update_server(cid: str, req: UpdateServerRequest, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Aktualisiert Server-Einstellungen durch Container-Recreate.
    
    Das Mumble-Docker-Image generiert die INI bei jedem Start aus den
    ENV-Variablen. Deshalb reicht INI-patchen + Restart nicht — der
    Container muss mit neuen ENV-Variablen neu erstellt werden.
    
    Das Volume (/data) bleibt erhalten: SQLite-DB, Zertifikate und
    registrierte User gehen nicht verloren.
    """
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")

    # Aktuelle Config aus den bestehenden ENV-Variablen und Labels lesen
    old_env = {}
    for item in (c.attrs.get("Config", {}).get("Env") or []):
        if "=" in item:
            k, v = item.split("=", 1)
            old_env[k] = v

    old_labels = c.labels.copy()
    old_name = c.name.lstrip("/")
    old_port = old_labels.get("mumble-agent.port", "")
    old_ext_id = old_labels.get("mumble-agent.external_id", "0")
    data_dir = _data_dir(old_name)

    # Neue ENV-Werte zusammenbauen (altes übernehmen, mit Updates überschreiben)
    new_env = dict(old_env)
    updated = []
    if req.name is not None:
        new_env["MUMBLE_CONFIG_REGISTER_NAME"] = req.name
        old_labels["mumble-agent.name"] = req.name
        updated.append("name")
    if req.welcome_text is not None:
        new_env["MUMBLE_CONFIG_WELCOMETEXT"] = req.welcome_text
        updated.append("welcome_text")
    if req.max_users is not None:
        new_env["MUMBLE_CONFIG_USERS"] = str(req.max_users)
        updated.append("max_users")
    if req.password is not None:
        if req.password:
            new_env["MUMBLE_CONFIG_SERVERPASSWORD"] = req.password
        else:
            new_env.pop("MUMBLE_CONFIG_SERVERPASSWORD", None)
        updated.append("password")

    if not updated:
        return {"ok": True, "note": "nothing to update"}

    # Container stoppen und entfernen
    try:
        c.stop(timeout=10)
        c.remove(force=True)
    except APIError as e:
        raise HTTPException(500, detail=f"cannot remove old container: {e}")

    # Neuen Container mit denselben Einstellungen aber neuen ENV-Werten erstellen
    port_int = int(old_port) if old_port.isdigit() else 64738
    try:
        new_c = docker_client.containers.run(
            image=DOCKER_IMAGE, name=old_name, detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=new_env,
            volumes={data_dir: {"bind": "/data", "mode": "rw"}},
            ports=(None if DOCKER_NETWORK == "host"
                   else {f"{port_int}/tcp": port_int, f"{port_int}/udp": port_int}),
            network_mode=DOCKER_NETWORK if DOCKER_NETWORK == "host" else None,
            labels=old_labels,
        )
    except APIError as e:
        raise HTTPException(500, detail=f"cannot recreate container: {e}")

    return {
        "ok": True,
        "updated_fields": updated,
        "container_id": new_c.id,
        "name": new_c.name,
    }

# --- SuperUser ---
@app.get("/v1/servers/{cid}/superuser")
async def get_superuser(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    pw = _extract_superuser_from_logs(c, timeout=3)
    return {"ok": True, "superuser_password": pw}

@app.post("/v1/servers/{cid}/superuser/reset")
async def reset_superuser(cid: str, req: SuperUserResetRequest, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    new_pw = req.password.strip() or _generate_password(16)
    try:
        exec_res = c.exec_run(
            ["/usr/bin/mumble-server", "-ini", INI_PATH_IN_CONTAINER, "-supw", new_pw],
            user="10000:10000",
        )
        if exec_res.exit_code != 0:
            output = exec_res.output.decode("utf-8", errors="replace")
            raise HTTPException(500, detail=f"mumble-server -supw failed (exit {exec_res.exit_code}): {output}")
    except APIError as e:
        raise HTTPException(500, detail=f"docker exec failed: {e}")
    return {"ok": True, "superuser_password": new_pw}

# --- Config (Raw-INI) via docker exec ---
@app.get("/v1/servers/{cid}/config")
async def get_config(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    content = _read_config_from_container(c)
    return {"ok": True, "content": content}

@app.put("/v1/servers/{cid}/config")
async def put_config(cid: str, req: ConfigUpdateRequest, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Ueberschreibt die Server-Config komplett durch Container-Recreate.
    
    Parst die INI-Felder aus dem Content und setzt die passenden
    MUMBLE_CONFIG_* ENV-Variablen fuer den neuen Container.
    """
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")

    current_port = c.labels.get("mumble-agent.port", "")
    port_match = re.search(r"^\s*port\s*=\s*(\d+)", req.content, re.MULTILINE)
    if port_match and port_match.group(1) != current_port:
        raise HTTPException(400, detail=f"port cannot be changed (currently {current_port})")
    db_match = re.search(r"^\s*database\s*=\s*(.+)$", req.content, re.MULTILINE)
    if db_match and db_match.group(1).strip() not in ("/data/mumble-server.sqlite", ""):
        raise HTTPException(400, detail="database path cannot be changed")

    # INI-Felder -> ENV-Variablen Mapping
    ini_to_env = {
        "registerName": "MUMBLE_CONFIG_REGISTER_NAME",
        "users": "MUMBLE_CONFIG_USERS",
        "welcometext": "MUMBLE_CONFIG_WELCOMETEXT",
        "port": "MUMBLE_CONFIG_PORT",
        "serverpassword": "MUMBLE_CONFIG_SERVERPASSWORD",
        "bandwidth": "MUMBLE_CONFIG_BANDWIDTH",
        "timeout": "MUMBLE_CONFIG_TIMEOUT",
        "opusthreshold": "MUMBLE_CONFIG_OPUSTHRESHOLD",
        "textmessagelength": "MUMBLE_CONFIG_TEXTMESSAGELENGTH",
        "allowhtml": "MUMBLE_CONFIG_ALLOWHTML",
    }

    # Alte ENV-Variablen uebernehmen, dann mit Werten aus dem neuen INI ueberschreiben
    old_env = {}
    for item in (c.attrs.get("Config", {}).get("Env") or []):
        if "=" in item:
            k, v = item.split("=", 1)
            old_env[k] = v

    new_env = dict(old_env)
    for line in req.content.splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if m:
            ini_key = m.group(1)
            ini_val = m.group(2).strip()
            if ini_key in ini_to_env:
                if ini_val:
                    new_env[ini_to_env[ini_key]] = ini_val
                else:
                    new_env.pop(ini_to_env[ini_key], None)

    old_labels = c.labels.copy()
    old_name = c.name.lstrip("/")
    data_dir = _data_dir(old_name)
    port_int = int(current_port) if current_port.isdigit() else 64738

    # Container recreate
    try:
        c.stop(timeout=10)
        c.remove(force=True)
    except APIError as e:
        raise HTTPException(500, detail=f"cannot remove old container: {e}")

    try:
        new_c = docker_client.containers.run(
            image=DOCKER_IMAGE, name=old_name, detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=new_env,
            volumes={data_dir: {"bind": "/data", "mode": "rw"}},
            ports=(None if DOCKER_NETWORK == "host"
                   else {f"{port_int}/tcp": port_int, f"{port_int}/udp": port_int}),
            network_mode=DOCKER_NETWORK if DOCKER_NETWORK == "host" else None,
            labels=old_labels,
        )
    except APIError as e:
        raise HTTPException(500, detail=f"cannot recreate container: {e}")

    return {"ok": True, "size": len(req.content), "container_id": new_c.id}

@app.get("/v1/servers/{cid}/viewer")
async def channel_viewer(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Gibt Channel-Baum + Online-User zurück — ohne Mumble-Client-Connect."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    result = get_mumble_viewer(c)
    if not result["ok"]:
        raise HTTPException(503, detail=result.get("error", "viewer unavailable"))
    return result

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})
