# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Andreas P. <https://nfsmw15.de>
"""
mumble-agent v2.0.0

FastAPI-Service zum Verwalten von Mumble-Servern als Docker-Container.

v2.0.0: ZeroC-ICE-Integration fuer live Channel/User/ACL/Ban-Verwaltung.
        Viewer, ACL, Channels, Kick, Bans via ICE ohne Server-Neustart.
v1.3.0: Channel-Viewer via SQLite + Log-Parsing.
v1.2.2: Config/SuperUser via docker exec.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import string
import shutil
import sys
import time
import urllib.request
from contextlib import asynccontextmanager, redirect_stderr
from datetime import datetime, timezone
from typing import Any

import docker
from docker.errors import APIError, NotFound
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

AGENT_VERSION = "2.8.0"

AGENT_TOKEN    = os.environ.get("MUMBLE_AGENT_TOKEN", "")
DOCKER_IMAGE   = os.environ.get("MUMBLE_AGENT_IMAGE", "mumblevoip/mumble-server:v1.6.870")
DOCKER_NETWORK = os.environ.get("MUMBLE_AGENT_NETWORK", "host")
DATA_ROOT      = os.environ.get("MUMBLE_AGENT_DATA", "/var/lib/mumble-agent")
LABEL_KEY      = "mumble-agent.managed"
INI_PATH       = "/data/mumble_server_config.ini"

if not AGENT_TOKEN:
    raise RuntimeError("MUMBLE_AGENT_TOKEN nicht gesetzt.")

docker_client: docker.DockerClient | None = None

# ── Update-Check ──────────────────────────────────────────────────────────────
_UPDATE_INTERVAL = 24 * 3600  # einmal täglich
_update_cache: dict[str, Any] = {"latest": None, "checked_at": 0}

def _fetch_latest_image() -> str | None:
    """Fragt Docker Hub nach dem neuesten versionierten Tag von mumblevoip/mumble-server."""
    url = ("https://hub.docker.com/v2/repositories/mumblevoip/mumble-server"
           "/tags?page_size=50&ordering=last_updated")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        tag_re = re.compile(r'^v\d+\.\d+\.\d+$')
        for entry in data.get("results", []):
            tag = entry.get("name", "")
            if tag_re.match(tag):
                return f"mumblevoip/mumble-server:{tag}"
    except Exception as e:
        print(f"[mumble-agent] Update-Check fehlgeschlagen: {e}", flush=True)
    return None

async def _update_check_loop() -> None:
    while True:
        latest = await asyncio.get_event_loop().run_in_executor(None, _fetch_latest_image)
        if latest:
            _update_cache["latest"]     = latest
            _update_cache["checked_at"] = int(time.time())
            print(f"[mumble-agent] Neuestes Image: {latest}", flush=True)
        await asyncio.sleep(_UPDATE_INTERVAL)

# ── ICE-Setup ────────────────────────────────────────────────────────────────
_MumbleServer = None   # nach _init_ice() gesetzt
_ICE_SLICE    = os.path.join(os.path.dirname(__file__), "MumbleServer.ice")

def _init_ice() -> None:
    global _MumbleServer
    if not os.path.exists(_ICE_SLICE):
        print(f"[mumble-agent] MumbleServer.ice nicht gefunden: {_ICE_SLICE}", flush=True)
        return
    try:
        import Ice
        # Slice-Include-Pfad ermitteln
        site_pkg = os.path.dirname(os.path.dirname(Ice.__file__))
        include  = os.path.join(site_pkg, "slice")
        if not os.path.isdir(include):
            include = site_pkg
        # loadSlice-Warnungen (veraltete Slice-Syntax) unterdrücken
        with redirect_stderr(io.StringIO()):
            Ice.loadSlice(["-I", include, _ICE_SLICE])
        import MumbleServer as _ms
        _MumbleServer = _ms
        print(f"[mumble-agent] ICE bereit (zeroc-ice, Slice={_ICE_SLICE})", flush=True)
    except Exception as e:
        print(f"[mumble-agent] ICE nicht verfügbar: {e}", flush=True)

@asynccontextmanager
async def lifespan(_: FastAPI):
    global docker_client
    _init_ice()
    docker_client = docker.from_env()
    os.makedirs(DATA_ROOT, mode=0o750, exist_ok=True)
    task = asyncio.create_task(_update_check_loop())
    yield
    task.cancel()
    if docker_client is not None:
        docker_client.close()

app = FastAPI(title="mumble-agent", version=AGENT_VERSION, lifespan=lifespan)


def _ice_port_for(c) -> int:
    """ICE-Port aus Container-Config lesen; Fallback = 6502 (Mumble-Standard)."""
    try:
        # Lese die ice=... Zeile aus der INI — unterstützt ice=tcp... und ice="tcp..."
        res = c.exec_run(
            ["sh", "-c",
             "grep '^ice=' /data/mumble_server_config.ini | grep -oP '(?<=-p )\\d+'"],
            demux=False,
        )
        if res.exit_code == 0 and res.output:
            port = int(res.output.decode().strip())
            if 1 <= port <= 65535:
                return port
    except Exception:
        pass
    return 6502


def _ice_connect(ice_port: int):
    """
    Verbindet per ICE mit Murmur, gibt (communicator, ServerPrx) zurück.
    Caller muss comm.destroy() aufrufen.
    Raises HTTPException(503) falls ICE nicht verfügbar.
    """
    if _MumbleServer is None:
        raise HTTPException(503, detail="ICE nicht initialisiert (MumbleServer.ice fehlt?)")
    import Ice
    comm = Ice.initialize()
    try:
        base = comm.stringToProxy(f"Meta:tcp -h 127.0.0.1 -p {ice_port} -t 5000")
        meta = _MumbleServer.MetaPrx.checkedCast(base)
        if not meta:
            comm.destroy()
            raise HTTPException(503, detail="ICE nicht erreichbar – ICE in Mumble-Config aktivieren")
        servers = meta.getBootedServers()
        if not servers:
            comm.destroy()
            raise HTTPException(503, detail="Mumble-Server nicht gebootet")
        return comm, servers[0]
    except Ice.ConnectionRefusedException:
        comm.destroy()
        raise HTTPException(503, detail=f"ICE-Verbindung verweigert (Port {ice_port}) – ICE aktivieren")
    except Ice.TimeoutException:
        comm.destroy()
        raise HTTPException(503, detail=f"ICE-Timeout (Port {ice_port}) – Server läuft oder ICE nicht aktiv?")
    except HTTPException:
        raise
    except Exception as e:
        comm.destroy()
        raise HTTPException(503, detail=f"ICE-Fehler: {e}")


# ── Pydantic-Modelle ──────────────────────────────────────────────────────────

class CreateServerRequest(BaseModel):
    name: str         = Field(min_length=1, max_length=64)
    port: int         = Field(ge=1024, le=65535)
    password: str     = Field(default="", max_length=128)
    max_users: int    = Field(default=10, ge=1, le=500)
    welcome_text: str = Field(default="", max_length=2000)
    external_id: int  = Field(default=0)

class UpdateServerRequest(BaseModel):
    name: str | None          = Field(default=None, max_length=64)
    password: str | None      = Field(default=None, max_length=128)
    max_users: int | None     = Field(default=None, ge=1, le=500)
    welcome_text: str | None  = Field(default=None, max_length=2000)
    bandwidth: int | None     = Field(default=None, ge=8000, le=1000000)
    timeout: int | None       = Field(default=None, ge=5, le=3600)
    textmessagelength: int | None  = Field(default=None, ge=0, le=100000)
    imagemessagelength: int | None = Field(default=None, ge=0, le=10485760)
    allowhtml: bool | None    = Field(default=None)
    opusthreshold: int | None = Field(default=None, ge=0, le=100)
    defaultchannel: int | None = Field(default=None, ge=0)
    rememberchannel: bool | None = Field(default=None)
    certrequired: bool | None = Field(default=None)
    usersperchannel: int | None = Field(default=None, ge=0, le=500)
    register_name: str | None    = Field(default=None, max_length=255)
    register_password: str | None = Field(default=None, max_length=255)
    register_url: str | None     = Field(default=None, max_length=512)
    register_hostname: str | None = Field(default=None, max_length=255)
    register_location: str | None = Field(default=None, max_length=64)
    autoban_attempts: int | None  = Field(default=None, ge=0)
    autoban_timeframe: int | None = Field(default=None, ge=0)
    autoban_time: int | None      = Field(default=None, ge=0)
    sendversion: bool | None     = Field(default=None)
    bonjour: bool | None         = Field(default=None)
    suggestversion: str | None   = Field(default=None, max_length=32)
    suggestpositional: bool | None    = Field(default=None)
    suggestpushtotalk: bool | None    = Field(default=None)
    channelnestinglimit: int | None   = Field(default=None, ge=0, le=50)
    allowping: bool | None            = Field(default=None)
    username: str | None              = Field(default=None, max_length=512)
    channelname: str | None           = Field(default=None, max_length=512)

class LiveSettingsRequest(BaseModel):
    name: str | None         = Field(default=None, max_length=64)
    password: str | None     = Field(default=None, max_length=128)
    max_users: int | None    = Field(default=None, ge=1, le=500)
    welcome_text: str | None = Field(default=None, max_length=2000)

class CertificateRequest(BaseModel):
    cert: str = Field(min_length=50, max_length=65536)
    key: str  = Field(min_length=50, max_length=65536)

class SuperUserResetRequest(BaseModel):
    password: str = Field(default="", max_length=128)

class AclEntryModel(BaseModel):
    user_id: int | None = Field(default=None)
    group: str | None   = Field(default=None, max_length=64)
    apply_here: bool    = Field(default=True)
    apply_sub: bool     = Field(default=True)
    grant: int          = Field(default=0, ge=0)
    deny: int           = Field(default=0, ge=0)

class AclGroupModel(BaseModel):
    name: str                  = Field(min_length=1, max_length=64)
    inherit: bool              = Field(default=True)
    inheritable: bool          = Field(default=True)
    members_add: list[int]     = Field(default_factory=list)
    members_remove: list[int]  = Field(default_factory=list)

class SetAclRequest(BaseModel):
    channel_id: int              = Field(ge=0)
    inherit_acl: bool            = Field(default=True)
    acl: list[AclEntryModel]     = Field(default_factory=list)
    groups: list[AclGroupModel]  = Field(default_factory=list)

class AddChannelRequest(BaseModel):
    name: str   = Field(min_length=1, max_length=64)
    parent: int = Field(default=0, ge=0)

class UpdateChannelRequest(BaseModel):
    name: str | None        = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    position: int | None    = Field(default=None)
    parent: int | None      = Field(default=None, ge=0)

class KickRequest(BaseModel):
    reason: str = Field(default="", max_length=256)

class UpdateUserRequest(BaseModel):
    mute: bool | None       = Field(default=None)
    deaf: bool | None       = Field(default=None)
    channel: int | None     = Field(default=None, ge=0)

class BanEntry(BaseModel):
    address: str    = Field(description="IPv4 oder IPv6-Adresse")
    bits: int       = Field(default=32, ge=1, le=128)
    name: str       = Field(default="")
    reason: str     = Field(default="")
    duration: int   = Field(default=0, ge=0, description="Sekunden, 0=permanent")

class SetBansRequest(BaseModel):
    bans: list[BanEntry] = Field(default_factory=list)

class UpdateImageRequest(BaseModel):
    image: str = Field(..., min_length=1, max_length=256)


# ── Docker-Helpers ────────────────────────────────────────────────────────────

def check_token(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(401, detail="missing authorization")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="invalid auth scheme")
    if not secrets.compare_digest(authorization[7:].strip(), AGENT_TOKEN):
        raise HTTPException(403, detail="invalid token")

def _container_name(external_id: int, port: int) -> str:
    return f"mumble-{external_id}-{port}" if external_id else f"mumble-{port}"

def _data_dir(name: str) -> str:
    return os.path.join(DATA_ROOT, name.lstrip("/"))

def _require_managed(c) -> None:
    if c.labels.get(LABEL_KEY) != "1":
        raise HTTPException(403, detail="container is not managed by mumble-agent")

def _generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))

def _config_for(req: CreateServerRequest) -> dict[str, str]:
    port = req.port
    cfg = {
        "MUMBLE_CONFIG_REGISTER_NAME": req.name,
        "MUMBLE_CONFIG_USERS":         str(req.max_users),
        "MUMBLE_CONFIG_WELCOMETEXT":   req.welcome_text or "Willkommen!",
        "MUMBLE_CONFIG_PORT":          str(port),
    }
    if req.password:
        cfg["MUMBLE_CONFIG_SERVERPASSWORD"] = req.password
    return cfg

_SUPERUSER_PW_RE = re.compile(r"Password for 'SuperUser' set to '([^']+)'")

def _search_superuser(text: str) -> str | None:
    m = _SUPERUSER_PW_RE.search(text)
    return m.group(1) if m else None

def _extract_superuser(c, timeout: int = 30) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            logs = c.logs(tail=500).decode("utf-8", errors="replace")
            pw = _search_superuser(logs)
            if pw:
                return pw
        except Exception:
            pass
        time.sleep(1)
    for path in ("/data/mumble-server.log", "/data/murmur.log"):
        try:
            res = c.exec_run(["cat", path], demux=False)
            if res.exit_code == 0 and res.output:
                pw = _search_superuser(res.output.decode("utf-8", errors="replace"))
                if pw:
                    return pw
        except Exception:
            continue
    return None

def _read_superuser(c) -> str | None:
    try:
        logs = c.logs().decode("utf-8", errors="replace")
        pw = _search_superuser(logs)
        if pw:
            return pw
    except Exception:
        pass
    for path in ("/data/mumble-server.log", "/data/murmur.log"):
        try:
            res = c.exec_run(["cat", path], demux=False)
            if res.exit_code == 0 and res.output:
                pw = _search_superuser(res.output.decode("utf-8", errors="replace"))
                if pw:
                    return pw
        except Exception:
            continue
    return None

def _read_config(c) -> str:
    res = c.exec_run(["cat", INI_PATH], demux=False)
    if res.exit_code != 0:
        raise HTTPException(500, detail="cannot read config from container")
    return res.output.decode("utf-8", errors="replace")

def _write_config(c, content: str) -> None:
    import tarfile
    data = content.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="mumble_server_config.ini")
        info.size = len(data)
        info.uid = info.gid = 10000
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    try:
        c.put_archive("/data", buf)
    except APIError as e:
        raise HTTPException(500, detail=f"put_archive failed: {e}")

_SETTINGS_MAP = {
    "MUMBLE_CONFIG_BANDWIDTH":         "bandwidth",
    "MUMBLE_CONFIG_TIMEOUT":           "timeout",
    "MUMBLE_CONFIG_TEXTMESSAGELENGTH": "textmessagelength",
    "MUMBLE_CONFIG_IMAGEMESSAGELENGTH":"imagemessagelength",
    "MUMBLE_CONFIG_ALLOWHTML":         "allowhtml",
    "MUMBLE_CONFIG_OPUSTHRESHOLD":     "opusthreshold",
    "MUMBLE_CONFIG_DEFAULTCHANNEL":    "defaultchannel",
    "MUMBLE_CONFIG_REMEMBERCHANNEL":   "rememberchannel",
    "MUMBLE_CONFIG_CERTREQUIRED":      "certrequired",
    "MUMBLE_CONFIG_USERSPERCHANNEL":   "usersperchannel",
    "MUMBLE_CONFIG_REGISTERNAME":      "register_name",
    "MUMBLE_CONFIG_REGISTERPASSWORD":  "register_password",
    "MUMBLE_CONFIG_REGISTERURL":       "register_url",
    "MUMBLE_CONFIG_REGISTERHOSTNAME":  "register_hostname",
    "MUMBLE_CONFIG_REGISTERLOCATION":  "register_location",
    "MUMBLE_CONFIG_AUTOBANATTEMPTS":   "autoban_attempts",
    "MUMBLE_CONFIG_AUTOBANTIMEFRAME":  "autoban_timeframe",
    "MUMBLE_CONFIG_AUTOBANTIME":       "autoban_time",
    "MUMBLE_CONFIG_SENDVERSION":       "sendversion",
    "MUMBLE_CONFIG_BONJOUR":           "bonjour",
    "MUMBLE_CONFIG_SUGGESTVERSION":    "suggestversion",
    "MUMBLE_CONFIG_SUGGESTPOSITIONAL":   "suggestpositional",
    "MUMBLE_CONFIG_SUGGESTPUSHTOTALK":   "suggestpushtotalk",
    "MUMBLE_CONFIG_CHANNELNESTINGLIMIT": "channelnestinglimit",
    "MUMBLE_CONFIG_ALLOWPING":           "allowping",
    "MUMBLE_CONFIG_USERNAME":            "username",
    "MUMBLE_CONFIG_CHANNELNAME":         "channelname",
    "MUMBLE_CONFIG_SSLCERT":             "ssl_cert",
    "MUMBLE_CONFIG_SSLKEY":              "ssl_key",
}

def _env_map(c) -> dict[str, str]:
    env = {}
    for item in (c.attrs.get("Config", {}).get("Env") or []):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    return env

def _recreate_container(c, new_env: dict[str, str],
                        new_labels: dict | None = None) -> docker.models.containers.Container:
    old_labels  = c.labels.copy()
    old_name    = c.name.lstrip("/")
    old_port    = old_labels.get("mumble-agent.port", "")
    data_dir    = _data_dir(old_name)
    port_int    = int(old_port) if old_port.isdigit() else 64738
    use_labels  = new_labels if new_labels is not None else old_labels
    backup_name = old_name + "_backup"

    def _run(name: str, env: dict, labels: dict):
        return docker_client.containers.run(
            image=DOCKER_IMAGE, name=name, detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=env,
            volumes={data_dir: {"bind": "/data", "mode": "rw"}},
            ports=(None if DOCKER_NETWORK == "host"
                   else {f"{port_int}/tcp": port_int, f"{port_int}/udp": port_int}),
            network_mode=DOCKER_NETWORK if DOCKER_NETWORK == "host" else None,
            labels=labels,
        )

    # Rename old container so new one can take the canonical name
    try:
        c.rename(backup_name)
    except APIError as e:
        raise HTTPException(500, detail=f"rename failed: {e}")

    try:
        c.stop(timeout=10)
    except APIError as e:
        try: c.rename(old_name)
        except Exception: pass
        raise HTTPException(500, detail=f"stop failed: {e}")

    try:
        new_c = _run(old_name, new_env, use_labels)
    except APIError as e:
        # Rollback: restart old container under its original name
        try:
            c.rename(old_name)
            c.start()
        except Exception:
            pass  # best-effort; data intact, manual recovery possible
        raise HTTPException(500, detail=f"container create failed, old container restored: {e}")

    # Success — drop backup
    try:
        c.remove(force=True)
    except APIError:
        pass  # non-critical, can be cleaned up manually
    return new_c


# ── ICE-Daten-Konverter ───────────────────────────────────────────────────────

def _tree_to_dict(tree) -> dict:
    return {
        "id":       tree.c.id,
        "name":     tree.c.name,
        "users":    [_user_to_dict(u) for u in tree.users],
        "children": [_tree_to_dict(ch) for ch in tree.children],
    }

def _user_to_dict(u) -> dict:
    return {
        "session":      u.session,
        "name":         u.name,
        "userid":       u.userid,
        "channel":      u.channel,
        "mute":         u.mute,
        "deaf":         u.deaf,
        "self_mute":    u.selfMute,
        "self_deaf":    u.selfDeaf,
        "recording":    u.recording,
        "idle":         u.idlesecs,
        "bytespersec":  u.bytespersec,
        "udp_ping":     round(u.udpPing, 1),
        "tcp_ping":     round(u.tcpPing, 1),
        "tcp_only":     u.tcponly,
        "online_secs":  u.onlinesecs,
        "os":           u.os,
        "os_version":   u.osversion,
        "version":      str(u.version) if u.version else '',
    }

def _channel_to_dict(ch) -> dict:
    return {
        "id":          ch.id,
        "name":        ch.name,
        "parent":      ch.parent,
        "description": ch.description,
        "temporary":   ch.temporary,
        "position":    ch.position,
        "links":       list(ch.links),
    }

def _acl_to_dict(a) -> dict:
    return {
        "user_id":    None if a.userid == -1 else a.userid,
        "group":      a.group if a.group else None,
        "apply_here": a.applyHere,
        "apply_sub":  a.applySubs,
        "inherited":  a.inherited,
        "grant":      a.allow,
        "deny":       a.deny,
    }

def _group_to_dict(g) -> dict:
    return {
        "name":           g.name,
        "inherit":        g.inherit,
        "inheritable":    g.inheritable,
        "inherited":      g.inherited,
        "members_add":    list(g.add),
        "members_remove": list(g.remove),
        "members":        list(g.members),
    }

def _ban_to_dict(b) -> dict:
    import socket, struct
    try:
        raw = bytes(b.address)
        # IPv4-mapped IPv6 → IPv4
        if len(raw) == 16 and raw[:12] == b'\x00'*10 + b'\xff\xff':
            addr = socket.inet_ntop(socket.AF_INET, raw[12:])
        elif len(raw) == 4:
            addr = socket.inet_ntop(socket.AF_INET, raw)
        else:
            addr = socket.inet_ntop(socket.AF_INET6, raw)
    except Exception:
        addr = str(bytes(b.address))
    return {
        "address":  addr,
        "bits":     b.bits,
        "name":     b.name,
        "hash":     b.hash,
        "reason":   b.reason,
        "start":    b.start,
        "duration": b.duration,
    }

def _ice_acl_entry(a: AclEntryModel):
    acl = _MumbleServer.ACL()
    acl.userid    = -1 if a.user_id is None else a.user_id
    acl.group     = a.group or ""
    acl.applyHere = a.apply_here
    acl.applySubs = a.apply_sub
    acl.inherited = False
    acl.allow     = a.grant
    acl.deny      = a.deny
    return acl

def _ice_group(g: AclGroupModel):
    grp = _MumbleServer.Group()
    grp.name        = g.name
    grp.inherit     = g.inherit
    grp.inheritable = g.inheritable
    grp.inherited   = False
    grp.add         = list(g.members_add)
    grp.remove      = list(g.members_remove)
    grp.members     = []
    return grp

def _addr_to_bytes(addr: str) -> list[int]:
    import socket
    try:
        # Versuche IPv4
        raw = socket.inet_pton(socket.AF_INET, addr)
        # Als IPv4-mapped IPv6 speichern
        return list(b'\x00'*10 + b'\xff\xff' + raw)
    except OSError:
        raw = socket.inet_pton(socket.AF_INET6, addr)
        return list(raw)


# ── Endpoints: Ping + Server-Lifecycle ────────────────────────────────────────

@app.get("/v1/ping")
async def ping(authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    latest = _update_cache["latest"]
    return {
        "ok": True, "agent": "mumble-agent", "version": AGENT_VERSION,
        "ice": _MumbleServer is not None,
        "time": int(time.time()),
        "docker_version": docker_client.version().get("Version") if docker_client else None,
        "mumble_image":   DOCKER_IMAGE,
        "latest_image":   latest,
        "update_available": latest is not None and latest != DOCKER_IMAGE,
    }

@app.post("/v1/image")
async def update_image(req: UpdateImageRequest,
                       authorization: str = Header(default=None)) -> dict[str, Any]:
    """Aktualisiert MUMBLE_AGENT_IMAGE in agent.env und startet den Agent neu."""
    check_token(authorization)
    if not re.match(r'^[a-zA-Z0-9][\w./:@-]{0,254}$', req.image):
        raise HTTPException(400, detail="ungültiger Image-Name")
    env_file = "/etc/mumble-agent/agent.env"
    try:
        with open(env_file, encoding="utf-8") as f:
            lines = f.readlines()
        new_lines, found = [], False
        for line in lines:
            if line.startswith("MUMBLE_AGENT_IMAGE="):
                new_lines.append(f"MUMBLE_AGENT_IMAGE={req.image}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"MUMBLE_AGENT_IMAGE={req.image}\n")
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except OSError as e:
        raise HTTPException(500, detail=f"agent.env schreiben fehlgeschlagen: {e}")
    print(f"[mumble-agent] Image aktualisiert auf {req.image} — starte neu…", flush=True)
    asyncio.get_event_loop().call_later(1.0, lambda: sys.exit(0))
    return {"ok": True, "image": req.image, "restarting": True}

@app.post("/v1/servers")
async def create_server(req: CreateServerRequest,
                        authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    name     = _container_name(req.external_id, req.port)
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
                LABEL_KEY:                    "1",
                "mumble-agent.external_id":   str(req.external_id),
                "mumble-agent.port":          str(req.port),
                "mumble-agent.name":          req.name,
            },
        )
    except APIError as e:
        raise HTTPException(500, detail=f"docker error: {e.explanation or str(e)}")
    superuser_pw = _extract_superuser(c, timeout=30)
    return {"ok": True, "container_id": c.id, "name": c.name,
            "superuser_password": superuser_pw}

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
    name = c.name.lstrip("/")
    try:
        c.remove(force=True)
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    data_dir = _data_dir(name)
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
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

@app.post("/v1/servers/{cid}/upgrade")
async def upgrade_server(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Container mit aktuellem DOCKER_IMAGE neu erstellen (Image-Upgrade)."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    # Image nur ziehen wenn nicht schon vorhanden
    try:
        docker_client.images.get(DOCKER_IMAGE)
    except docker.errors.ImageNotFound:
        try:
            docker_client.images.pull(DOCKER_IMAGE)
        except APIError as e:
            raise HTTPException(500, detail=f"image pull fehlgeschlagen: {e}")
    # SQLite-DB auf NULL-Werte prüfen — Mumble 1.6 Migration bricht sonst ab
    data_dir = _data_dir(c.name.lstrip("/"))
    db_path = os.path.join(data_dir, "mumble-server.sqlite")
    if os.path.isfile(db_path):
        try:
            import sqlite3 as _sq
            con = _sq.connect(db_path)
            con.execute("UPDATE channel_info SET value = '' WHERE value IS NULL")
            con.commit()
            con.close()
        except Exception as e:
            print(f"[mumble-agent] DB-Vorbereinigung fehlgeschlagen (nicht kritisch): {e}", flush=True)
    old_env = _env_map(c)
    new_c = _recreate_container(c, old_env)
    return {"ok": True, "container_id": new_c.id, "image": DOCKER_IMAGE}

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

@app.get("/v1/servers/{cid}/logs")
async def server_logs(cid: str, tail: int = 200,
                      authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    tail = max(10, min(2000, tail))
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
        log = c.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except NotFound:
        raise HTTPException(404, detail="container not found")
    except APIError as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True, "log": log, "tail": tail}


# ── Endpoints: Stats + Viewer (via ICE) ───────────────────────────────────────

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
            pass

    online = 0
    if c.status == "running" and _MumbleServer:
        try:
            comm, srv = _ice_connect(_ice_port_for(c))
            try:
                online = len(srv.getUsers())
            finally:
                comm.destroy()
        except Exception:
            pass

    return {"ok": True, "online": online, "uptime": uptime,
            "status": c.status, "started_at": started_at,
            "image": c.attrs.get("Config", {}).get("Image", "")}

@app.get("/v1/servers/{cid}/dashboard")
async def server_dashboard(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Dashboard-Daten: ICE-Stats + Docker-Ressourcen in einem Aufruf."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")

    # ── Uptime ────────────────────────────────────────────────────────────────
    started_at = c.attrs.get("State", {}).get("StartedAt", "")
    uptime_secs = 0
    if started_at and c.status == "running":
        try:
            ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime_secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
        except Exception:
            pass

    # ── ICE-Daten ─────────────────────────────────────────────────────────────
    users_list: list[dict] = []
    channel_count = 0
    ban_count = 0
    if c.status == "running" and _MumbleServer:
        try:
            comm, srv = _ice_connect(_ice_port_for(c))
            try:
                ice_users = srv.getUsers()
                users_list = [_user_to_dict(u) for u in ice_users.values()]
                channel_count = len(srv.getChannels())
                ban_count = len(srv.getBans())
            finally:
                comm.destroy()
        except Exception:
            pass  # ICE nicht erreichbar – leere Werte zurückgeben

    # ── Docker-Ressourcen ─────────────────────────────────────────────────────
    cpu_pct = 0.0
    mem_mb  = 0.0
    net_rx_mb = 0.0
    net_tx_mb = 0.0
    if c.status == "running":
        try:
            stats = c.stats(stream=False)
            # CPU
            cpu_delta = (stats['cpu_stats']['cpu_usage']['total_usage']
                         - stats['precpu_stats']['cpu_usage']['total_usage'])
            sys_delta  = (stats['cpu_stats']['system_cpu_usage']
                          - stats['precpu_stats'].get('system_cpu_usage', 0))
            num_cpu    = stats['cpu_stats'].get('online_cpus', 1)
            cpu_pct    = round((cpu_delta / sys_delta) * num_cpu * 100, 1) if sys_delta > 0 else 0.0
            # RAM
            mem_mb = round(stats['memory_stats'].get('usage', 0) / 1048576, 1)
            # Netzwerk (alle Interfaces summieren)
            net_rx = sum(v.get('rx_bytes', 0) for v in stats.get('networks', {}).values())
            net_tx = sum(v.get('tx_bytes', 0) for v in stats.get('networks', {}).values())
            net_rx_mb = round(net_rx / 1048576, 2)
            net_tx_mb = round(net_tx / 1048576, 2)
        except Exception:
            pass  # Docker-Stats nicht verfügbar – Nullwerte behalten

    return {
        "ok": True,
        "data": {
            "status":        c.status,
            "uptime_secs":   uptime_secs,
            "users":         users_list,
            "user_count":    len(users_list),
            "channel_count": channel_count,
            "ban_count":     ban_count,
            "cpu_percent":   cpu_pct,
            "mem_mb":        mem_mb,
            "net_rx_mb":     net_rx_mb,
            "net_tx_mb":     net_tx_mb,
        },
    }

@app.get("/v1/servers/{cid}/viewer")
async def channel_viewer(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    """Channel-Baum + Online-User via ICE (live, exakt)."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    if c.status != "running":
        raise HTTPException(409, detail="server not running")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        tree  = srv.getTree()
        users = srv.getUsers()
        return {
            "ok":         True,
            "channels":   _tree_to_dict(tree),
            "user_count": len(users),
        }
    finally:
        comm.destroy()


