# Spoofer — build plan

Tethered iOS location spoofer. macOS host, iPhone on iOS 26.6, no jailbreak.
Built on `pymobiledevice3`. Primary target: Find My. Secondary: Life360.

---

## Frozen scope

**In v1**

- Python core on `pymobiledevice3`; macOS only
- Connection over **Wi-Fi** (same LAN) with USB as fallback
- `tunneld` as a root launchd **daemon** — installed once, starts at boot
- App as a user launchd **agent** — always running, no terminal
- DDI auto-mount on connect (no sudo)
- Web UI: Leaflet + OSM map, Nominatim search, click-to-set, favorites
- **Instant teleport** (default) and **Glide** (opt-in, great-circle at chosen speed)
- LAN-accessible UI behind a secret token → drive it from the iPhone
- Auto-reconnect + re-apply on drop, macOS notification, red/green banner
- `caffeinate` held while a spoof is active
- `clear()` on graceful quit
- One `test_spoofer.py`

**Out of v1**

Route drawing / polyline editor (stretch), joystick, Windows, PyInstaller +
notarization, GPX import/export, untethered persistence, `--userspace` tunnel
(needs Python 3.14; host has 3.11), auto-spoof-on-connect, iOS Shortcuts.

**Hard constraints — not engineering problems, physics of the approach**

- Spoof is alive only while the tunnel is alive. Nothing persists.
- Mac must be awake. `caffeinate` blocks idle sleep, **not lid-close sleep.**
  Lid shut in a bag does not work.
- Phone and Mac must share a network (or a cable). Laptop-at-home +
  phone-on-cellular is impossible.
- GPS only. Wi-Fi BSSID and IP geolocation still report the truth — Life360
  checks those, Find My does not.

---

## Architecture

Three files. Resist adding a fourth.

```
spoofer.py        engine + async web server + launchd install subcommand
index.html        map, search, favorites, glide toggle, status banner
test_spoofer.py   interpolator + point-list engine
~/.spoofer.json   favorites, token, last position
```

**The one design decision everything hangs on:** the engine consumes a
**list of points**, never a single coordinate.

- teleport → 1-point list
- glide → N-point list from great-circle interpolation
- drawn route (stretch) → 200-point list from the map

One code path, one timer, one jitter function. The stretch feature becomes
"make the UI emit more points" instead of a rewrite.

---

## Milestones

### M0 — CLI proof *(one afternoon — do not skip)*

Everything downstream is worthless if this fails.

```bash
brew install pipx && pipx install pymobiledevice3
```

1. Enable Developer Mode on the phone, trust the Mac, reboot
2. `pymobiledevice3 mounter auto-mount`
3. `sudo pymobiledevice3 remote tunneld` — note the RSD address/port
4. `pymobiledevice3 developer dvt simulate-location set --rsd <addr> <port> -- 41.8781 -87.6298`
5. Confirm in Maps. Then `... simulate-location clear`

**Also test in M0, because it reshapes the product:** does `tunneld` discover
the phone **over Wi-Fi** with no cable? If yes, the cable disappears from the
design. If no, USB-only and say so in the README.

**Gate:** teleport and clear, reliably, twice in a row.
**Risk:** iOS 26 is newer than most guides. Expect at least one broken thing;
update `pymobiledevice3` first before debugging anything else.

**M0 result (verified on this Mac/phone):**
- `pipx install pymobiledevice3` built its venv against **Python 3.14.6**
  (Homebrew pulled it in as a dependency) — the `--userspace` no-sudo tunnel
  is available now, not blocked on a future install. Still starting with
  `tunneld` per Q8; revisit `--userspace` after M1 works.
- `sudo` needs the pipx venv's full binary path — root's PATH doesn't include
  `~/.local/bin`. Use
  `/Users/ibrahimansari/Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3`
  in any launchd plist or sudo invocation.
- `tunneld` exposes a local HTTP API on `127.0.0.1:49151` — `GET /` returns
  connected devices with their RSD tunnel address/port. Useful for the engine
  to discover the tunnel without scraping `tunneld`'s stdout.
- `--wifi` monitoring is on by default in `tunneld`; USB-first discovery
  confirmed working. Wi-Fi-only (no cable) still needs a real test with the
  phone disconnected — do that before M1 to confirm the design in Q1–Q10.
- **`developer dvt simulate-location set` blocks and holds the DVT connection
  open for as long as the spoof should stay live.** Killing that process (or
  losing the tunnel) is what reverts the phone. This is the actual mechanism
  behind "spoof dies with the tunnel" — confirms the engine must be one
  long-lived async task per session, not a fire-and-forget call, and that
  losing that task is precisely the trigger M3's reconnect logic watches for.
- Confirmed device-wide: both Maps and Weather reflected the fake location
  simultaneously. Life360 did not update within the test window — expected,
  since it requests a fix on its own schedule rather than polling
  continuously; foregrounding the Life360 app should force a fresh request.

