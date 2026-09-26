#!/usr/bin/env python3
"""DropIt - beam files between your phone and PC over local WiFi.

No cloud, no USB cable, no accounts. Run it, open the shown address
(or scan the QR code) on your phone. Same WiFi required.

Usage:
    python dropit.py                 # serves on port 8000, saves to ~/DropIt
    python dropit.py --port 9000 --dir ./shared

Stdlib only for the core - no pip install needed. (QR codes appear
automatically if the `qrcode` package is installed.)
"""

import argparse
import html
import json
import mimetypes
import os
import re
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0.0"

try:
    import qrcode  # noqa: F401
    HAS_QR = True
except ImportError:
    HAS_QR = False

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DropIt</title>
<style>
  :root { --bg:#0b0e14; --card:#131829; --card2:#1a2136; --line:#263049; --txt:#eef1f8;
          --muted:#8b94ad; --acc:#38bdf8; --acc2:#818cf8; --good:#34d399; --bad:#f87171; }
  * { box-sizing:border-box; margin:0; }
  body { background:radial-gradient(1100px 500px at 50% -10%, #16213d 0%, var(--bg) 55%) fixed, var(--bg);
         color:var(--txt); font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         max-width:760px; margin:0 auto; padding:26px 18px 70px; }
  .topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:4px; }
  .brand { display:flex; align-items:center; gap:13px; }
  .logo { width:46px; height:46px; border-radius:14px; background:linear-gradient(135deg,var(--acc),var(--acc2));
          display:flex; align-items:center; justify-content:center; font-size:25px;
          box-shadow:0 8px 24px rgba(56,189,248,.35); }
  h1 { font-size:25px; letter-spacing:-.02em; }
  .ver { font-size:11px; color:var(--muted); border:1px solid var(--line); padding:3px 9px; border-radius:20px; }
  .status { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--good); box-shadow:0 0 12px var(--good);
         animation:pulse 2.2s infinite; }
  @keyframes pulse { 50% { opacity:.45; } }
  .tagline { color:var(--muted); font-size:14px; margin:4px 0 22px; }
  .card { background:linear-gradient(180deg,var(--card2),var(--card)); border:1px solid var(--line);
          border-radius:18px; padding:22px; margin-bottom:16px; box-shadow:0 12px 32px rgba(0,0,0,.35); }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); margin-bottom:16px; }
  .connect { display:flex; gap:20px; align-items:center; flex-wrap:wrap; }
  .urlbox { flex:1; min-width:250px; }
  .url { background:#090c15; border:1px solid var(--line); border-radius:12px; padding:14px 16px;
         display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .url a { color:var(--acc); text-decoration:none; font-size:19px; font-weight:700; word-break:break-all; }
  .copybtn { border:1px solid var(--line); background:var(--card2); color:var(--txt); border-radius:9px;
             padding:8px 14px; font-size:13px; cursor:pointer; white-space:nowrap; }
  .copybtn:hover { border-color:var(--acc); }
  .qr { text-align:center; }
  .qr img { width:136px; height:136px; border-radius:12px; background:#fff; padding:7px;
            border:1px solid var(--line); }
  .qr div { font-size:12px; color:var(--muted); margin-top:8px; }
  .hint { color:var(--muted); font-size:13px; margin-top:14px; line-height:1.55; }
  #drop { border:2px dashed var(--line); border-radius:14px; padding:44px 16px; text-align:center;
          cursor:pointer; transition:border-color .15s, background .15s; }
  #drop:hover { border-color:var(--acc2); }
  #drop.over { border-color:var(--acc); background:rgba(56,189,248,.07); transform:scale(1.005); }
  #drop .big { font-size:18px; font-weight:650; margin-bottom:8px; }
  #drop .small { color:var(--muted); font-size:13px; }
  #drop .small b { color:var(--txt); }
  #uploads { margin-top:14px; display:flex; flex-direction:column; gap:9px; }
  .up { background:#090c15; border:1px solid var(--line); border-radius:11px; padding:11px 14px; font-size:14px; }
  .up .bar { height:6px; background:var(--line); border-radius:3px; margin-top:9px; overflow:hidden; }
  .up .bar > div { height:100%; width:0%; background:linear-gradient(90deg,var(--acc),var(--acc2)); transition:width .12s; }
  .up.done .bar > div { background:var(--good); }
  .up.err { color:var(--bad); }
  .frow { display:flex; align-items:center; gap:12px; padding:11px 4px; border-top:1px solid var(--line); font-size:14px; }
  .frow:first-child { border-top:none; }
  .fic { font-size:20px; }
  .fname { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .fsize { color:var(--muted); font-size:12px; white-space:nowrap; }
  .btn { border:1px solid var(--line); background:#090c15; color:var(--txt); border-radius:9px;
         padding:8px 14px; font-size:13px; cursor:pointer; text-decoration:none; white-space:nowrap; }
  .btn:hover { border-color:var(--acc); }
  .btn.danger:hover { border-color:var(--bad); color:var(--bad); }
  .empty { color:var(--muted); font-size:14px; padding:10px 4px; }
  .cardhead { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
  .cardhead h2 { margin-bottom:0; }
  footer { text-align:center; color:var(--muted); font-size:12px; margin-top:26px; }
  .toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%) translateY(20px); background:var(--card2);
           border:1px solid var(--acc); color:var(--txt); padding:11px 20px; border-radius:12px; font-size:14px;
           opacity:0; transition:.25s; pointer-events:none; z-index:50; }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <div class="logo">&#x1F4E5;</div>
    <div><h1>DropIt</h1></div>
    <span class="ver">v__VERSION__</span>
  </div>
  <div class="status"><span class="dot"></span>Running</div>
</div>
<p class="tagline">Beam files between your phone and PC over WiFi. Nothing leaves your network.</p>

<div class="card">
  <h2>Connect your phone</h2>
  <div class="connect">
    <div class="urlbox">
      <div class="url"><a href="__URL__">__URL__</a><button class="copybtn" id="copy">Copy</button></div>
      <p class="hint">Open this address on your phone &mdash; both devices need the same WiFi.<br>First run may ask for firewall permission &mdash; allow it.</p>
    </div>
    __QR__
  </div>
</div>

<div class="card">
  <h2>Send files</h2>
  <div id="drop">
    <div class="big">Drop files here</div>
    <div class="small">or click to browse &mdash; they land in <b>__DIRNAME__</b></div>
    <input type="file" id="picker" multiple style="display:none">
  </div>
  <div id="uploads"></div>
</div>

<div class="card">
  <div class="cardhead"><h2>On this PC</h2>__DESKTOP__</div>
  <div id="files"><p class="empty">Loading&hellip;</p></div>
</div>

<footer>DropIt &middot; local only &middot; no cloud, no accounts, no tracking</footer>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const drop = $("#drop"), picker = $("#picker");
const esc = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function fmt(n){ if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(1)+" KB";
  if(n<1073741824) return (n/1048576).toFixed(1)+" MB"; return (n/1073741824).toFixed(2)+" GB"; }
function iconFor(n){ const e=n.split(".").pop().toLowerCase();
  if(["png","jpg","jpeg","gif","webp","svg","heic","bmp"].includes(e)) return "&#x1F5BC;&#xFE0F;";
  if(["mp4","mov","mkv","webm","avi"].includes(e)) return "&#x1F3AC;";
  if(["mp3","wav","flac","m4a","ogg"].includes(e)) return "&#x1F3B5;";
  if(["zip","rar","7z","tar","gz"].includes(e)) return "&#x1F4E6;";
  if(["pdf"].includes(e)) return "&#x1F4D5;";
  return "&#x1F4C4;"; }
function toast(msg){ const t=$("#toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),2200); }

$("#copy").onclick = async () => {
  const url = "__URL__";
  try { await navigator.clipboard.writeText(url); }
  catch(e){ const i=document.createElement("input"); i.value=url; document.body.appendChild(i);
    i.select(); document.execCommand("copy"); i.remove(); }
  toast("Link copied \\u2014 send it to your phone");
};

const of = $("#openfolder");
if(of) of.onclick = () => { try { window.pywebview.api.open_folder(); } catch(e){} };

drop.onclick = () => picker.click();
["dragover","dragenter"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", ev => uploadFiles(ev.dataTransfer.files));
picker.addEventListener("change", () => { uploadFiles(picker.files); picker.value=""; });

function uploadFiles(files){
  [...files].forEach(f => {
    const row = document.createElement("div");
    row.className = "up";
    row.innerHTML = `<div>${esc(f.name)} <span style="color:var(--muted)">(${fmt(f.size)})</span></div><div class="bar"><div></div></div>`;
    $("#uploads").prepend(row);
    const bar = row.querySelector(".bar > div");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = ev => { if(ev.lengthComputable) bar.style.width = (ev.loaded/ev.total*100)+"%"; };
    xhr.onload = () => {
      if(xhr.status === 200){ row.classList.add("done"); bar.style.width="100%";
        toast("\\u2705 "+f.name+" received"); loadFiles(); }
      else { row.classList.add("err"); row.firstElementChild.textContent = "Upload failed: "+f.name; }
      setTimeout(()=>row.remove(), 4500);
    };
    xhr.onerror = () => { row.classList.add("err"); row.firstElementChild.textContent = "Upload failed: "+f.name; };
    const fd = new FormData(); fd.append("file", f);
    xhr.send(fd);
  });
}

async function loadFiles(){
  const box = $("#files");
  try {
    const r = await fetch("/api/files"); const files = await r.json();
    if(!files.length){ box.innerHTML = '<p class="empty">Nothing here yet &mdash; beam something over from your phone.</p>'; return; }
    box.innerHTML = files.map((f,i) =>
      `<div class="frow"><span class="fic">${iconFor(f.name)}</span>` +
      `<span class="fname" title="${esc(f.name)}">${esc(f.name)}</span>` +
      `<span class="fsize">${fmt(f.size)}</span>` +
      `<a class="btn" href="/dl/${encodeURIComponent(f.name)}">Download</a>` +
      `<button class="btn danger" data-del="${i}">Delete</button></div>`
    ).join("");
    box.querySelectorAll("[data-del]").forEach(b =>
      b.onclick = () => delFile(files[+b.dataset.del].name));
  } catch(e){ box.innerHTML = '<p class="empty">Could not load file list.</p>'; }
}

async function delFile(name){
  if(!confirm("Delete "+name+"?")) return;
  await fetch("/api/delete", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name})});
  loadFiles(); toast("Deleted "+name);
}

loadFiles(); setInterval(loadFiles, 5000);
</script>
</body>
</html>
"""


def lan_ip() -> str:
    """Best-effort LAN IP without sending any traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def safe_name(name: str) -> str:
    name = os.path.basename(name).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "upload"


def unique_path(directory: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return candidate


def parse_multipart(body: bytes, boundary: bytes):
    """Yield (filename, content) for file parts. Binary-safe."""
    files = []
    for part in body.split(b"--" + boundary):
        if part in (b"", b"--\r\n", b"--", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"--"):
            part = part[:-2]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        m = re.search(r'filename="([^"]*)"', head.decode("latin-1", "replace"))
        if m:
            files.append((safe_name(m.group(1)), content))
    return files


def qr_png(url: str) -> bytes:
    """PNG bytes of a QR code for url. Raises if qrcode/Pillow missing."""
    import io

    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = f"DropIt/{VERSION}"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        if self.path == "/":
            qr_block = (
                '<div class="qr"><img src="/qr.png" alt="QR code">'
                "<div>Scan with your<br>phone camera</div></div>"
                if HAS_QR else ""
            )
            desktop_btn = (
                '<button class="btn" id="openfolder">&#x1F4C1; Open folder</button>'
                if getattr(self.server, "desktop", False) else ""
            )
            page = (
                PAGE.replace("__VERSION__", VERSION)
                .replace("__URL__", html.escape(self.server.url))
                .replace("__DIRNAME__", html.escape(os.path.basename(self.server.directory)))
                .replace("__QR__", qr_block)
                .replace("__DESKTOP__", desktop_btn)
            )
            self._send(200, page.encode())
        elif self.path == "/qr.png":
            try:
                self._send(200, qr_png(self.server.url), "image/png")
            except Exception:  # noqa: BLE001 - qrcode/Pillow not installed
                self._send(404, b"qr unavailable", "text/plain")
        elif self.path == "/api/files":
            items = []
            for name in sorted(os.listdir(self.server.directory)):
                p = os.path.join(self.server.directory, name)
                if os.path.isfile(p):
                    items.append({"name": name, "size": os.path.getsize(p)})
            self._json(items)
        elif self.path.startswith("/dl/"):
            name = safe_name(urllib.parse.unquote(self.path[len("/dl/"):]))
            path = os.path.join(self.server.directory, name)
            if not os.path.isfile(path):
                self._send(404, b"not found", "text/plain")
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        ctype = self.headers.get("Content-Type") or ""

        if self.path == "/api/upload" and "multipart/form-data" in ctype:
            m = re.search(r"boundary=([^;]+)", ctype)
            if not m or length <= 0:
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            body = self.rfile.read(length)
            saved = []
            for filename, content in parse_multipart(body, m.group(1).encode()):
                dest = unique_path(self.server.directory, filename)
                with open(dest, "wb") as f:
                    f.write(content)
                saved.append(os.path.basename(dest))
                self.log_message("saved %s (%d bytes)", dest, len(content))
            self._json({"ok": True, "saved": saved})
        elif self.path == "/api/delete":
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                name = safe_name(data.get("name", ""))
                path = os.path.join(self.server.directory, name)
                if os.path.isfile(path):
                    os.remove(path)
                    self._json({"ok": True})
                else:
                    self._json({"ok": False}, 404)
            except (json.JSONDecodeError, OSError):
                self._json({"ok": False}, 400)
        else:
            self._send(404, b"not found", "text/plain")


def make_server(port: int, directory: str, desktop: bool = False) -> ThreadingHTTPServer:
    os.makedirs(directory, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.url = f"http://{lan_ip()}:{port}"
    server.directory = os.path.abspath(directory)
    server.desktop = desktop
    server.daemon_threads = True
    return server


def main():
    ap = argparse.ArgumentParser(description="DropIt - beam files phone <-> PC over WiFi")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "DropIt"))
    args = ap.parse_args()

    server = make_server(args.port, args.dir)

    print(f"\n  DropIt {VERSION} running!")
    print(f"  On your phone, open:  {server.url}")
    print(f"  Files land in:        {server.directory}")
    print("  Stop with Ctrl+C\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