# ── Endpoints: Live-User-Verwaltung ──────────────────────────────────────────

@app.get("/v1/servers/{cid}/users")
async def get_users(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        users = srv.getUsers()
        return {"ok": True, "users": {str(k): _user_to_dict(v) for k, v in users.items()}}
    finally:
        comm.destroy()

@app.post("/v1/servers/{cid}/users/{session}/kick")
async def kick_user(cid: str, session: int, req: KickRequest,
                    authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        srv.kickUser(session, req.reason)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=f"kick fehlgeschlagen: {e}")
    finally:
        comm.destroy()

@app.patch("/v1/servers/{cid}/users/{session}")
async def update_user(cid: str, session: int, req: UpdateUserRequest,
                      authorization: str = Header(default=None)) -> dict[str, Any]:
    """User stummschalten, taubschalten oder in Channel verschieben."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        users = srv.getUsers()
        if session not in users:
            raise HTTPException(404, detail="user session not found")
        state = users[session]
        if req.mute   is not None: state.mute    = req.mute
        if req.deaf   is not None: state.deaf    = req.deaf
        if req.channel is not None: state.channel = req.channel
        srv.setState(state)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"setState fehlgeschlagen: {e}")
    finally:
        comm.destroy()


# ── Endpoints: Channel-Verwaltung ─────────────────────────────────────────────

@app.get("/v1/servers/{cid}/channels")
async def get_channels(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        channels = srv.getChannels()
        return {"ok": True, "channels": {str(k): _channel_to_dict(v) for k, v in channels.items()}}
    finally:
        comm.destroy()

@app.post("/v1/servers/{cid}/channels")
async def add_channel(cid: str, req: AddChannelRequest,
                      authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        channel_id = srv.addChannel(req.name, req.parent)
        return {"ok": True, "channel_id": channel_id}
    except Exception as e:
        raise HTTPException(500, detail=f"addChannel fehlgeschlagen: {e}")
    finally:
        comm.destroy()

@app.patch("/v1/servers/{cid}/channels/{channel_id}")
async def update_channel(cid: str, channel_id: int, req: UpdateChannelRequest,
                         authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        channels = srv.getChannels()
        if channel_id not in channels:
            raise HTTPException(404, detail="channel not found")
        ch = channels[channel_id]
        if req.name        is not None: ch.name        = req.name
        if req.description is not None: ch.description = req.description
        if req.position    is not None: ch.position    = req.position
        if req.parent      is not None: ch.parent      = req.parent
        srv.setChannelState(ch)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"setChannelState fehlgeschlagen: {e}")
    finally:
        comm.destroy()

@app.delete("/v1/servers/{cid}/channels/{channel_id}")
async def remove_channel(cid: str, channel_id: int,
                         authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        srv.removeChannel(channel_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=f"removeChannel fehlgeschlagen: {e}")
    finally:
        comm.destroy()


# ── Endpoints: ACL (via ICE, kein Neustart) ────────────────────────────────

@app.get("/v1/servers/{cid}/acl")
async def get_acl(cid: str, channel_id: int = 0,
                  authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        acls, groups, inherit = srv.getACL(channel_id)
        reg = srv.getRegisteredUsers("")
        return {
            "ok":               True,
            "channel_id":       channel_id,
            "inherit_acl":      inherit,
            "acl":              [_acl_to_dict(a) for a in acls],
            "groups":           [_group_to_dict(g) for g in groups],
            "registered_users": [{"id": uid, "name": name} for uid, name in reg.items()],
        }
    finally:
        comm.destroy()

@app.put("/v1/servers/{cid}/acl")
async def set_acl(cid: str, req: SetAclRequest,
                  authorization: str = Header(default=None)) -> dict[str, Any]:
    """Setzt ACL via ICE — kein Neustart nötig."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        acls   = [_ice_acl_entry(a) for a in req.acl]
        groups = [_ice_group(g)     for g in req.groups]
        srv.setACL(req.channel_id, acls, groups, req.inherit_acl)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=f"setACL fehlgeschlagen: {e}")
    finally:
        comm.destroy()