### M1 — Python core

Replace shelling out with the library. `get_tunneld_devices()` →
`DvtProvider(rsd)` → `LocationSimulation(dvt).set(lat, lon)`.

Write the point-list engine here: an async task that walks a list of points,
sleeps ~1s between them, applies Gaussian jitter, and holds on the last one.

**Gate:** a Python function sets the phone's location. Verify signatures
against the installed package's `docs/`, not against web snippets — the API
went async incrementally and most examples online are stale.

**M1 result:** built `spoofer.py` (engine) and `test_spoofer.py` (self-check),
verified against the actual installed 10.7.3 source, not the brief's pinned
9.33.4 snippets — signatures matched closely but `LocationSimulation` is
constructed from a `DvtProvider` and used as its own async context manager,
not called as a bare function. Confirmed:
- `SpoofEngine.start()`/`stop()` wraps `get_tunneld_devices()` →
  `DvtProvider(rsd)` → `LocationSimulation(dvt)`, holding the connection open
  in a background `asyncio.Task` per the M0 finding.
- Real bug caught and fixed: a bare `except KeyboardInterrupt` around the
  coroutine does **not** reliably catch Ctrl+C under asyncio — a signal
  arriving while the loop is blocked in its selector surfaces in the loop's
  own driver code, not the suspended coroutine, so cleanup can be skipped
  silently. Fixed with explicit `loop.add_signal_handler()` for both SIGINT
  and SIGTERM, setting an `asyncio.Event` the main coroutine awaits — verified
  by sending real SIGTERM and confirming "location cleared, exiting" prints
  and the DVT connection closes every time.
- `uv run spoofer.py` needs its own PEP 723 dependency block per file — a
  script that only imports another script doesn't inherit its metadata, so
  `test_spoofer.py` carries the same `dependencies = [...]` header.

### M2 — Web UI ✅ done

Local server, `127.0.0.1` only for now. Serves `index.html`, exposes
`/set`, `/clear`, `/status`, `/favorites`.

Map with click-to-set, Nominatim search box, favorites list, glide toggle
with a speed picker, status banner.

Glide origin is **clicked by the user** on first use (there is no API to read
the phone's real position). Afterwards the origin is the last fake position.

**Framework:** check whether `pymobiledevice3` already pulls in FastAPI +
uvicorn for its own `tunneld` HTTP API. If so, use it — free. Only add a
dependency if that turns out false.

**Gate:** a non-technical person sets a location without a terminal.

**M2 result:** confirmed FastAPI/uvicorn/starlette/pydantic are already
transitive deps of `pymobiledevice3` — used them, no new dependency added.
Built into `spoofer.py`: `build_app(engine)` (routes are `/api/status`,
`/api/set`, `/api/clear`, `/api/favorites` [GET/POST], `/api/favorites/{name}`
[DELETE] — under `/api/` unlike the path names sketched above), an
Origin-check middleware (verified: same-origin 200, foreign Origin 403), and
a `serve` subcommand (`uv run spoofer.py serve --port 8731`). `index.html` is
Leaflet + OSM tiles + client-side Nominatim search, no build step. Full
click-through tested in a real browser against the real phone: search →
click result → Set Location → phone updates (confirmed on Maps); Save
Favorite → persists to `~/.spoofer.json`; click favorite → Set Location →
Clear; glide with no prior position → 400 → UI prompts "click your real
position" → click → glide starts from there (confirmed via `/api/status`
showing `mode: "glide"`).

**Serious bug found and fixed here — read this before touching signal
handling anywhere in this codebase:** uvicorn installs its own SIGINT/SIGTERM
handlers using the plain `signal.signal()` API, not `loop.add_signal_handler()`.
Verified directly: set an active teleport through `serve`, sent a real SIGTERM
to the process, and it died **without ever reaching the `finally: engine.stop()`
block** — no "location cleared, exiting" printed, no lingering process, and
the phone was left stuck on the fake location with nothing watching it. This
is exactly the "badly-terminated session sticks the device" footgun the plan
already warned about, except triggered by our *own* framework's shutdown
path, not user error. Fixed by disabling uvicorn's handler
(`server.install_signal_handlers = lambda: None`) and reusing the
`loop.add_signal_handler()` pattern already proven twice in M1, just flipping
`server.should_exit = True` instead of a local event. Re-tested the same
active-spoof-then-SIGTERM scenario afterward — clean "location cleared,
exiting" every time, phone confirmed reverted on the actual device.
**Takeaway for M3/M4: never trust a library's own signal handling without
testing it against a live spoof. Always verify with an *active* spoof, not an
already-cleared one — that's what hid this bug the first time.**

### M3 — Resilience ✅ done

This is the actual product, not polish. A silent revert is the failure mode
that exposes you.

- Reconnect loop: on drop, re-establish, re-mount DDI if needed, re-apply the
  last position
