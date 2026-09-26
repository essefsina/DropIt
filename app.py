#!/usr/bin/env python3
"""DropIt desktop app - native window + system tray around the DropIt server.

Run:
    python app.py
Build a Windows .exe:
    pip install -r requirements.txt
    pyinstaller --noconfirm --onefile --windowed --name DropIt --icon assets/icon.ico app.py
"""

import argparse
import os
import sys
import threading

import dropit

VERSION = dropit.VERSION


# ---------------------------------------------------------------- js api
class Api:
    """Exposed to the web UI as window.pywebview.api."""

    def __init__(self, directory: str):
        self.directory = directory

    def open_folder(self) -> bool:
        try:
            if sys.platform == "win32":
                os.startfile(self.directory)  # noqa: S606
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", self.directory])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", self.directory])
            return True
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------- tray icon
def _tray_image():
    from PIL import Image, ImageDraw

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
                winreg.SetValueEx(k, "DropIt", 0, winreg.REG_SZ, f'"{sys.executable}"')
            else:
                try:
                    winreg.DeleteValue(k, "DropIt")
                except OSError:
                    pass
    except OSError:
        pass


def start_tray(server) -> None:
    """Best-effort system tray; the app works fine without it."""
    try:
        import pystray
    except ImportError:
        return

    def on_copy(icon, item):
        copy_text(server.url)

    def on_autostart(icon, item):
        set_autostart(not is_autostart())

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Copy phone link", on_copy, default=True),
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
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="DropIt desktop app")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "DropIt"))
    args = ap.parse_args()

    server = dropit.make_server(args.port, args.dir, desktop=True)
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

    start_tray(server)

    window = webview.create_window(
        "DropIt",
        f"http://127.0.0.1:{args.port}",
        width=800,
        height=720,
        min_size=(560, 600),
        js_api=Api(server.directory),
    )
    webview.start()
    os._exit(0)


if __name__ == "__main__":
    main()