# ── Endpoints: Ban-Verwaltung ─────────────────────────────────────────────────

@app.get("/v1/servers/{cid}/bans")
async def get_bans(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        bans = srv.getBans()
        return {"ok": True, "bans": [_ban_to_dict(b) for b in bans]}
    finally:
        comm.destroy()

@app.put("/v1/servers/{cid}/bans")
async def set_bans(cid: str, req: SetBansRequest,
                   authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        ice_bans = []
        for b in req.bans:
            ban          = _MumbleServer.Ban()
            ban.address  = _addr_to_bytes(b.address)
            ban.bits     = b.bits
            ban.name     = b.name
            ban.reason   = b.reason
            ban.duration = b.duration
            ban.start    = int(time.time())
            ban.hash     = ""
            ice_bans.append(ban)
        srv.setBans(ice_bans)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=f"setBans fehlgeschlagen: {e}")
    finally:
        comm.destroy()


# ── Endpoints: ICE aktivieren ─────────────────────────────────────────────────

@app.post("/v1/servers/{cid}/ice/enable")
async def enable_ice(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    """
    Aktiviert ICE für einen bestehenden Container.
    Schreibt ice=tcp -h 127.0.0.1 -p {port+10000} in die INI und startet neu.
    """
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    mumble_port = 64738
    for item in (c.attrs.get("Config", {}).get("Env") or []):
        if item.startswith("MUMBLE_CONFIG_PORT="):
            try:
                mumble_port = int(item.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    ice_port = mumble_port + 10000
    ice_line = f"ice=tcp -h 127.0.0.1 -p {ice_port}"
    ini = _read_config(c)
    if "ice=" not in ini:
        ini = ini.rstrip() + f"\n{ice_line}\n"
        _write_config(c, ini)
    try:
        c.restart(timeout=10)
    except APIError as e:
        raise HTTPException(500, detail=f"Neustart fehlgeschlagen: {e}")
    return {"ok": True, "ice_port": ice_port, "note": "ICE aktiviert, Server neugestartet"}


# ── Endpoints: SuperUser ──────────────────────────────────────────────────────

@app.get("/v1/servers/{cid}/superuser")
async def get_superuser(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    return {"ok": True, "superuser_password": _read_superuser(c)}

@app.post("/v1/servers/{cid}/superuser/reset")
async def reset_superuser(cid: str, req: SuperUserResetRequest,
                          authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    new_pw = req.password.strip() or _generate_password(16)
    try:
        res = c.exec_run(
            ["/usr/bin/mumble-server", "-ini", INI_PATH, "-supw", new_pw],
            user="10000:10000",
        )
        if res.exit_code != 0:
            raise HTTPException(500, detail=f"supw failed: {res.output.decode('utf-8', errors='replace')}")
    except APIError as e:
        raise HTTPException(500, detail=f"docker exec failed: {e}")
    return {"ok": True, "superuser_password": new_pw}


@app.get("/v1/servers/{cid}/settings")
async def get_settings(cid: str, authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    env = _env_map(c)
    settings: dict[str, Any] = {}
    for env_key, field in _SETTINGS_MAP.items():
        if env_key in env:
            settings[field] = env[env_key]
    return {"ok": True, "settings": settings}

@app.patch("/v1/servers/{cid}")
async def update_server(cid: str, req: UpdateServerRequest,
                        authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    old_env = _env_map(c)
    new_env = dict(old_env)
    old_labels = c.labels.copy()
    updated = []

    def _s(env_key: str, value: Any, field: str) -> None:
        new_env[env_key] = str(value); updated.append(field)
    def _sb(env_key: str, value: bool | None, field: str) -> None:
        if value is None: return
        new_env[env_key] = "true" if value else "false"; updated.append(field)
    def _so(env_key: str, value: Any, field: str) -> None:
        if value: new_env[env_key] = str(value)
        else:     new_env.pop(env_key, None)
        updated.append(field)

    if req.name is not None:
        new_env["MUMBLE_CONFIG_REGISTER_NAME"] = req.name
        old_labels["mumble-agent.name"]        = req.name
        updated.append("name")
    if req.welcome_text is not None: _so("MUMBLE_CONFIG_WELCOMETEXT",   req.welcome_text, "welcome_text")
    if req.max_users    is not None: _s( "MUMBLE_CONFIG_USERS",         req.max_users,    "max_users")
    if req.password     is not None: _so("MUMBLE_CONFIG_SERVERPASSWORD",req.password,     "password")
    if req.bandwidth    is not None: _s( "MUMBLE_CONFIG_BANDWIDTH",     req.bandwidth,    "bandwidth")
    if req.timeout      is not None: _s( "MUMBLE_CONFIG_TIMEOUT",       req.timeout,      "timeout")
    if req.textmessagelength  is not None: _s("MUMBLE_CONFIG_TEXTMESSAGELENGTH",  req.textmessagelength,  "textmessagelength")
    if req.imagemessagelength is not None: _s("MUMBLE_CONFIG_IMAGEMESSAGELENGTH", req.imagemessagelength, "imagemessagelength")
    if req.allowhtml    is not None: _sb("MUMBLE_CONFIG_ALLOWHTML",     req.allowhtml,    "allowhtml")
    if req.opusthreshold is not None: _s("MUMBLE_CONFIG_OPUSTHRESHOLD", req.opusthreshold,"opusthreshold")
    if req.defaultchannel is not None: _s("MUMBLE_CONFIG_DEFAULTCHANNEL",req.defaultchannel,"defaultchannel")
    if req.rememberchannel is not None: _sb("MUMBLE_CONFIG_REMEMBERCHANNEL",req.rememberchannel,"rememberchannel")
    if req.certrequired is not None: _sb("MUMBLE_CONFIG_CERTREQUIRED",  req.certrequired, "certrequired")
    if req.usersperchannel is not None: _s("MUMBLE_CONFIG_USERSPERCHANNEL",req.usersperchannel,"usersperchannel")
    if req.register_name     is not None: _so("MUMBLE_CONFIG_REGISTERNAME",    req.register_name,     "register_name")
    if req.register_password is not None: _so("MUMBLE_CONFIG_REGISTERPASSWORD",req.register_password,  "register_password")
    if req.register_url      is not None: _so("MUMBLE_CONFIG_REGISTERURL",     req.register_url,       "register_url")
    if req.register_hostname is not None: _so("MUMBLE_CONFIG_REGISTERHOSTNAME",req.register_hostname,  "register_hostname")
    if req.register_location is not None: _so("MUMBLE_CONFIG_REGISTERLOCATION",req.register_location,  "register_location")
    if req.autoban_attempts  is not None: _s("MUMBLE_CONFIG_AUTOBANATTEMPTS",  req.autoban_attempts,  "autoban_attempts")
    if req.autoban_timeframe is not None: _s("MUMBLE_CONFIG_AUTOBANTIMEFRAME", req.autoban_timeframe, "autoban_timeframe")
    if req.autoban_time      is not None: _s("MUMBLE_CONFIG_AUTOBANTIME",      req.autoban_time,      "autoban_time")
    if req.sendversion       is not None: _sb("MUMBLE_CONFIG_SENDVERSION",     req.sendversion,       "sendversion")
    if req.bonjour           is not None: _sb("MUMBLE_CONFIG_BONJOUR",         req.bonjour,           "bonjour")
    if req.suggestversion    is not None: _so("MUMBLE_CONFIG_SUGGESTVERSION",  req.suggestversion,    "suggestversion")
    if req.suggestpositional    is not None: _sb("MUMBLE_CONFIG_SUGGESTPOSITIONAL",   req.suggestpositional,    "suggestpositional")
    if req.suggestpushtotalk    is not None: _sb("MUMBLE_CONFIG_SUGGESTPUSHTOTALK",   req.suggestpushtotalk,    "suggestpushtotalk")
    if req.channelnestinglimit  is not None: _s( "MUMBLE_CONFIG_CHANNELNESTINGLIMIT", req.channelnestinglimit,  "channelnestinglimit")
    if req.allowping            is not None: _sb("MUMBLE_CONFIG_ALLOWPING",           req.allowping,            "allowping")
    if req.username             is not None: _so("MUMBLE_CONFIG_USERNAME",            req.username,             "username")
    if req.channelname          is not None: _so("MUMBLE_CONFIG_CHANNELNAME",         req.channelname,          "channelname")

    if not updated:
        return {"ok": True, "note": "nothing to update"}
    new_c = _recreate_container(c, new_env)
    return {"ok": True, "updated_fields": updated, "container_id": new_c.id, "name": new_c.name}


@app.patch("/v1/servers/{cid}/live")
async def update_settings_live(cid: str, req: LiveSettingsRequest,
                               authorization: str = Header(default=None)) -> dict[str, Any]:
    """Aendert Server-Einstellungen via ICE ohne Neustart."""
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    comm, srv = _ice_connect(_ice_port_for(c))
    try:
        mapping = {
            'name':         'registername',
            'password':     'serverpassword',
            'max_users':    'users',
            'welcome_text': 'welcometext',
        }
        updated = []
        for field, ice_key in mapping.items():
            val = getattr(req, field)
            if val is not None:
                srv.setConf(ice_key, str(val))
                updated.append(field)
        if req.password is not None and req.password == '':
            srv.setConf('serverpassword', '')
        return {"ok": True, "updated": updated}
    except Exception as e:
        raise HTTPException(500, detail=f"ICE setConf fehlgeschlagen: {e}")
    finally:
        comm.destroy()


# ── Endpoints: Zertifikat ─────────────────────────────────────────────────────

@app.put("/v1/servers/{cid}/certificate")
async def set_certificate(cid: str, req: CertificateRequest,
                          authorization: str = Header(default=None)) -> dict[str, Any]:
    import tarfile as tf
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    for filename, content in [("mumble.crt", req.cert), ("mumble.key", req.key)]:
        data = content.encode()
        buf = io.BytesIO()
        with tf.open(fileobj=buf, mode="w") as tar:
            info = tf.TarInfo(name=filename)
            info.size = len(data)
            info.uid = info.gid = 10000
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        try:
            c.put_archive("/data", buf)
        except APIError as e:
            raise HTTPException(500, detail=f"put_archive failed: {e}")
    old_env = _env_map(c)
    new_env = dict(old_env)
    new_env["MUMBLE_CONFIG_SSLCERT"] = "/data/mumble.crt"
    new_env["MUMBLE_CONFIG_SSLKEY"]  = "/data/mumble.key"
    new_c = _recreate_container(c, new_env)
    return {"ok": True, "container_id": new_c.id}

@app.delete("/v1/servers/{cid}/certificate")
async def remove_certificate(cid: str,
                              authorization: str = Header(default=None)) -> dict[str, Any]:
    check_token(authorization)
    assert docker_client is not None
    try:
        c = docker_client.containers.get(cid)
        _require_managed(c)
    except NotFound:
        raise HTTPException(404, detail="container not found")
    old_env = _env_map(c)
    new_env = dict(old_env)
    new_env.pop("MUMBLE_CONFIG_SSLCERT", None)
    new_env.pop("MUMBLE_CONFIG_SSLKEY", None)
    new_c = _recreate_container(c, new_env)
    return {"ok": True, "container_id": new_c.id}


# ── Error-Handler ─────────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"ok": False, "error": exc.detail})