- `osascript -e 'display notification ...'` on drop **and** on recovery — one
  line, stdlib `subprocess`, no dependency
- `caffeinate -i` as a child process for the lifetime of an active spoof
- `clear()` on SIGINT/SIGTERM and normal exit (guards the documented footgun
  where a badly-terminated session sticks the device at a fake coordinate
  until reboot)

**Gate:** yank the connection mid-spoof. Notification fires. Restore it.
Position comes back on its own.

**M3 result (code in; physical yank-test pending):**
- `SpoofEngine._walk` now heartbeats the held point every `HEARTBEAT_S` (5s)
  via a re-`set()`, so a dead tunnel is detected without waiting for the next
  user action (the old `sleep(3600)` hold could never notice a drop).
- On any non-cancel failure: `_abandon()` (no `clear()` — tunnel is gone),
  flip `connection` to `"reconnecting"`, notify once, backoff 2s, reconnect.
  Mid-glide resumes from the next unsent index; hold re-applies `points[-1]`.
- Connect failure with a live tunneld device tries `auto_mount(rsd)` once
  (swallows `AlreadyMountedError`) then retries DVT open.
- `caffeinate -i` child process spans the active walk; killed on `stop()` /
  walk `finally`.
- `/api/status` exposes `connection: idle|connected|reconnecting`; banner
  turns amber while reconnecting.
- Self-check green (`uv run test_spoofer.py`).
- **Physical gate passed:** yank mid-spoof → notification + amber banner →
  restore → recovery notification + position re-applied on its own.

### M4 — Zero friction ✅ done

- `spoofer.py install-service` writes and bootstraps a root LaunchDaemon for
  `tunneld` (one sudo, ever) and a user LaunchAgent for the app itself
- LAN mode: opt-in flag, binds beyond loopback, requires
  `secrets.token_urlsafe(32)` on every request, keeps the `Origin` check
- Add the tokenized URL to the iPhone home screen

**Gate:** reboot the Mac, touch nothing, open the bookmark on the phone, set a
location.

**M4 result (code in; reboot gate pending):**
- `install-service` writes `/Library/LaunchDaemons/com.spoofer.tunneld.plist`
  (full pipx pymobiledevice3 path) and `~/Library/LaunchAgents/com.spoofer.app.plist`
  (`uv run … serve --lan`), bootstraps both via `launchctl`.
- `serve --lan` binds `0.0.0.0`, generates/persists token in `~/.spoofer.json`,
  requires it on every request (header / query / Bearer). Origin check kept.
- UI reads `?token=` (and localStorage), sends `X-Spoofer-Token` on all API calls.
- Prints the phone bookmark URL: `http://<lan-ip>:8731/?token=…`
- **Reboot gate passed:** reboot Mac → touch nothing → open home-screen
  bookmark on phone → set location works.

### Stretch — route drawing

Leaflet-Geoman for waypoint add/drag/delete (do not hand-roll: ~150 fiddly
lines vs ~10 with the plugin). Emit the point list to the existing engine.

Known ceiling: straight segments cut through buildings and water. Believable
commutes need a routing engine (public OSRM demo server, rate-limited). Without
snapping this produces *longer* fake routes, not more *believable* ones — which
is most of why it is a stretch and not v1.

---

## Security

The UI is an HTTP server on a machine that can move your identity around.

- Bind `127.0.0.1` by default. LAN is opt-in and explicit.
- Reject foreign `Origin` headers. Any webpage you visit can reach `localhost`
  in the background — that is normal browser behaviour, not an exploit.
- Token required on every request once LAN mode is on.
- The app never runs as root. `tunneld` holds the privilege, in its own
  process, with its own lifecycle — which is also why it survives app crashes
  and makes reconnect instant.

---

## Known risks

| Risk | Mitigation |
|---|---|
| iOS update breaks mounting/tunneling | Update `pymobiledevice3` first. Fall back to `go-ios` or Xcode+GPX until it catches up. Expect this every September. |
| `pymobiledevice3` async API churn | Write against the installed package's docs. Pin the version; update deliberately. |
| Wi-Fi tunnel doesn't work | Fall back to USB. Test in M0 before designing around it. |
| Silent revert while unattended | M3 is entirely this. Do not defer it. |
| Life360 detects via Wi-Fi/IP | Not solvable in software. Pick plausible destinations; keep speeds human. |
| Find My geofence alerts | A teleport fires "arrived/left" notifications instantly if anyone has one set on you. Glide avoids it. |

---

## The check

`test_spoofer.py`, assert-based, no framework:

- haversine distance and bearing against two known city pairs
- interpolation produces the requested point count, starts at A, ends at B,
  and every step is within tolerance of the target speed
- the point-list engine treats a 1-point list (teleport) and an N-point list
  (glide) identically

Nothing else needs a test. The pymobiledevice3 calls are I/O against a
physical phone; the gates at each milestone cover those.
