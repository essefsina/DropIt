#!/usr/bin/env python3
"""DropIt desktop app - native window + system tray around the DropIt server.

Run:
    python app.py
Build a Windows .exe:
    pip install -r requirements.txt
    pyinstaller --noconfirm --onefile --windowed --name DropIt --icon icon.ico app.py
"""

import argparse
import json
import os
import subprocess
import sys
import threading

import dropit

VERSION = dropit.VERSION


# ------------------------------------------------------------ saved config
def _config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "DropIt")
    else:
        folder = os.path.join(os.path.expanduser("~"), ".config", "DropIt")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


# ---------------------------------------------------------------- js api
def _browse_for_folder_win(title="Choose a folder", initial=None):
    """Native Windows folder picker (SHBrowseForFolderW).

    pywebview's file dialog opens a *file* picker even with directory=True,
    so this calls the real Win32 folder browser instead. Runs its own modal
    loop, so it works from any thread. Returns the path or None on cancel.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BFFM_INITIALIZED = 1
    BFFM_SETSELECTIONW = 0x467  # WM_USER + 103

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", ctypes.c_uint),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    init_path = os.path.abspath(os.path.expanduser(initial)) if initial else None

    @ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint,
                        wintypes.LPARAM, wintypes.LPARAM)
    def _cb(hwnd, msg, lp, data):
        if msg == BFFM_INITIALIZED and init_path:
            user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, init_path)
        return 0

    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                    wintypes.WPARAM, wintypes.LPCWSTR]
    user32.SendMessageW.restype = wintypes.LPARAM
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    # NOTE: every one of these needs explicit argtypes. Without them ctypes
    # defaults to c_int, which cannot hold a 64-bit pointer/address and dies
    # with "OverflowError: int too long to convert".
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long  # HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    # The new-style folder dialog needs COM on this thread. pywebview's
    # JS bridge may call us from a thread where COM isn't initialized,
    # which makes the dialog fail outright.
    COINIT_APARTMENTTHREADED = 0x2
    com_ok = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        display = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        bi = BROWSEINFOW()
        bi.hwndOwner = None
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        bi.lpfn = ctypes.cast(_cb, ctypes.c_void_p)
        bi.lParam = 0
        bi.iImage = 0

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        try:
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if shell32.SHGetPathFromIDListW(pidl, buf):
                return buf.value or None
            return None
        finally:
            ole32.CoTaskMemFree(pidl)
            _cb  # keep the callback alive until the dialog is gone  # noqa: B018
    finally:
        if com_ok == 0:  # S_OK: we initialized COM, so uninitialize it
            ole32.CoUninitialize()


class Api:
    """Exposed to the web UI as window.pywebview.api."""

    def __init__(self, server):
        self.server = server

    def open_folder(self) -> bool:
        try:
            if sys.platform == "win32":
                os.startfile(self.server.directory)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.server.directory])
            else:
                subprocess.Popen(["xdg-open", self.server.directory])
            return True
        except Exception:  # noqa: BLE001
            return False

    def open_file(self, name: str) -> bool:
        """Open a file in the save folder with its default app."""
        try:
            clean = dropit.safe_name(name)
            path = os.path.abspath(os.path.join(self.server.directory, clean))
            if not path.startswith(os.path.abspath(self.server.directory) + os.sep):
                return False
            if not os.path.isfile(path):
                return False
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception:  # noqa: BLE001
            return False

    def open_path(self, path: str) -> bool:
        """Open an arbitrary local file (e.g. a note the user saved via Save dialog)."""
        try:
            p = os.path.abspath(os.path.expanduser(str(path)))
            if not os.path.isfile(p):
                return False
            if sys.platform == "win32":
                os.startfile(p)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_directory(self, path: str) -> dict:
        """Change the save folder from the web UI. Returns JSON-serializable."""
        try:
            raw = str(path).strip().strip('"')
            if not raw:
                return {"ok": False, "error": "empty path"}
            new = os.path.abspath(os.path.expanduser(raw))
            if sys.platform == "win32":
                drive = os.path.splitdrive(new)[0]
                if drive and not os.path.isdir(drive + os.sep):
                    return {"ok": False, "error": f"drive {drive} not found"}
            os.makedirs(new, exist_ok=True)
            self.server.directory = new
            cfg = load_config()
            cfg["directory"] = new
            save_config(cfg)
            return {"ok": True, "directory": new, "dirname": os.path.basename(new)}
        except OSError as e:
            return {"ok": False, "error": str(e)}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "unexpected error"}

    def browse_directory(self) -> dict:
        """Let the user pick the save folder with a native folder dialog."""
        if sys.platform == "win32":
            # Real folder picker. pywebview's dialog opens a *file* picker
            # even with directory=True, so it is not used here on Windows.
            try:
                path = _browse_for_folder_win(
                    title="Choose the DropIt save folder",
                    initial=self.server.directory,
                )
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                return {"ok": False,
                        "error": "Folder picker failed (%s: %s)"
                                 % (type(e).__name__, e)}
            if not path:
                return {"ok": False, "error": "cancelled"}
            try:
                return self.set_directory(path)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}
        try:
            import webview

            wins = getattr(webview, "windows", [])
            if not wins:
                return {"ok": False, "error": "no window"}
            result = wins[0].create_file_dialog(
                webview.OPEN_DIALOG,
                directory=True,
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "error": "cancelled"}
            path = result[0] if isinstance(result, (list, tuple)) else result
            return self.set_directory(path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def save_note(self, params) -> dict:
        """Save a text note through a native save dialog (desktop window)."""
        try:
            import webview

            text = str(params.get("text", "")) if isinstance(params, dict) else ""
            nid = str(params.get("id", "note")) if isinstance(params, dict) else "note"
            wins = getattr(webview, "windows", [])
            if not wins:
                return {"ok": False, "error": "no window"}
            safe = "".join(c for c in nid if c.isalnum())[:32] or "note"
            result = wins[0].create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename="shared-text-%s.txt" % safe,
                file_types=("Text files (*.txt)",),
            )
            if not result:
                return {"ok": False, "error": "cancelled"}
            path = result[0] if isinstance(result, (list, tuple)) else result
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return {"ok": True, "path": path}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def get_clipboard(self) -> dict:
        """Read text from the native clipboard (no browser permission prompt)."""
        try:
            import tkinter

            r = tkinter.Tk()
            r.withdraw()
            try:
                text = r.clipboard_get()
            except Exception:  # noqa: BLE001
                text = ""  # empty or non-text clipboard
            r.destroy()
            return {"ok": True, "text": text or ""}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": "", "error": str(e)}


# --------------------------------------------------------------- tray icon
def _tray_image():
    from PIL import Image

    real = dropit.asset_path("icon.png")
    if real:
        try:
            img = Image.open(real).convert("RGBA")
            img.thumbnail((64, 64), Image.LANCZOS)
            return img
        except Exception:  # noqa: BLE001
            pass
    # fallback: generated arrow icon
    from PIL import ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, size - 4, size - 4], radius=14, fill=(56, 189, 248, 255))
    # down arrow into tray
    d.rectangle([size // 2 - 5, 14, size // 2 + 5, 38], fill="white")
    d.polygon(
        [(size // 2 - 13, 36), (size // 2 + 13, 36), (size // 2, 50)],
        fill="white",
    )
    d.rectangle([16, 50, size - 16, 55], fill="white")
    return img


def copy_text(text: str) -> None:
    try:
        import tkinter

        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------- autostart
def _run_key():
    import winreg

    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_READ | winreg.KEY_SET_VALUE,
    )


def is_autostart() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with _run_key() as k:
            winreg.QueryValueEx(k, "DropIt")
        return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        with _run_key() as k:
            import winreg

            if enable:
                # --minimized: start hidden in the tray, no window popup on boot
                winreg.SetValueEx(
                    k, "DropIt", 0, winreg.REG_SZ, f'"{sys.executable}" --minimized'
                )
            else:
                try:
                    winreg.DeleteValue(k, "DropIt")
                except OSError:
                    pass
    except OSError:
        pass


def start_tray(server, window=None):
    """Best-effort system tray; the app works fine without it. Returns the icon."""
    try:
        import pystray
    except ImportError:
        return None

    def on_open(icon, item):
        try:
            if window is not None:
                window.show()
                window.restore()
        except Exception:  # noqa: BLE001
            pass

    def on_copy(icon, item):
        copy_text(server.url)

    def on_autostart(icon, item):
        set_autostart(not is_autostart())

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open DropIt", on_open, default=True),
        pystray.MenuItem("Copy phone link", on_copy),
        pystray.MenuItem(
            "Start with Windows",
            on_autostart,
            checked=lambda item: is_autostart(),
            enabled=sys.platform == "win32" and getattr(sys, "frozen", False),
        ),
        pystray.MenuItem("Quit DropIt", on_quit),
    )
    try:
        icon = pystray.Icon("DropIt", _tray_image(), f"DropIt - {server.url}", menu)
        icon.run_detached()
        return icon
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="DropIt desktop app")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=None,
                    help="Save folder (overrides the remembered one)")
    ap.add_argument("--minimized", action="store_true",
                    help="Start hidden in the system tray")
    args = ap.parse_args()

    cfg = load_config()
    directory = (
        args.dir
        or cfg.get("directory")
        or os.path.join(os.path.expanduser("~"), "DropIt")
    )

    server = dropit.make_server(args.port, directory, desktop=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"DropIt {VERSION} - {server.url} -> {server.directory}")

    try:
        import webview
    except ImportError:
        print("pywebview not installed - opening in browser instead.")
        print("Install the desktop shell with: pip install -r requirements.txt")
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    window = webview.create_window(
        "DropIt",
        f"http://127.0.0.1:{args.port}",
        width=1280,
        height=800,
        min_size=(720, 600),
        js_api=Api(server),
        hidden=args.minimized,
    )

    start_tray(server, window)

    # Minimize-to-tray: the minimize button hides the window to the tray.
    # Closing (X) exits the app for real. Reopen from the tray (double-click
    # it or choose Open DropIt); quit from the tray menu.
    def on_minimized():
        try:
            window.hide()
        except Exception:  # noqa: BLE001
            pass

    try:
        window.events.minimized += on_minimized
    except Exception:  # noqa: BLE001
        pass

    webview.start()
    os._exit(0)


if __name__ == "__main__":
    main()
