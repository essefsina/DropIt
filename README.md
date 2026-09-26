# 📥 DropIt

Beam files between your phone and PC over local WiFi. No cloud, no USB cable, no accounts.

## 🖥️ Desktop app (recommended)

1. On your **Windows PC**, unzip this folder and double-click **`build.bat`**
2. It installs everything and builds `dist\DropIt.exe` (takes a couple minutes the first time)
3. Double-click `DropIt.exe` — a real native window opens, with a tray icon while it runs

The app shows a **QR code** — scan it with your phone camera and you're connected. The tray menu lets you copy the phone link, toggle **Start with Windows**, and quit.

## 🐍 Quick run (no install)

The core is still a single stdlib-only Python file — nothing to install:

```bash
python dropit.py
```

Open the printed address (or scan the QR code) on your phone. Same WiFi required.

Options: `python dropit.py --port 9000 --dir ./shared`

## How it works

- Phone → PC: drop files on the page (or tap to browse), watch the progress bar
- PC → phone: use the Download button next to any file in the list
- Files land in `~/DropIt`; duplicates get `(1)`, `(2)` suffixes instead of overwriting
- Delete anything from the list when you're done

## Notes

- Both devices must be on the same WiFi network
- Windows may ask for firewall permission on first run — allow it
- The desktop window needs Microsoft Edge WebView2 (preinstalled on Windows 10/11)
- LAN-only by design — don't expose it to the internet

## Project layout

| File | What it is |
|---|---|
| `dropit.py` | The server + web UI. Stdlib only, runs anywhere |
| `app.py` | Desktop shell: native window, tray icon, autostart |
| `requirements.txt` | Desktop/build dependencies |
| `build.bat` | One-click Windows build → `DropIt.exe` |
| `assets/icon.ico` | App icon |

## Why it exists

Emailing yourself photos is a crime against convenience. This kills it.
