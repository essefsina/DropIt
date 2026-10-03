"""DropIt stub installer — works like Firefox's stub installer.

A tiny downloader with a progress window: it fetches the latest full
DropIt-Setup.exe from your GitHub Releases "latest" URL, then runs it.
The full installer (Inno Setup) handles the actual install/upgrade.

Built on the Windows PC by build.bat:
    pyinstaller --noconfirm --onefile --windowed --name DropItInstaller \
        --icon icon.ico --add-data "icon.ico;." stub.py

Stdlib only (tkinter + urllib) — no extra pip deps.
"""

import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

# ---------------------------------------------------------------------------
# The stable download URL of the FULL installer.
# Upload every release's installer asset with the SAME file name
# (DropIt-Setup.exe) and this URL always serves the newest release:
#     https://github.com/<your-username>/DropIt/releases/latest/download/DropIt-Setup.exe
# ---------------------------------------------------------------------------
DOWNLOAD_URL = "https://github.com/REPLACE-ME/DropIt/releases/latest/download/DropIt-Setup.exe"

SETUP_FILENAME = "DropIt-Setup.exe"
CHUNK_SIZE = 256 * 1024


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class StubApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("DropIt Setup")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic
            pass

        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        self.status = ttk.Label(frame, text="Connecting…")
        self.status.pack(anchor=tk.W, pady=(0, 10))

        self.progress = ttk.Progressbar(frame, length=340, mode="determinate")
        self.progress.pack(fill=tk.X)

        # Center on screen
        self.root.update_idletasks()
        w, h = 380, 130
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        threading.Thread(target=self.download_and_run, daemon=True).start()

    def set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status.config(text=text))

    def set_progress(self, value: float, maximum: float) -> None:
        def update() -> None:
            self.progress.config(mode="determinate", maximum=maximum, value=value)

        self.root.after(0, update)

    def fail(self, message: str) -> None:
        def show() -> None:
            messagebox.showerror("DropIt Setup", message)
            self.root.destroy()

        self.root.after(0, show)

    def download_and_run(self) -> None:
        if "REPLACE-ME" in DOWNLOAD_URL:
            self.fail(
                "This installer was built without a download URL.\n"
                "Ask Essef for a fresh DropItInstaller.exe."
            )
            return

        dest = os.path.join(tempfile.gettempdir(), SETUP_FILENAME)
        try:
            self.set_status("Downloading DropIt…")
            req = urllib.request.Request(
                DOWNLOAD_URL, headers={"User-Agent": "DropIt-Stub-Installer"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
                total = resp.getheader("Content-Length")
                total = int(total) if total else 0
                downloaded = 0
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        self.set_progress(downloaded, total)
                        pct = downloaded * 100 // total
                        self.set_status(f"Downloading DropIt… {pct}%")
            if os.path.getsize(dest) == 0:
                raise OSError("downloaded file is empty")
        except Exception as exc:  # noqa: BLE001 - show any failure to the user
            try:
                os.remove(dest)
            except OSError:
                pass
            self.fail(
                f"Couldn't download DropIt:\n{exc}\n\n"
                "Check your internet connection and try again."
            )
            return

        self.set_status("Starting installer…")
        try:
            # Hand off to the full installer, then get out of the way.
            subprocess.Popen([dest], close_fds=True)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"Couldn't start the installer:\n{exc}")
            return
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    StubApp().run()
