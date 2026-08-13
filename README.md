# Spoofer

Tethered iOS location spoofer for macOS (Find My primary, Life360 secondary).
Built on [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).

**Requirements:** macOS, iPhone with Developer Mode, USB or same Wi‑Fi, Homebrew `uv`, `pipx install pymobiledevice3`.

## Quick start

```bash
# One-time: tunnel daemon + always-on web UI (needs sudo once)
export PATH="$HOME/.local/bin:$PATH"
cd /path/to/Spoofer
uv run spoofer.py install-service
```

That prints a phone URL like `http://<lan-ip>:8731/?token=…` — open it in Safari → Share → Add to Home Screen.

Manual (no launchd):

```bash
# Terminal 1 — tunnel (or use the LaunchDaemon from install-service)
sudo "/path/to/pipx/venvs/pymobiledevice3/bin/pymobiledevice3" remote tunneld

# Terminal 2
uv run spoofer.py serve          # loopback only
uv run spoofer.py serve --lan    # LAN + token (stored in ~/.spoofer.json)
```

Self-check: `uv run test_spoofer.py`

## Layout

| File | Role |
|------|------|
| `spoofer.py` | Engine, FastAPI UI, CLI, `install-service` |
| `index.html` | Leaflet map UI |
| `test_spoofer.py` | Interpolator / auth self-check |
| `PLAN.md` | Milestones and decisions |
| `CLAUDE.md` | Non-negotiable constraints for contributors |

Config/favorites/token live in `~/.spoofer.json` (not in this repo).

## Notes

- Spoof lasts only while the tunnel is up. Lid-close sleep still kills it; `caffeinate` only blocks idle sleep.
- Always clears simulated location on exit (SIGINT/SIGTERM).
- Default bind is `127.0.0.1`. `--lan` requires the token and keeps the Origin check.
