# Handoff — resuming in Cursor

Session paused here to switch tools. This is a self-contained "what to know
to keep going" doc — `PLAN.md` and `CLAUDE.md` are the source of truth for
scope/decisions, this is just the fast-resume path.

## Status: M0–M4 done and verified against the real phone. v1 complete. Stretch (route drawing) optional.

## What exists

```
spoofer.py       engine (SpoofEngine) + FastAPI web server + CLI (teleport/glide/clear/serve)
index.html       Leaflet map + Nominatim search + favorites + glide toggle, served at /
test_spoofer.py  assert-based self-check for the interpolator
PLAN.md          full plan, milestones, decision log, results — read this first
CLAUDE.md        non-negotiable constraints for any AI/dev touching this repo
~/.spoofer.json  favorites (created on first save; not checked into the repo)
```

## To resume right now

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /Users/ibrahimansari/Spoofer

# 1. Bring up the tunnel (needs your phone plugged in or on the same Wi-Fi,
#    Developer Mode already enabled from M0). Run in its own terminal, stays open:
sudo "/Users/ibrahimansari/Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3" remote tunneld

# 2. Confirm it sees the phone:
curl -s http://127.0.0.1:49151/

# 3. Run the self-check:
uv run test_spoofer.py

# 4. Start the web UI:
uv run spoofer.py serve
# open http://127.0.0.1:8731
```

## If the phone is ever stuck on a fake location and nothing else is running

```bash
export PATH="$HOME/.local/bin:$PATH"
pymobiledevice3 developer dvt simulate-location clear --rsd <addr> <port>
```
(`<addr>`/`<port>` from `curl -s http://127.0.0.1:49151/` while `tunneld` is up.)

## What's next — M4 (per PLAN.md)

M3 code is in (`reconnect` / `notify_macos` / `caffeinate` / amber banner). Still needs the physical gate:
yank mid-spoof → notification → restore → position returns on its own.

Implemented:
- Reconnect loop: on tunnel drop, re-establish, re-mount DDI if needed,
  re-apply the last position
- macOS notification on drop **and** recovery — `osascript -e 'display
  notification ...'`, one line, stdlib `subprocess`, no dependency
- `caffeinate -i` held as a child process for the lifetime of an active spoof
- Gate: yank the connection mid-spoof, notification fires, restore it,
  position comes back on its own

Then M4 (launchd install for zero-friction boot, LAN mode + token so the
iPhone itself can drive it) and the route-drawing stretch feature — both
untouched, fully speced in PLAN.md.

## Things that will bite you if skipped

- **Signal handling is not "just works."** Any new code that owns process
  shutdown (a new server, a new CLI mode, a launchd wrapper in M4) must use
  `loop.add_signal_handler()`, and must be tested against an **active** spoof
  specifically — testing with an already-cleared spoof hides the failure.
  Full story with the exact bug that was found: PLAN.md → M2 result.
- `sudo` can't see `pipx`-installed binaries — root's PATH excludes
  `~/.local/bin`. Always use the full pipx venv path shown above for
  anything running under `sudo` or `launchd`.
- `uv run <file>.py` reads dependencies from *that file's own* PEP 723
  header, not from whatever it imports. Any new standalone script that
  transitively needs `pymobiledevice3` needs its own header:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["pymobiledevice3>=10.7"]
  # ///
  ```
- The engine's whole design rests on **point lists**, never a single
  coordinate — teleport is a 1-point list, glide is N points. Don't
  special-case teleport; the route-drawing stretch feature depends on this
  staying true.
- Real, verified environment facts (not assumptions): Python 3.14.6 is
  actually installed (via Homebrew, pulled in by `pipx`), `pymobiledevice3`
  10.7.3, `uv` via Homebrew. iPhone is iOS 26.6, Mac is macOS 26.5.2.
