# DropIt Windows installer

Two exes come out of `build.bat`:

| File | What it is | You post… |
|---|---|---|
| `dist-installer\DropIt-Setup.exe` | Full offline installer (Inno Setup) | …to each GitHub Release, as an asset |
| `dist\DropItInstaller.exe` | Tiny stub, like Firefox's stub installer | …anywhere, once — it always installs the latest version |

## How the stub works

`stub.py` is a small downloader with a progress window. When run, it
fetches the newest `DropIt-Setup.exe` from your releases page and launches it.
The full installer then handles the real work:

- Installs per-user to `%LOCALAPPDATA%\DropIt` — no admin rights needed.
- Upgrades cleanly: same AppId, so a new Setup just replaces the old install.
- Kills any running DropIt before installing (no more locked-exe failures).
- Cleans up the old manual-install mess on first run: stray `DropIt.exe` on
  the Desktop, the old Startup-folder shortcut, and pre-installer copies in
  `%LOCALAPPDATA%\DropIt`.
- Adds Start Menu shortcuts; optional desktop shortcut during setup.
- The app's "Start with Windows" toggle keeps working across updates because
  the install path never changes.
- The exe is stamped with its version (right-click → Properties → Details),
  and the version also shows in the app's web UI.

The installer asset keeps a **stable file name** (`DropIt-Setup.exe`, no
version in it) so the stub's download URL never breaks:
`https://github.com/<you>/DropIt/releases/latest/download/DropIt-Setup.exe`

## One-time setup (on the Windows PC)

1. Install Inno Setup 6 from https://jrsoftware.org/isdl.php (free).
2. Set `DOWNLOAD_URL` at the top of `stub.py` to your releases URL.
3. On GitHub, create a Release (not a pre-release) and upload
   `dist-installer\DropIt-Setup.exe` as an asset.

## Every new version (on the Windows PC)

1. Unzip the latest `dropit-desktop.zip`, bump `VERSION` in `dropit.py`.
2. Run `build.bat` — it builds the app, the full installer, and the stub.
3. Upload the new `dist-installer\DropIt-Setup.exe` to a new GitHub Release.
4. That's it — the stub you already posted now installs the new version.
   No need to re-post anything.
