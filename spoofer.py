#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=10.7"]
# ///
"""Location-spoof engine: a point-list walker over pymobiledevice3's DVT LocationSimulation.

Requires `sudo pymobiledevice3 remote tunneld` running separately (see PLAN.md M0).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import secrets
import shutil
import signal
import socket
import subprocess
import warnings
from pathlib import Path

from pymobiledevice3.exceptions import AlreadyMountedError
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import auto_mount
from pymobiledevice3.tunneld.api import get_tunneld_devices

with warnings.catch_warnings():
    # pymobiledevice3's own tunneld does the same: pydantic v1 compat shim warns under 3.14.
    warnings.simplefilter("ignore", category=UserWarning)
    import fastapi

import uvicorn
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

Point = tuple[float, float]

EARTH_RADIUS_M = 6_371_000.0
CONFIG_PATH = Path.home() / ".spoofer.json"
FAVORITES_PATH = CONFIG_PATH  # alias — config also holds token
# Re-set the held point this often so a dead tunnel surfaces without waiting for the next user action.
HEARTBEAT_S = 5.0
RECONNECT_BACKOFF_S = 2.0
DEFAULT_PORT = 8731
DAEMON_LABEL = "com.spoofer.tunneld"
AGENT_LABEL = "com.spoofer.app"
PYMOBILEDEVICE3_BIN = (
    Path.home()
    / "Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3"
)


def haversine_distance_m(a: Point, b: Point) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def initial_bearing_rad(a: Point, b: Point) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(x, y)


def destination_point(start: Point, bearing_rad: float, distance_m: float) -> Point:
    lat1, lon1 = map(math.radians, start)
    ang_dist = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist) + math.cos(lat1) * math.sin(ang_dist) * math.cos(bearing_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing_rad) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def interpolate_glide(start: Point, end: Point, speed_mps: float, hz: float = 1.0, jitter_m: float = 0.0) -> list[Point]:
    """Point list walking start -> end at speed_mps, sampled at hz. First point is exactly
    `start`, last is exactly `end` — teleport is just the degenerate 1-point list [start]."""
    distance = haversine_distance_m(start, end)
    if distance == 0:
        return [start]
    bearing = initial_bearing_rad(start, end)
    step_distance = max(speed_mps / hz, 1e-6)
    steps = max(1, math.ceil(distance / step_distance))
    points = [start]
    for i in range(1, steps + 1):
        d = min(i * step_distance, distance)
        lat, lon = destination_point(start, bearing, d)
        if jitter_m and i < steps:  # never jitter the landing point
            lat, lon = destination_point((lat, lon), random.uniform(0, 2 * math.pi), random.gauss(0, jitter_m))
        points.append((lat, lon))
    points[-1] = end
    return points


def notify_macos(title: str, message: str) -> None:
    """Fire a macOS user notification. One-liner via osascript — no extra dependency."""

    def _esc(s: str) -> str:
        return s.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))

    subprocess.run(
        ["osascript", "-e", f'display notification "{_esc(message)}" with title "{_esc(title)}"'],
        check=False,
        capture_output=True,
    )


class SpoofEngine:
    """Holds one live DVT LocationSimulation connection and walks it through a point list.

    The DVT connection must stay open for the spoof to remain live (confirmed in M0 —
    `simulate-location set` blocks on purpose). `start()` owns a background task that
    holds the connection, walks the points, then heartbeats the last one until `stop()`
    cancels it. On tunnel drop the task reconnects, remounts DDI if needed, and re-applies
    from the next unsent point (or the held last point).
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._loc: LocationSimulation | None = None
        self._dvt_cm: DvtProvider | None = None
        self._caffeinate: subprocess.Popen | None = None
        self.current_point: Point | None = None
        self.mode: str | None = None
        # idle | connected | reconnecting — surfaced to the UI banner
        self.connection: str = "idle"

    def _start_caffeinate(self) -> None:
        if self._caffeinate is not None:
            return
        # -i: prevent idle sleep for the lifetime of an active spoof (lid-close still sleeps).
        self._caffeinate = subprocess.Popen(
            ["caffeinate", "-i"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_caffeinate(self) -> None:
        if self._caffeinate is None:
            return
        with contextlib.suppress(Exception):
            self._caffeinate.terminate()
            self._caffeinate.wait(timeout=2)
        self._caffeinate = None

    async def _try_remount_ddi(self, rsd) -> None:
        """Best-effort DDI remount after a reconnect — already-mounted is fine."""
        try:
            await auto_mount(rsd)
        except AlreadyMountedError:
            pass
        except Exception:
            pass

    async def _open_simulation(self, rsd) -> None:
        self._dvt_cm = DvtProvider(rsd)
        dvt = await self._dvt_cm.__aenter__()
        self._loc = LocationSimulation(dvt)
        await self._loc.__aenter__()

    async def _connect(self) -> None:
        rsds = await get_tunneld_devices()
        if not rsds:
            raise RuntimeError("no device found via tunneld — is `sudo pymobiledevice3 remote tunneld` running?")
        rsd = rsds[0]
        try:
            await self._open_simulation(rsd)
        except Exception:
            # Tunnel may be up but DDI unmounted after a drop — remount and retry once.
            await self._abandon()
            await self._try_remount_ddi(rsd)
            rsds = await get_tunneld_devices()
            if not rsds:
                raise RuntimeError("no device found via tunneld after DDI remount")
            await self._open_simulation(rsds[0])

    async def _abandon(self) -> None:
        """Drop a dead connection without clear() — the tunnel is already gone."""
        if self._loc is not None:
            with contextlib.suppress(Exception):
                await self._loc.__aexit__(None, None, None)
            self._loc = None
        if self._dvt_cm is not None:
            with contextlib.suppress(Exception):
                await self._dvt_cm.__aexit__(None, None, None)
            self._dvt_cm = None

    async def _disconnect(self) -> None:
        if self._loc is not None:
            with contextlib.suppress(Exception):
                await self._loc.clear()
            with contextlib.suppress(Exception):
                await self._loc.__aexit__(None, None, None)
            self._loc = None
        if self._dvt_cm is not None:
            with contextlib.suppress(Exception):
                await self._dvt_cm.__aexit__(None, None, None)
            self._dvt_cm = None

    async def _walk(self, points: list[Point], hz: float) -> None:
        """Walk `points`, then heartbeat the last one. Reconnect + re-apply on drop."""
        self._start_caffeinate()
        interval = 1.0 / hz
        heartbeat = max(HEARTBEAT_S, interval)
        idx = 0
        notified_drop = False
        try:
            while True:
                try:
                    await self._connect()
                    self.connection = "connected"
                    if notified_drop:
                        notify_macos("Spoofer", "Connection restored — spoof re-applied")
                        notified_drop = False

                    assert self._loc is not None
                    while idx < len(points):
                        lat, lon = points[idx]
                        await self._loc.set(lat, lon)
                        self.current_point = (lat, lon)
                        idx += 1
                        await asyncio.sleep(interval)

                    last = points[-1]
                    while True:
                        await self._loc.set(*last)
                        self.current_point = last
                        await asyncio.sleep(heartbeat)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._abandon()
                    self.connection = "reconnecting"
                    if not notified_drop:
                        notify_macos("Spoofer", "Connection lost — reconnecting…")
                        notified_drop = True
                    await asyncio.sleep(RECONNECT_BACKOFF_S)
        finally:
            self.connection = "idle"
            await self._disconnect()
            self._stop_caffeinate()

    async def start(self, points: list[Point], hz: float = 1.0, mode: str = "teleport") -> None:
        """Replace any active spoof with a new point-list walk."""
        if not points:
            raise ValueError("engine requires a non-empty point list")
        await self.stop()
        self.current_point = points[0]
        self.mode = mode
        self.connection = "reconnecting"  # flips to connected once DVT is up
        self._task = asyncio.create_task(self._walk(points, hz))

    async def stop(self) -> None:
        """Cancel the active walk and restore real GPS."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.current_point = None
        self.mode = None
        self.connection = "idle"
        self._stop_caffeinate()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"favorites": []}
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def load_favorites() -> list[dict]:
    return load_config().get("favorites", [])


def save_favorites(favorites: list[dict]) -> None:
    cfg = load_config()
    cfg["favorites"] = favorites
    save_config(cfg)


def get_or_create_token() -> str:
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        token = secrets.token_urlsafe(32)
        cfg["token"] = token
        save_config(cfg)
    return token


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _uv_bin() -> Path:
    found = shutil.which("uv")
    if found:
        return Path(found)
    homebrew = Path("/opt/homebrew/bin/uv")
    if homebrew.exists():
        return homebrew
    raise RuntimeError("uv not found — brew install uv")


def _extract_token(request: "fastapi.Request") -> str:
    header = request.headers.get("x-spoofer-token")
    if header:
        return header.strip()
    q = request.query_params.get("token")
    if q:
        return q.strip()
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class SetBody(BaseModel):
    lat: float
    lon: float
    mode: str = "teleport"
    speed: float = 1.4
    origin_lat: float | None = None
    origin_lon: float | None = None


class FavoriteBody(BaseModel):
    name: str
    lat: float
    lon: float


def build_app(engine: SpoofEngine, *, token: str | None = None) -> fastapi.FastAPI:
    """Build the FastAPI app. Pass token to require auth on every request (LAN mode)."""
    app = fastapi.FastAPI()
    index_path = Path(__file__).parent / "index.html"


    @app.middleware("http")
    async def check_origin(request: fastapi.Request, call_next):
        # Any webpage a browser has open can background-POST to localhost — this is
        # normal browser behaviour, not an exploit, so it needs an explicit reject.
        origin = request.headers.get("origin")
        if origin is not None:
            host = request.headers.get("host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def check_token(request: fastapi.Request, call_next):
        # LAN mode only — loopback serve leaves token=None and skips this.
        if token is not None:
            provided = _extract_token(request)
            # compare_digest requires equal length — reject mismatched sizes without raising.
            if (
                not provided
                or len(provided) != len(token)
                or not secrets.compare_digest(provided, token)
            ):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/api/status")
    async def status() -> dict:
        point = engine.current_point
        return {
            "active": point is not None,
            "lat": point[0] if point else None,
            "lon": point[1] if point else None,
            "mode": engine.mode,
            "connection": engine.connection,
            "lan": token is not None,
        }

    @app.post("/api/set")
    async def set_location(body: SetBody) -> dict:
        target: Point = (body.lat, body.lon)
        if body.mode == "glide":
            origin = engine.current_point
            if origin is None:
                if body.origin_lat is None or body.origin_lon is None:
                    raise fastapi.HTTPException(
                        400, "glide needs an origin — click your current real position on the map first"
                    )
                origin = (body.origin_lat, body.origin_lon)
            points = interpolate_glide(origin, target, body.speed, jitter_m=1.5)
        else:
            points = [target]
        await engine.start(points, mode=body.mode)
        return {"ok": True, "points": len(points)}

    @app.post("/api/clear")
    async def clear_location() -> dict:
        await engine.stop()
        return {"ok": True}

    @app.get("/api/favorites")
    async def get_favorites() -> list[dict]:
        return load_favorites()

    @app.post("/api/favorites")
    async def add_favorite(body: FavoriteBody) -> list[dict]:
        favorites = [f for f in load_favorites() if f["name"] != body.name]
        favorites.append(body.model_dump())
        save_favorites(favorites)
        return favorites

    @app.delete("/api/favorites/{name}")
    async def delete_favorite(name: str) -> list[dict]:
        favorites = [f for f in load_favorites() if f["name"] != name]
        save_favorites(favorites)
        return favorites

    return app


def _plist_xml(label: str, program_args: list[str], *, root: bool, cwd: str | None = None) -> str:
    """Minimal launchd plist. KeepAlive + RunAtLoad so reboot brings services back."""
    args_xml = "\n".join(f"    <string>{a}</string>" for a in program_args)
    log_dir = "/Library/Logs" if root else str(Path.home() / "Library/Logs")
    out_log = f"{log_dir}/{label}.out.log"
    err_log = f"{log_dir}/{label}.err.log"
    cwd_xml = f"""  <key>WorkingDirectory</key>
  <string>{cwd}</string>
""" if cwd else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
{cwd_xml}  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{out_log}</string>
  <key>StandardErrorPath</key>
  <string>{err_log}</string>
</dict>
</plist>
"""


def install_service(*, port: int = DEFAULT_PORT, lan: bool = True) -> None:
    """Write + bootstrap root tunneld daemon and user app agent. Needs one sudo."""
    if not PYMOBILEDEVICE3_BIN.exists():
        raise SystemExit(f"pymobiledevice3 not found at {PYMOBILEDEVICE3_BIN}")
    uv = _uv_bin()
    script = Path(__file__).resolve()
    token = get_or_create_token() if lan else None

    # --- root LaunchDaemon: tunneld ---
    daemon_plist = Path(f"/Library/LaunchDaemons/{DAEMON_LABEL}.plist")
    daemon_body = _plist_xml(
        DAEMON_LABEL,
        [str(PYMOBILEDEVICE3_BIN), "remote", "tunneld"],
        root=True,
    )
    tmp_daemon = Path("/tmp") / f"{DAEMON_LABEL}.plist"
    tmp_daemon.write_text(daemon_body)
    print(f"installing {daemon_plist} (sudo)…")
    rc = subprocess.run(["sudo", "cp", str(tmp_daemon), str(daemon_plist)])
    if rc.returncode != 0:
        raise SystemExit(
            "sudo failed — run this in your own Terminal (needs your Mac password):\n"
            "  cd /Users/ibrahimansari/Spoofer && uv run spoofer.py install-service\n"
            "Stop any manual tunneld / serve first so ports 49151 and 8731 are free."
        )
    subprocess.run(["sudo", "chmod", "644", str(daemon_plist)], check=True)
    # bootout is fine to fail if not loaded yet (prints "No such process" on first install)
    subprocess.run(
        ["sudo", "launchctl", "bootout", f"system/{DAEMON_LABEL}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["sudo", "launchctl", "bootstrap", "system", str(daemon_plist)], check=True)
    subprocess.run(["sudo", "launchctl", "enable", f"system/{DAEMON_LABEL}"], check=False)
    subprocess.run(["sudo", "launchctl", "kickstart", "-k", f"system/{DAEMON_LABEL}"], check=False)
    print(f"  tunneld daemon: system/{DAEMON_LABEL}")

    # --- user LaunchAgent: serve (LAN by default) ---
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    agent_plist = agents / f"{AGENT_LABEL}.plist"
    serve_args = [str(uv), "run", str(script), "serve", "--port", str(port)]
    if lan:
        serve_args.append("--lan")
    agent_body = _plist_xml(
        AGENT_LABEL,
        serve_args,
        root=False,
        cwd=str(script.parent),
    )
    # Inject PATH so uv/homebrew resolve under launchd's minimal environment.
    agent_body = agent_body.replace(
        "  <key>RunAtLoad</key>",
        """  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>"""
        + str(Path.home())
        + """</string>
  </dict>
  <key>RunAtLoad</key>""",
        1,
    )
    agent_plist.write_text(agent_body)
    uid = os.getuid()
    domain = f"gui/{uid}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["launchctl", "bootstrap", domain, str(agent_plist)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{AGENT_LABEL}"], check=False)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{AGENT_LABEL}"], check=False)
    print(f"  app agent: {domain}/{AGENT_LABEL}")

    if lan and token:
        url = f"http://{lan_ip()}:{port}/?token={token}"
        print()
        print("Add this URL to your iPhone home screen (Safari → Share → Add to Home Screen):")
        print(f"  {url}")
        print()
        print("Token also saved in ~/.spoofer.json — keep it private.")
    else:
        print(f"loopback UI: http://127.0.0.1:{port}")


async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Set or clear the iPhone's simulated location.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    teleport = sub.add_parser("teleport", help="Instantly set a single location and hold it.")
    teleport.add_argument("lat", type=float)
    teleport.add_argument("lon", type=float)

    glide = sub.add_parser("glide", help="Walk from one location to another at a given speed.")
    glide.add_argument("lat1", type=float)
    glide.add_argument("lon1", type=float)
    glide.add_argument("lat2", type=float)
    glide.add_argument("lon2", type=float)
    glide.add_argument("--speed", type=float, default=1.4, help="m/s, default walking pace")
    glide.add_argument("--jitter", type=float, default=1.5, help="gaussian jitter radius in meters")

    sub.add_parser("clear", help="Stop any spoof and restore real GPS.")

    serve = sub.add_parser("serve", help="Start the local web UI (map, search, favorites).")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Bind 0.0.0.0 and require the ~/.spoofer.json token on every request",
    )

    install = sub.add_parser(
        "install-service",
        help="Install root tunneld LaunchDaemon + user app LaunchAgent (one sudo).",
    )
    install.add_argument("--port", type=int, default=DEFAULT_PORT)
    install.add_argument(
        "--no-lan",
        action="store_true",
        help="Install the app agent bound to 127.0.0.1 only (no phone access)",
    )

    args = parser.parse_args()

    if args.cmd == "install-service":
        install_service(port=args.port, lan=not args.no_lan)
        return

    engine = SpoofEngine()

    if args.cmd == "clear":
        await engine.stop()
        print("cleared")
        return

    if args.cmd == "serve":
        token = get_or_create_token() if args.lan else None
        host = "0.0.0.0" if args.lan else "127.0.0.1"
        app = build_app(engine, token=token)
        config = uvicorn.Config(app, host=host, port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        # uvicorn installs its own handlers via signal.signal() rather than
        # loop.add_signal_handler() — verified unreliable here (a SIGTERM during an
        # active spoof killed the process without ever reaching our `finally`, leaving
        # the phone stuck spoofed). Disable it and reuse the loop.add_signal_handler
        # pattern already proven twice for teleport/glide, just flipping the flag
        # uvicorn's own serve loop already polls for shutdown.
        server.install_signal_handlers = lambda: None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, setattr, server, "should_exit", True)
        if args.lan and token:
            url = f"http://{lan_ip()}:{args.port}/?token={token}"
            print(f"LAN mode — open on your phone and Add to Home Screen:")
            print(f"  {url}")
        else:
            print(f"open http://127.0.0.1:{args.port}")
        try:
            await server.serve()
        finally:
            await engine.stop()
            print("location cleared, exiting")
        return

    # teleport / glide: hold until stopped.
    # Bare `except KeyboardInterrupt` around the coroutine is unreliable with asyncio —
    # a signal delivered while the loop is blocked in its selector surfaces in the loop's
    # own driver code, not in this suspended coroutine. Register real handlers instead,
    # for both SIGINT (Ctrl+C) and SIGTERM (what `launchd`/`kill` send on stop, per the
    # CLAUDE.md clear-on-exit requirement) so shutdown is deterministic either way.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        if args.cmd == "teleport":
            await engine.start([(args.lat, args.lon)])
            print(f"holding at {args.lat}, {args.lon} — Ctrl+C to clear")
        else:  # glide
            points = interpolate_glide(
                (args.lat1, args.lon1), (args.lat2, args.lon2), args.speed, jitter_m=args.jitter
            )
            print(f"gliding through {len(points)} points at {args.speed} m/s — Ctrl+C to clear")
            await engine.start(points, mode="glide")

        await stop_event.wait()
    finally:
        await engine.stop()
        print("location cleared, exiting")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_cli_main())
