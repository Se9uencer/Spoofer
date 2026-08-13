# Spoofer

Tethered iOS location spoofer (Find My primary, Life360 secondary). macOS
host only. Built on `pymobiledevice3`. Full plan, milestones, and decision
rationale: [PLAN.md](PLAN.md) — read it before making architectural changes.

## Non-negotiable constraints

- **Engine takes a point list, never a single coordinate.** Teleport = 1
  point, glide = N interpolated points, future route drawing = 200 points.
  Do not special-case teleport as `set(lat, lon)`.
- **macOS only.** No Windows/Linux code paths.
- Three files: `spoofer.py`, `index.html`, `test_spoofer.py`. Resist a fourth.
- UI binds `127.0.0.1` by default; LAN mode is opt-in, requires a token, and
  checks `Origin`. Never loosen this without the user asking.
- `tunneld` runs as its own root process, independent of the app's lifecycle
  — the app never runs as root.
- Always `clear()` the spoofed location on exit (SIGINT/SIGTERM/normal) —
  a badly-terminated session can stick the device at a fake coordinate.
- **Never trust a library's own signal handling.** uvicorn's shutdown handler
  (`signal.signal()`-based) silently skipped our cleanup on real SIGTERM —
  see PLAN.md M2 result. Any new code that owns process lifetime must use
  `loop.add_signal_handler()` (proven reliable, used 3x now) and must be
  verified against an *active* spoof, not an idle one, or the bug hides.

## Out of scope (do not build unless asked)

Route drawing UI, joystick control, Windows support, PyInstaller/notarization,
GPX import/export, untethered persistence, auto-spoof-on-connect, iOS
Shortcuts integration.

## Environment notes

- Homebrew pulled in **Python 3.14.6** as a pipx dependency (not the system
  3.11) — `--userspace` no-sudo tunnel is actually available now, just not
  used yet (still on `tunneld` per the original decision).
- iPhone is iOS 26.6; Mac is macOS 26.5.2.
- `pymobiledevice3` 10.7.3 is installed via `pipx` (not a project venv) —
  its binary path (needed for `sudo`, since root's PATH excludes
  `~/.local/bin`):
  `/Users/ibrahimansari/Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3`
- `uv` is installed via Homebrew — `spoofer.py`/`test_spoofer.py` carry PEP
  723 inline dependency blocks, so `uv run spoofer.py <cmd>` handles the
  actual project dependency (`pymobiledevice3` as a library, pulling in
  FastAPI/uvicorn/pydantic) without a checked-in venv.
- M0–M4 are done and verified against the real phone (v1 complete). See
  PLAN.md / HANDOFF.md. Stretch: route drawing — only if asked.
