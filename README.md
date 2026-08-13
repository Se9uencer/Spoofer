# Spoofer

Fake your iPhone’s GPS from a Mac (works with Find My; Life360 is hit-or-miss).
macOS only. Phone stays tethered to the Mac over USB or the same Wi‑Fi.

Built on [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).

## What you need

- A Mac (stays awake while spoofing — closing the lid will kill it)
- An iPhone on the **same Wi‑Fi** as the Mac, or plugged in with USB
- About 10 minutes for one-time setup

## Setup (friends — do this once)

### 1. Install tools

```bash
brew install uv pipx
pipx ensurepath
pipx install pymobiledevice3
```

Quit and reopen Terminal (so `~/.local/bin` is on your PATH).

### 2. Prepare the iPhone

1. Plug into the Mac once, tap **Trust**
2. Settings → Privacy & Security → **Developer Mode** → On → reboot when asked
3. After reboot, confirm Developer Mode is still on

### 3. Get this repo

```bash
git clone https://github.com/Se9uencer/Spoofer.git
cd Spoofer
export PATH="$HOME/.local/bin:$PATH"
```

### 4. Sanity check (optional but useful)

```bash
uv run test_spoofer.py
# expect: all checks passed
```

### 5. Install so it survives reboot

```bash
uv run spoofer.py install-service
```

Enter your Mac password when asked. It will print a URL like:

```text
http://192.168.x.x:8731/?token=LONG_SECRET
```

On the **iPhone** (Safari): open that URL → Share → **Add to Home Screen**.

Done. After a Mac reboot you shouldn’t need to touch Terminal again.

## Everyday use

1. Mac awake, phone on same Wi‑Fi (or USB)
2. Open the home-screen icon on the phone (or `http://127.0.0.1:8731` on the Mac)
3. Search / tap the map → **Set Location**
4. **Clear** when you’re done (or quit — it clears on exit)

**Glide** = walk there slowly instead of teleporting (better if someone has Find My geofences on you).

## If something breaks

| Symptom | Fix |
|--------|-----|
| “no device found via tunneld” | Phone unlocked, same Wi‑Fi/USB, Developer Mode on. Check `curl -s http://127.0.0.1:49151/` returns JSON. |
| Phone stuck on a fake place | With tunneld up: `pymobiledevice3 developer dvt simulate-location clear --rsd <addr> <port>` (addr/port from that curl). |
| Lost the phone URL / token | It’s in `~/.spoofer.json` under `"token"`, or rerun `uv run spoofer.py serve --lan` to print the URL again. |
| `sudo` can’t find `pymobiledevice3` | Use the full pipx path: `~/Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3` |

Manual mode (no launchd), if you prefer:

```bash
# Terminal 1
sudo "$HOME/Library/Application Support/pipx/venvs/pymobiledevice3/bin/pymobiledevice3" remote tunneld

# Terminal 2
uv run spoofer.py serve --lan
```

## Limits (read these)

- Spoof only lives while the Mac tunnel is up — not “set and forget on cellular alone”
- `caffeinate` blocks *idle* sleep, **not** closing the lid
- GPS only — Wi‑Fi/IP location can still show the truth (Life360 may notice)
- Your LAN token is a secret: anyone on your Wi‑Fi with that URL can move **your** pin

## Project layout

| File | Role |
|------|------|
| `spoofer.py` | Engine, web UI server, CLI, `install-service` |
| `index.html` | Map UI |
| `test_spoofer.py` | Self-check |
| `PLAN.md` | Design / milestones |
| `CLAUDE.md` | Contributor constraints |

Config, favorites, and token: `~/.spoofer.json` (never committed).
