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
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.7.43"

_SVG_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>'
_SVG_FOLDER_OPEN = '<svg viewBox="0 0 24 24"><path d="M2.5 6a2 2 0 0 1 2-2h4l2 2.5h9a2 2 0 0 1 2 2V9h-19V6z" fill="#fbbf24"/><path d="M2.5 9h19v9a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2V9z" fill="#ffedd5"/><path d="M2.5 12.5h19V18a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2v-5.5z" fill="#ffffff"/></svg>'
_SVG_FOLDER_EDIT = '<svg viewBox="0 0 24 24"><path d="M2.5 7a2 2 0 0 1 2-2h4l2 2.5h6a2 2 0 0 1 2 2V9h-16V7z" fill="#a78bfa"/><path d="M2.5 9h16v9a2 2 0 0 1-2 2h-12a2 2 0 0 1-2-2V9z" fill="#ddd6fe"/><g transform="rotate(45 14.5 13.5)"><rect x="13.1" y="7.5" width="2.8" height="8.5" rx="1.2" fill="#fbbf24"/><path d="M13.1 16h2.8l-1.4 2.4z" fill="#78350f"/></g></svg>'

try:
    import qrcode  # noqa: F401
    HAS_QR = True
except ImportError:
    HAS_QR = False


# ------------------------------------------------------------ text notes
# Shared texts live in the app's own data dir (notes.json) -- NOT as files
# in the shared folder. The folder stays 100% the user's files.
_notes_lock = threading.Lock()

# Recently seen remote clients (ip -> last-seen timestamp). The desktop's own
# loopback traffic is excluded so the count reflects real connected devices.
_seen_lock = threading.Lock()
_seen_ips = {}
_SEEN_WINDOW = 20  # seconds

def _note_seen(ip):
    if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return
    with _seen_lock:
        _seen_ips[ip] = time.time()

def _device_count():
    now = time.time()
    with _seen_lock:
        for ip in [ip for ip, ts in _seen_ips.items() if now - ts >= _SEEN_WINDOW]:
            del _seen_ips[ip]
        return len(_seen_ips)

def _data_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "DropIt")
    else:
        folder = os.path.join(os.path.expanduser("~"), ".config", "DropIt")
    os.makedirs(folder, exist_ok=True)
    return folder

def _notes_path():
    return os.path.join(_data_dir(), "notes.json")

def load_notes():
    try:
        with open(_notes_path(), encoding="utf-8") as f:
            data = json.load(f)
        notes = [n for n in data
                 if isinstance(n, dict) and n.get("id") and isinstance(n.get("text"), str)]
        notes.sort(key=lambda n: n.get("created", 0), reverse=True)
        return notes
    except (OSError, ValueError):
        return []

def save_notes(notes):
    tmp = _notes_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes, f)
    os.replace(tmp, _notes_path())

def _new_note_id(notes):
    taken = {n.get("id") for n in notes}
    nid = "n%d" % int(time.time() * 1000)
    while nid in taken:
        nid += "1"
    return nid

def add_note(text):
    notes = load_notes()
    note = {"id": _new_note_id(notes), "text": text, "created": int(time.time())}
    notes.append(note)
    save_notes(notes)
    return note

def delete_note(nid):
    notes = load_notes()
    kept = [n for n in notes if n.get("id") != nid]
    if len(kept) == len(notes):
        return False
    save_notes(kept)
    return True

def edit_note(nid, text):
    notes = load_notes()
    for n in notes:
        if n.get("id") == nid:
            n["text"] = text
            save_notes(notes)
            return True
    return False

def migrate_snippets(directory):
    """One-time import of legacy snippet-*.txt files into the notes store."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    moved = []
    for name in names:
        ln = name.lower()
        if not (ln.startswith("snippet-") and ln.endswith(".txt")):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
            m = re.match(r"snippet-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name, re.I)
            if m:
                y, mo, d, h, mi, s = (int(g) for g in m.groups())
                created = int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))
            else:
                created = int(os.path.getmtime(path))
            if text:
                moved.append((text, created))
            os.remove(path)
        except OSError:
            continue
    if moved:
        with _notes_lock:
            notes = load_notes()
            for text, created in moved:
                notes.append({"id": _new_note_id(notes), "text": text, "created": created})
            notes.sort(key=lambda n: n.get("created", 0), reverse=True)
            save_notes(notes)

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="DropIt">
<meta name="theme-color" content="#0a0e1a">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<title>DropIt</title>
<style>
  :root { --bg:#0a0d16; --card:#12172a; --card2:#1a2140; --line:#2a3560; --txt:#eef1f8;
          --muted:#8f99b8; --acc:#22d3ee; --acc2:#a78bfa; --good:#34d399; --bad:#fb7185; }
  * { box-sizing:border-box; margin:0; }
  body { background:radial-gradient(1100px 500px at 50% -10%, #16213d 0%, var(--bg) 55%) fixed, var(--bg);
         color:var(--txt); font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         max-width:1320px; margin:0 auto; padding:26px 18px 70px;
         height:100vh; height:100dvh; display:flex; flex-direction:column; }
  .topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;
            flex-wrap:wrap; gap:10px; }
  .brand { display:flex; align-items:center; gap:13px; }
  .logo { width:46px; height:46px; border-radius:14px; background:linear-gradient(135deg,var(--acc),var(--acc2));
          display:flex; align-items:center; justify-content:center; font-size:25px;
          box-shadow:0 8px 24px rgba(56,189,248,.35); }
  .logoimg { width:46px; height:46px; border-radius:14px;
          box-shadow:0 8px 24px rgba(56,189,248,.35); }
  h1 { font-size:25px; letter-spacing:-.02em; }
  .ver { font-size:11px; color:var(--muted); border:1px solid var(--line); padding:3px 9px; border-radius:20px; }
  .status { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--good); box-shadow:0 0 12px var(--good);
         animation:pulse 2.2s infinite; }
  .dot.idle { background:#5b6478; box-shadow:none; animation:none; }
  @keyframes pulse { 50% { opacity:.45; } }
  .copybtn { border:1px solid var(--line); background:var(--card2); color:var(--txt); border-radius:9px;
             padding:8px 14px; font-size:13px; cursor:pointer; white-space:nowrap; }
  .copybtn:hover { border-color:var(--acc); }
  .qr { text-align:center; }
  .qr img { width:136px; height:136px; border-radius:12px; background:#fff; padding:7px;
            border:1px solid var(--line); }
  .qr div { font-size:12px; color:var(--muted); margin-top:8px; }
  .hint { color:var(--muted); font-size:13px; margin-top:14px; line-height:1.55; }
  #drop { border:2px dashed var(--line); border-radius:14px; padding:10px 12px; text-align:center;
          cursor:pointer; transition:border-color .15s, background .15s; }
  #drop:hover { border-color:var(--acc2); }
  #drop.over { border-color:var(--acc); background:rgba(56,189,248,.07); transform:scale(1.005); }
  #drop .dropicon { width:30px; height:30px; margin:0 auto 6px; display:block;
          filter:drop-shadow(0 3px 10px rgba(129,140,248,.45)); }
  #drop .big { font-size:15px; font-weight:650; margin-bottom:4px; }
  #drop .small { color:var(--muted); font-size:12px; }
  #drop .small b { color:var(--txt); word-break:break-all; }
  #snippet { width:100%; background:#090c15; border:1px solid var(--line); border-radius:12px;
             color:var(--txt); padding:12px 14px; font-size:14px; font-family:inherit; resize:vertical;
             min-height:110px; }
             resize:vertical; line-height:1.5; }
  #snippet:focus { outline:none; border-color:var(--acc); }
  #snippet::placeholder { color:var(--muted); }
  .snipcard { background:#090c15; border:1px solid var(--line); border-radius:14px;
              padding:14px 16px 14px 19px; margin-bottom:12px; position:relative; overflow:hidden; }
  .snipcard::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
              background:linear-gradient(180deg,var(--nac,var(--acc)),var(--nac2,var(--acc2))); }
  .snipcard:last-child { margin-bottom:0; }
  .sniphead { display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--muted); margin-bottom:9px; }
  .sniphead .who { color:var(--nac,var(--acc)); font-weight:700; }
  .sniptext { font-size:14.5px; line-height:1.55; white-space:pre-wrap; word-break:break-word;
              display:-webkit-box; -webkit-line-clamp:5; -webkit-box-orient:vertical; overflow:hidden;
              user-select:text; -webkit-user-select:text; cursor:text; }
  .snipcard.expanded .sniptext { display:block; }
  .sniptext a { color:var(--acc); }
  .snipmore { background:none; border:none; color:var(--acc); font-size:13px; cursor:pointer;
              padding:5px 0 0; font-family:inherit; }
  #ctxmenu { position:fixed; z-index:9999; min-width:150px; background:#12172a;
             border:1px solid var(--line); border-radius:10px; padding:5px;
             box-shadow:0 10px 30px rgba(0,0,0,.5); }
  #ctxmenu .ctxitem { display:block; width:100%; text-align:left; background:none; border:none;
             color:#eef1f8; font-size:13.5px; font-family:inherit; padding:9px 12px;
             border-radius:7px; cursor:pointer; }
  #ctxmenu .ctxitem:hover { background:#1d2547; }
  #ctxmenu .ctxitem.off { color:#5a6378; cursor:default; }
  #ctxmenu .ctxitem.off:hover { background:none; }
  .snipactions { display:flex; gap:8px; margin-top:11px; flex-wrap:nowrap; }
  .snipactions .btn { flex:1 1 0; min-width:0; padding:8px 4px; font-size:12.5px; white-space:nowrap; }
  .editbox { width:100%; box-sizing:border-box; background:#0b0e14; color:#eef1f8;
             border:1px solid var(--line); border-radius:9px; padding:10px;
             font-size:14px; font-family:inherit; resize:vertical; margin-bottom:8px; }
  #uploads { margin-top:14px; display:flex; flex-direction:column; gap:9px; }  .up { background:#090c15; border:1px solid var(--line); border-radius:11px; padding:11px 14px; font-size:14px; }
  .up .bar { height:6px; background:var(--line); border-radius:3px; margin-top:9px; overflow:hidden; }
  .up .bar > div { height:100%; width:0%; background:linear-gradient(90deg,var(--acc),var(--acc2)); transition:width .12s; }
  .up.done .bar > div { background:var(--good); }
  .up.err { color:var(--bad); }
  .frow { display:flex; align-items:center; gap:12px; padding:10px 8px; margin:0 -8px;
          border-top:1px solid var(--line); font-size:14px; border-radius:12px; transition:background .15s; }
  .frow:first-child { border-top:none; }
  .frow:hover { background:rgba(34,211,238,.05); }
  .thumb { width:54px; height:54px; object-fit:cover; border-radius:12px; flex:none;
          border:1px solid var(--line); background:#090c15; }
  .fic { font-size:20px; }
  .thwrap { position:relative; width:54px; height:54px; flex:none; }
  .thwrap .fic { display:flex; align-items:center; justify-content:center;
          width:54px; height:54px; border:1px solid var(--line); border-radius:12px; background:#090c15; }
  .tbadge { position:absolute; right:-6px; bottom:-6px; font-size:12px; line-height:1;
          background:#141a30; border:1px solid var(--line); border-radius:7px; padding:2px 3px; }
  .tbadge.tlabel { font-size:9px; font-weight:800; letter-spacing:.5px; padding:3px 5px; color:#7dd3fc; }
  .fmeta { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:3px; }
  .fname { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; font-size:13.5px; }
  .fsize { color:var(--muted); font-size:11.5px; white-space:nowrap; }
  .ftime { color:var(--muted); font-size:11px; white-space:nowrap; opacity:.8; }
  .btn { border:1px solid var(--line); background:#090c15; color:var(--txt); border-radius:10px;
         padding:8px 14px; font-size:13px; cursor:pointer; text-decoration:none; white-space:nowrap;
         transition:border-color .15s, filter .15s; }
  .btn:hover { border-color:var(--acc); }
  .frow .btn { padding:8px 10px; font-size:12px; }
  .btn.primary { background:linear-gradient(135deg,var(--acc),var(--acc2)); border:none;
         color:#0a0f1e; font-weight:700; }
  .btn.primary:hover { filter:brightness(1.12); }
  .btn.danger:hover { border-color:var(--bad); color:var(--bad); }
  .btn.dangergrad { background:linear-gradient(135deg,#f87171,#dc2626); border:none; color:#fff; font-weight:700;
         display:inline-flex; align-items:center; justify-content:center; gap:6px; }
  .btn.dangergrad:hover { filter:brightness(1.12); border:none; }
  .btn.dangergrad svg { width:15px; height:15px; flex:none; }
  .fstack { display:inline-flex; flex-direction:column; gap:6px; align-items:stretch; }
  .fstack .btn, .fstack .msavedbtn { width:100%; margin:0; }
  .empty { color:var(--muted); font-size:14px; padding:10px 4px; }
  .connectbar { display:flex; align-items:center; gap:8px; background:#090c15; border:1px solid var(--line);
         border-radius:999px; padding:6px 8px 6px 16px; }
  .connectbar a { color:var(--acc); font-weight:700; font-size:14px; text-decoration:none; word-break:break-all; }
  .connectwrap { display:flex; align-items:center; gap:10px; }
  .qrmini { background:#fff; border:1px solid var(--line); border-radius:10px; padding:4px;
            cursor:pointer; line-height:0; }
  .qrmini:empty { display:none; }
  .qrmini img { width:52px; height:52px; display:block; border-radius:6px; }
  .qrpop { display:none; margin:2px auto 14px; max-width:440px; text-align:center; }
  .qrpop.open { display:block; }
  .board { display:flex; flex-wrap:wrap; gap:14px; justify-content:center; align-items:stretch;
           flex:1 1 auto; min-height:0; width:100%; }
  .col { background:linear-gradient(180deg,var(--card2),var(--card)); border:1px solid var(--line);
          border-radius:18px; padding:18px; box-shadow:0 12px 32px rgba(0,0,0,.35);
          position:relative; overflow:hidden; width:340px; max-width:100%; flex:0 0 auto;
          height:calc(100vh - 210px); height:calc(100dvh - 210px); min-height:240px;
          display:flex; flex-direction:column; }
  .col::before { content:""; position:absolute; top:0; left:24px; right:24px; height:1px;
          background:linear-gradient(90deg,transparent,rgba(34,211,238,.55),rgba(167,139,250,.55),transparent); }
  .colhead { display:flex; align-items:center; gap:10px; margin-bottom:14px; flex-wrap:wrap; row-gap:8px; }
  .colhead h2 { font-size:12px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); }
  .count { font-size:11px; color:var(--muted); border:1px solid var(--line); background:#090c15;
           padding:2px 9px; border-radius:12px; }
  .count:empty { display:none; }
  .colbody { display:flex; flex-direction:column; gap:12px; flex:1; min-height:0; overflow-y:auto;
             padding-right:4px; scrollbar-width:thin; scrollbar-color:#2a3560 transparent; }
  .colbody::-webkit-scrollbar { width:8px; }
  .colbody::-webkit-scrollbar-thumb { background:#2a3560; border-radius:8px; }
  .colbody::-webkit-scrollbar-track { background:transparent; }
  .minicard { background:#090c15; border:1px solid var(--line); border-radius:14px; padding:16px; }
  .minicard h3 { font-size:13.5px; font-weight:650; margin-bottom:12px; display:flex; align-items:center; gap:8px; }
  .hicon { width:20px; height:20px; flex:none; filter:drop-shadow(0 2px 6px rgba(129,140,248,.4)); }
  footer { text-align:center; color:var(--muted); font-size:12px; margin-top:26px; }
  .mtabs { display:none; }
  .nbtnrow, .fbtnrow { display:flex; gap:8px; margin:0 0 12px; }
  .nbtnrow .btn { flex:1 1 0; min-width:0; margin:0; padding:8px 6px; font-size:12px;
    border-radius:12px; display:inline-flex; align-items:center; justify-content:center; gap:6px;
    overflow:hidden; text-overflow:ellipsis; }
  .fbtnrow { gap:6px; }
  .fbtnrow .btn { flex:1 1 0; min-width:0; margin:0; padding:10px 4px; font-size:13px;
    border-radius:12px; display:inline-flex; align-items:center; justify-content:center; gap:5px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .nbtnrow .btn svg, .fbtnrow .btn svg { width:15px; height:15px; flex:none; }
  .btn.openf { background:linear-gradient(135deg,#2dd4bf,#0ea5e9); border:none;
    color:#052e33; font-weight:700; }
  .btn.openf:hover { filter:brightness(1.12); border:none; }
  .btn.changef { background:linear-gradient(135deg,#A78BFA,#7C3AED); border:none;
    color:#fff; font-weight:700; }
  .btn.changef:hover { filter:brightness(1.12); border:none; }
  .msavedbtn { display:none; }
  .mqrmini { display:none; }
  .msend { display:none; }
  .mlabel { font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); font-weight:700; margin-bottom:10px; }
  .mbtn-primary { width:100%; margin-top:12px; padding:13px; border:none; border-radius:13px; font-size:15px;
           font-weight:800; font-family:inherit; color:#0a0f1e; cursor:pointer;
           background:linear-gradient(135deg,#818cf8,#22d3ee); }
  .mbtn-secondary { width:100%; padding:13px; border-radius:13px; font-size:15px; font-weight:700;
           font-family:inherit; color:var(--txt); background:#10182b; border:1px solid var(--line); cursor:pointer; }
  .mbtn-primary:active, .mbtn-secondary:active { filter:brightness(1.2); }
  #muploads { margin-top:10px; }
  .nact { display:flex; gap:8px; margin-top:11px; }
  .nact button { flex:1 1 0; display:flex; align-items:center; justify-content:center;
          padding:7px 4px; background:linear-gradient(135deg,var(--c1,#22d3ee),var(--c2,#0ea5e9));
          border:none; border-radius:10px; cursor:pointer; color:#0a0f1e; }
  .nact button svg { width:15px; height:15px; display:block; }
  .nact button:active { filter:brightness(1.15); }
  .mdlg { display:none; position:fixed; inset:0; z-index:110; background:rgba(4,6,12,.82);
          align-items:center; justify-content:center; padding:22px; }
  .mdlg.open { display:flex; }
  .mbox { width:100%; max-width:430px; background:var(--card2); border:1px solid var(--line);
          border-radius:16px; padding:18px; box-shadow:0 18px 50px rgba(0,0,0,.5); }
  .mbox h3 { font-size:16px; margin:0 0 12px; }
  .cfmsg { color:var(--muted); font-size:14px; line-height:1.55; margin:0 0 16px; word-break:break-word; }
  .mdlg .mrow { display:flex; gap:10px; }
  .mdlg .mrow .btn { flex:1; }
  .mbox textarea { width:100%; box-sizing:border-box; min-height:130px; background:#090c15;
          border:1px solid var(--line); border-radius:10px; color:var(--txt); padding:12px;
          font-size:14.5px; font-family:inherit; resize:vertical; }
  .mbox textarea:focus { outline:none; border-color:var(--acc); }
  .mbox .mrow { display:flex; gap:10px; margin-top:12px; }
  .mbox .mrow .btn { flex:1; padding:11px; font-size:14px; }
  .viewer { display:none; position:fixed; inset:0; z-index:100; background:rgba(4,6,12,.93);
            align-items:center; justify-content:center; padding:18px; }
  .viewer.open { display:flex; }
  .vbox { width:100%; max-width:640px; max-height:88vh; max-height:88dvh; overflow:auto;
          background:var(--card); border:1px solid var(--line); border-radius:16px; padding:14px; }
  .vhead { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:10px; }
  #vtitle { font-size:13px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .vclose { background:#1a2036; border:1px solid var(--line); color:var(--txt); border-radius:10px;
            width:34px; height:34px; font-size:15px; cursor:pointer; flex:0 0 auto; }
  #vcontent img { max-width:100%; border-radius:10px; display:block; }
  #vcontent video { width:100%; border-radius:10px; background:#000; }
  #vcontent audio { width:100%; }
  #vcontent iframe { width:100%; height:70vh; border:none; border-radius:8px; background:#fff; }
  #vcontent pre { white-space:pre-wrap; word-break:break-word; font-size:13px; max-height:60vh;
                 overflow:auto; background:#090c15; border:1px solid var(--line);
                 border-radius:10px; padding:12px; }
  .docxview { text-align:left; line-height:1.65; font-size:15px; }
  .docxview h1,.docxview h2,.docxview h3,.docxview h4 { margin:.7em 0 .35em; line-height:1.3; }
  .docxview p { margin:.45em 0; }
  .docxview table.dx { border-collapse:collapse; margin:.7em 0; max-width:100%; }
  .docxview table.dx td { border:1px solid var(--line); padding:6px 9px; }
  .vrow { display:flex; align-items:center; gap:10px; padding:10px 4px; border-top:1px solid var(--line); }
  .vrow .fname { flex:1; }
  @media (max-width:720px){
    body { padding:16px 12px 104px; display:block; height:auto; }
    .board { display:block; flex:none; }
    .board .col { display:none; width:100%; margin:0;
             height:calc(100vh - 330px); height:calc(100dvh - 330px); min-height:340px; }
    body[data-mtab="send"] .board .col,
    body[data-mtab="notes"] .board .col,
    body[data-mtab="files"] .board .col { height:calc(100dvh - 205px); min-height:380px; }
    footer { display:none; }
    .col[data-col="files"] #openfolder, .col[data-col="files"] #changefolder { display:none; }
    .board .col.active { display:flex; }
    .qrpop.open { max-width:100%; }
    .connectwrap { display:none; }
    .mqrmini { display:flex; align-items:center; flex:0 0 auto; }
    .mqrmini img { width:42px; height:42px; }
    .mqrrow { display:flex; gap:10px; align-items:center; }
    .mqrrow .maddrwrap { position:relative; display:flex; flex:1; min-width:0; width:0; }
    .mqrrow .maddrwrap .mtext { flex:1; min-width:0; width:0; padding-right:42px; }
    .mcopybtn { position:absolute; right:2px; top:50%; transform:translateY(-50%); background:none; border:none; font-size:17px; padding:8px 10px; cursor:pointer; }
    #mpcconnect { background:linear-gradient(135deg,#c084fc,#7c3aed); }
    .msend .minicard { padding:10px 10px 11px; }
    .msend .minicard + .minicard { margin-top:8px; }
    .msend .mlabel { margin-bottom:6px; }
    .msend .mtext { padding:9px 10px; }
    .msend .mhint { font-size:12px; margin:6px 2px 0; }
    .msend .mbtn-primary, .msend .mbtn-secondary { padding:10px; margin-top:8px; }
    .msendfoot { text-align:center; color:var(--muted); font-size:11px; margin:8px 4px 0; white-space:nowrap; }
    .mtext { width:100%; box-sizing:border-box; background:#090c15; border:1px solid var(--line);
             border-radius:10px; color:var(--txt); padding:12px; font-size:16px; font-family:inherit; }
    .mtext:focus { outline:none; border-color:var(--acc); }
    .mhint { color:var(--muted); font-size:12.5px; margin:10px 2px 0; line-height:1.5; }
    .msavedbtn { display:inline-flex; align-items:center; justify-content:center; gap:6px; border-radius:10px; padding:8px 14px;
             font-size:13px; font-weight:800; font-family:inherit; cursor:pointer; color:#0a0f1e;
             background:linear-gradient(135deg,#818cf8,#22d3ee); border:1px solid rgba(129,140,248,.9);
             box-shadow:0 0 14px rgba(56,189,248,.28); }
    .msavedbtn:active { filter:brightness(1.15); }
    .mtabs { display:flex; position:fixed; left:0; right:0; bottom:0; z-index:60; gap:6px;
             background:rgba(9,12,21,.97); border-top:1px solid var(--line);
             padding:8px 10px calc(8px + env(safe-area-inset-bottom)); }
    .mtab { flex:1; display:flex; flex-direction:column; align-items:center; gap:3px; position:relative;
            background:none; border:none; color:var(--muted); font-size:11px; font-family:inherit;
            padding:8px 4px; border-radius:12px; cursor:pointer; }
    .mtab .mi { font-size:20px; }
    .mtab.sel { color:var(--txt); background:rgba(34,211,238,.1); }
    .mcount { position:absolute; top:2px; right:calc(50% - 28px); font-size:10px; background:var(--acc);
              color:#0a0f1e; font-weight:700; border-radius:10px; padding:1px 6px; }
    .mcount:empty { display:none; }
    .nbtnrow { margin:0 0 10px; }
    .nbtnrow .btn { padding:12px 8px; font-size:14px; border-radius:12px; }
    .col[data-col="send"] .colbody > .minicard { display:none; }
    .col[data-col="send"] .msend { display:block; }
    #msendtext { width:100%; box-sizing:border-box; min-height:52px; background:#090c15;
             border:1px solid var(--line); border-radius:12px; color:var(--txt); padding:12px;
             font-size:16px; font-family:inherit; resize:vertical; }
    #msendtext:focus { outline:none; border-color:var(--acc); }
    #msendtext::placeholder { color:var(--muted); }
    .nact { margin-top:10px; }
    .nact button { padding:8px 4px; }
    .nact button svg { width:18px; height:18px; }
    .toast { bottom:calc(112px + env(safe-area-inset-bottom)); }
  }
  .toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%) translateY(20px); background:var(--card2);
           border:1px solid var(--acc); color:var(--txt); padding:11px 20px; border-radius:12px; font-size:14px;
           opacity:0; transition:.25s; pointer-events:none; z-index:120; max-width:92vw; text-align:center; }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <img class="logoimg" src="/icon.png?v=__VERSION__" alt="DropIt"
         onerror="this.remove();document.getElementById('logofallback').style.display='flex';">
    <div class="logo" id="logofallback" style="display:none">&#x1F4E5;</div>
    <div><h1>DropIt</h1></div>
    <span class="ver" title="build 2026-10-02 23:40">v__VERSION__</span>
  </div>
  <div class="connectwrap">
    <div class="connectbar">
      <a href="__URL__">__URL__</a>
      <button class="copybtn" id="copy">Copy</button>
    </div>
    <button class="qrmini" id="qrtoggle" title="Show large QR code">__QRMINI__</button>
  </div>
  <div class="status" id="statusdiv"><span class="dot" id="statusdot"></span><span id="statustext">No Devices Connected</span></div>
</div>
<div class="qrpop" id="qrpop">__QR__
  <p class="hint">Open the address on your phone &mdash; both devices need the same WiFi.<br>First run may ask for firewall permission &mdash; allow it.</p>
</div>

<div class="board">
  <div class="col" data-col="send">
    <div class="colhead"><h2>&#x2B06;&#xFE0F; Send</h2></div>
    <div class="colbody">
      <div class="minicard">
        <h3><svg class="hicon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <defs><linearGradient id="hg1" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
            <stop stop-color="#a78bfa"/><stop offset="1" stop-color="#38bdf8"/>
          </linearGradient></defs>
          <path d="M22 2 11 13" stroke="url(#hg1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M22 2 15 22l-4-9-9-4 20-7z" stroke="url(#hg1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>Send Files</h3>
        <div id="drop">
          <svg class="dropicon" viewBox="0 0 64 64" fill="none" aria-hidden="true">
            <defs><linearGradient id="dg" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
              <stop stop-color="#a78bfa"/><stop offset="1" stop-color="#38bdf8"/>
            </linearGradient></defs>
            <path d="M32 4C32 4 12 28 12 42a20 20 0 0 0 40 0C52 28 32 4 32 4Z" fill="url(#dg)"/>
            <path d="M22 42a10 10 0 0 0 10 10" stroke="#fff" stroke-opacity=".7" stroke-width="3.5"
                  stroke-linecap="round"/>
            <path d="M24 20c2-3 5-7 8-10 3 3 6 7 8 10" stroke="#fff" stroke-opacity=".9" stroke-width="3"
                  stroke-linecap="round"/>
          </svg>
          <div class="big">Drop Your Files Here</div>
          <div class="small">or click to browse &mdash; they land in <b id="dirnamelabel">__DIRNAME__</b></div>
          <input type="file" id="picker" multiple style="display:none">
        </div>
        <div id="uploads"></div>
      </div>
      <div class="minicard">
        <h3><svg class="hicon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <defs><linearGradient id="hg2" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
            <stop stop-color="#a78bfa"/><stop offset="1" stop-color="#38bdf8"/>
          </linearGradient></defs>
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"
                stroke="url(#hg2)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>Send Text</h3>
        <textarea id="snippet" rows="3" placeholder="Paste a link, email, phone number, or any text&#8230;"></textarea>
        <div style="display:flex;justify-content:flex-end;margin-top:10px;">
          <button class="btn primary" id="sendsnippet">Send Text</button>
        </div>
      </div>
      <div class="msend">
        <div class="minicard">
          <div class="mlabel">Your Link</div>
          <div class="mqrrow">
            <div class="maddrwrap">
              <input id="mpchost" class="mtext" spellcheck="false" autocomplete="off" autocapitalize="off">
              <button class="mcopybtn" id="mcopy" title="Copy link">&#x1F4CB;</button>
            </div>
            <button class="qrmini mqrmini" id="qrtoggleM" title="Show large QR code">__QRMINI__</button>
          </div>
          <p class="mhint">Shown on the DropIt window on your PC &mdash; enter once, it&apos;s remembered.</p>
          <button class="mbtn-primary" id="mpcconnect">&#x1F4BE;&nbsp; Save &amp; connect</button>
        </div>
        <div class="minicard">
          <div class="mlabel">Send Files</div>
          <button class="mbtn-secondary" id="msendfiles">&#x1F4C1;&nbsp; Choose files to send</button>
          <input type="file" id="mpicker" multiple style="display:none">
          <div id="muploads"></div>
        </div>
        <div class="minicard">
          <div class="mlabel">Share Text</div>
          <textarea id="msendtext" rows="3" placeholder="Paste a link, email, phone number&#8230;"></textarea>
          <button class="mbtn-primary" id="msendsnippet">&#x2708;&#xFE0F;&nbsp; Send Text</button>
        </div>
        <p class="msendfoot">Local only &middot; No cloud &middot; No accounts &middot; Just DropIt</p>
      </div>
    </div>
  </div>
  <div class="col" data-col="notes">
    <div class="colhead"><h2>&#x1F4AC; Shared Texts</h2><span class="count" id="snipcount"></span></div>
    <div class="nbtnrow"><button class="btn primary" id="maddnote">+&nbsp; Add New Text</button><button class="btn dangergrad" id="delallnotesbtn" style="display:none"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg> Delete All</button></div>
    <div class="colbody"><div id="snips"><p class="empty">Loading&hellip;</p></div></div>
  </div>
  <div class="col" data-col="files">
    <div class="colhead"><h2>&#x1F4C1; Shared Files</h2><span class="count" id="filecount"></span>__FILEHEAD__</div>
    __FILEBTNS__
    <div class="colbody"><div id="files"><p class="empty">Loading&hellip;</p></div></div>
  </div>
</div>

<nav class="mtabs">
  <button class="mtab sel" data-tab="send"><span class="mi">&#x2B06;&#xFE0F;</span><span>Send</span></button>
  <button class="mtab" data-tab="notes"><span class="mi">&#x1F4AC;</span><span>Notes</span><span class="mcount" id="mnotecount"></span></button>
  <button class="mtab" data-tab="files"><span class="mi">&#x1F4C1;</span><span>Files</span><span class="mcount" id="mfilecount"></span></button>
</nav>

<footer>DropIt &middot; Local only &middot; No cloud &middot; No accounts &middot; Just DropIt</footer>
<div class="toast" id="toast"></div>
<div class="viewer" id="viewer"><div class="vbox">
  <div class="vhead"><span id="vtitle"></span><button class="vclose" id="vclose">&#x2715;</button></div>
  <div id="vcontent"></div>
</div></div>
<div class="mdlg" id="cfdlg"><div class="mbox">
  <h3 id="cftitle">Save file?</h3>
  <p class="cfmsg" id="cfmsg"></p>
  <div class="mrow"><button class="btn" id="cfno">Discard</button><button class="btn primary" id="cfyes">Save on phone</button></div>
</div></div>

<div class="mdlg" id="mdlg"><div class="mbox">
  <h3>New Shared Text</h3>
  <textarea id="mnote" placeholder="Write your shared text&hellip;"></textarea>
  <div class="mrow"><button class="btn" id="mcancel">Cancel</button><button class="btn primary" id="msave">Save</button></div>
</div></div>

<script>
const $ = s => document.querySelector(s);
const drop = $("#drop"), picker = $("#picker");
const esc = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const SVG = {
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 114 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
  dl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>',
  del: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>',
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>'
};

function fmt(n){ if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(1)+" KB";
  if(n<1073741824) return (n/1048576).toFixed(1)+" MB"; return (n/1073741824).toFixed(2)+" GB"; }
function iconFor(n){ const e=n.split(".").pop().toLowerCase();
  if(["png","jpg","jpeg","gif","webp","svg","heic","bmp"].includes(e)) return "🖼️";
  if(["mp4","mov","mkv","webm","avi","m4v"].includes(e)) return "🎬";
  if(["mp3","wav","flac","m4a","ogg","aac"].includes(e)) return "🎵";
  if(["zip","rar","7z","tar","gz"].includes(e)) return "📦";
  if(["pdf"].includes(e)) return "📕";
  if(["doc","docx","odt","rtf"].includes(e)) return "📘";
  if(["xls","xlsx","ods","csv"].includes(e)) return "📗";
  if(["ppt","pptx","odp"].includes(e)) return "📙";
  if(["html","htm","xml"].includes(e)) return "🌐";
  if(["py","js","ts","css","java","c","cpp","json"].includes(e)) return "💻";
  if(["txt","md","log"].includes(e)) return "📝";
  return "📄"; }
function badgeFor(n){
  if(/\\.(png|jpe?g|gif|webp|bmp|heic|svg)$/i.test(n)) return '<span class="tbadge">🖼️</span>';
  if(/\\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(n)) return '<span class="tbadge">🎬</span>';
  if(/\\.(mp3|wav|flac|m4a|ogg|aac)$/i.test(n)) return '<span class="tbadge">🎵</span>';
  const p = n.split(".");
  const e = (p.length > 1 ? p.pop() : "file").toUpperCase().slice(0, 4);
  return '<span class="tbadge tlabel">' + esc(e) + '</span>';
}
function thumbFor(n){
  if(/\\.(png|jpe?g|gif|webp|bmp|heic|svg)$/i.test(n))
    return `<img class="thumb" loading="lazy" src="/thumb/${encodeURIComponent(n)}" alt="">`;
  if(/\\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(n))
    return `<video class="thumb" preload="metadata" muted playsinline disablepictureinpicture src="/dl/${encodeURIComponent(n)}#t=0.5"></video>`;
  return `<span class="fic">${iconFor(n)}</span>`;
}
async function openFile(name){
  try {
    if(window.pywebview && window.pywebview.api && window.pywebview.api.open_file){
      const ok = await window.pywebview.api.open_file(name);
      if(!ok) toast("Couldn't open that file");
      return;
    }
  } catch(e){}
  location.href = "/dl/" + encodeURIComponent(name);
}
function toast(msg){ const t=$("#toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),2200); }

// ---------- phone download cache: Download once, Open after ----------
function hasBridge(){ try{ return !!(window.pywebview && window.pywebview.api && window.pywebview.api.open_file); }catch(e){ return false; } }
const MEMBLOBS = {};
const DLSTATE = {};
try { Object.assign(DLSTATE, JSON.parse(localStorage.getItem("dropit_dl") || "{}")); } catch(e){}
function isDownloaded(f){ const v = DLSTATE[f.name]; return v === f.size || (!!v && v.size === f.size); }
function markDownloaded(f){ DLSTATE[f.name] = {size: f.size, ts: Math.floor(Date.now()/1000)};
  try{ localStorage.setItem("dropit_dl", JSON.stringify(DLSTATE)); }catch(e){} }
function dlLabel(f){ return (hasBridge() || isDownloaded(f)) ? "Open" : "Download"; }

const DBCACHE = {
  db:null,
  open(){ return new Promise((res, rej) => {
    if(this.db) return res(this.db);
    try{
      const rq = indexedDB.open("dropit-cache", 1);
      rq.onupgradeneeded = () => rq.result.createObjectStore("files");
      rq.onsuccess = () => { this.db = rq.result; res(this.db); };
      rq.onerror = () => rej(rq.error);
    }catch(e){ rej(e); }
  });},
  async get(name){ try{ const db = await this.open();
      return await new Promise((res, rej) => { const t = db.transaction("files").objectStore("files").get(name);
        t.onsuccess = () => res(t.result || null); t.onerror = () => rej(t.error); });
    }catch(e){ return null; } },
  async put(name, blob){ try{ const db = await this.open();
      await new Promise((res, rej) => { const t = db.transaction("files","readwrite").objectStore("files").put(blob, name);
        t.onsuccess = () => res(); t.onerror = () => rej(t.error); });
    }catch(e){} },
  async del(name){ try{ const db = await this.open();
      await new Promise((res, rej) => { const t = db.transaction("files","readwrite").objectStore("files").delete(name);
        t.onsuccess = () => res(); t.onerror = () => rej(t.error); });
    }catch(e){} },
  async clear(){ try{ const db = await this.open();
      await new Promise((res, rej) => { const t = db.transaction("files","readwrite").objectStore("files").clear();
        t.onsuccess = () => res(); t.onerror = () => rej(t.error); });
    }catch(e){} }
};

async function ensureSpace(){
  try{
    const est = await navigator.storage.estimate();
    if(est.usage > 400*1024*1024){
      await DBCACHE.clear();
      for(const k in MEMBLOBS) delete MEMBLOBS[k];
      for(const k in DLSTATE) delete DLSTATE[k];
      try{ localStorage.setItem("dropit_dl", "{}"); }catch(e){}
    }
  }catch(e){}
}

let VURL = null;

// ---------- overlay back-button handling ----------
// The viewer and the download confirm dialog each push a history entry, so the
// phone's back button / swipe-back closes the top overlay instead of the browser.
// A user back-press already consumes that entry, so only a self-close (X / button)
// issues history.back() to remove it -- never both.
const ovStack = [];
let ovSkipPop = false;
function ovOpened(onClose){
  ovSkipPop = false;   // a stale skip must never swallow a future real back-press
  const top = ovStack[ovStack.length - 1];
  if(top && top._isViewer && onClose._isViewer){
    ovStack.pop();     // viewer replacing viewer keeps the single live entry
  } else if(window.history){
    try{ history.pushState({dov:1}, ""); }catch(e){}
  }
  ovStack.push(onClose);
}
function ovClosed(onClose, selfClose){
  const i = ovStack.lastIndexOf(onClose);
  if(i >= 0) ovStack.splice(i, 1);
  if(selfClose){
    ovSkipPop = true;
    try{ history.back(); }catch(e){ ovSkipPop = false; }
  }
}
window.addEventListener("popstate", () => {
  if(ovSkipPop){ ovSkipPop = false; return; }
  const top = ovStack.pop();
  if(top){ try{ top(false); }catch(e){} }
});

function _viewerHide(){
  $("#viewer").classList.remove("open");
  $("#vcontent").innerHTML = "";
  if(VURL){ URL.revokeObjectURL(VURL); VURL = null; }
}
function showViewer(title, html, url){
  _viewerHide();
  $("#vtitle").textContent = title;
  $("#vcontent").innerHTML = html;
  $("#viewer").classList.add("open");
  VURL = url || null;
  const closer = (self) => { _viewerHide(); ovClosed(closer, self !== false); };
  closer._isViewer = true;
  ovOpened(closer);
}
function closeViewer(byUser){
  const wasOpen = $("#viewer").classList.contains("open");
  _viewerHide();
  if(!wasOpen) return;
  let own = null;
  for(let i = ovStack.length - 1; i >= 0; i--){
    if(ovStack[i]._isViewer){ own = ovStack[i]; break; }
  }
  ovClosed(own, byUser !== false);
}
$("#vclose").onclick = () => closeViewer();
$("#viewer").onclick = e => { if(e.target.id === "viewer") closeViewer(); };

// ---------- download confirm dialog ----------
function askSaveDiscard(title, msg){
  return new Promise(res => {
    const d = $("#cfdlg");
    $("#cftitle").textContent = title;
    $("#cfmsg").textContent = msg;
    let settled = false;
    const dcloser = (self) => done(false, self);
    const done = (v, self) => {
      if(settled) return; settled = true;
      d.classList.remove("open");
      ovClosed(dcloser, self !== false);
      res(v);
    };
    $("#cfyes").onclick = () => done(true);
    $("#cfno").onclick = () => done(false);
    d.classList.add("open");
    ovOpened(dcloser);
  });
}

async function downloadFile(f, btn){
  if(btn){ if(btn.dataset.busy) return; btn.dataset.busy = "1"; btn.textContent = "…"; }
  const resetBtn = () => { if(btn){ delete btn.dataset.busy; btn.textContent = "Download"; } };
  try{
    toast("Downloading " + f.name);
    const r = await fetch("/dl/" + encodeURIComponent(f.name));
    if(!r.ok) throw 0;
    const blob = await r.blob();
    const keep = await askSaveDiscard("Download complete",
      "Save " + f.name + " (" + fmt(f.size) + ") on this phone?");
    if(!keep){ toast("File not saved on this phone."); resetBtn(); return; }
    await ensureSpace();
    MEMBLOBS[f.name] = blob;
    DBCACHE.put(f.name, blob);
    markDownloaded(f);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = f.name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    lastFilesSig = null;
    await loadFiles();
    document.querySelectorAll('#files [data-name]').forEach(b => {
      if(b.dataset.name === f.name) b.textContent = "Open";
    });
    toast("Saved in DropIt — tap Open to view it");
  }catch(e){ toast("Download failed"); resetBtn(); }
  if(btn){ delete btn.dataset.busy; }
}

async function openBlob(f){
  let blob = MEMBLOBS[f.name] || await DBCACHE.get(f.name);
  if(!blob){ downloadFile(f); return; }
  MEMBLOBS[f.name] = blob;
  const n = f.name;
  if(/\\.(txt|md|log|json|csv|xml|html|css|js)$/i.test(n)){
    showViewer(n, `<pre>${esc((await blob.text()).slice(0, 30000))}</pre>`);
    return;
  }
  if(/\\.docx$/i.test(n)){
    showViewer(n, '<div style="text-align:center;opacity:.6;padding:24px">Opening document\\u2026</div>');
    try{
      const r = await fetch("/api/docx/?name=" + encodeURIComponent(n));
      if(r.status === 404) throw "gone";
      if(!r.ok) throw "preview";
      showViewer(n, '<div class="docxview">' + (await r.text()) + "</div>");
    }catch(e){
      const why = e === "gone"
        ? "This file is no longer in the shared folder on your PC."
        : "No preview available for this Word file.";
      showViewer(n, '<div style="text-align:center;padding:24px"><p style="opacity:.65;margin:0 0 16px">' + why + '</p>' +
        '<button class="btn primary" id="vdocxopen">Open in Word app</button></div>');
      const vb = document.getElementById("vdocxopen");
      if(vb) vb.onclick = () => { closeViewer(); openWithApp(f); };
    }
    return;
  }
  const url = URL.createObjectURL(blob);
  if(/\\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(n))
    showViewer(n, `<img src="${url}" alt="">`, url);
  else if(/\\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(n))
    showViewer(n, `<video src="${url}" controls autoplay playsinline style="width:100%">`, url);
  else if(/\\.(mp3|wav|flac|m4a|ogg|aac)$/i.test(n))
    showViewer(n, `<audio src="${url}" controls autoplay style="width:100%">`, url);
  else if(/\\.pdf$/i.test(n)){
    window.open(url, "_blank");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  else openWithApp(n, blob, url);
}

async function openWithApp(n, blob, url){
  const file = new File([blob], n, { type: blob.type || "application/octet-stream" });
  if(navigator.share){
    try { await navigator.share({ files:[file] }); URL.revokeObjectURL(url); return; }
    catch(e){}
  }
  URL.revokeObjectURL(url);
  toast("Saved in Downloads \\u2014 open it from your Files app");
}

function fileTap(f, btn){
  if(hasBridge()){ openFile(f.name); return; }
  if(isDownloaded(f)) openBlob(f);
  else downloadFile(f, btn);
}

async function clearPhoneCache(){
  for(const k in MEMBLOBS) delete MEMBLOBS[k];
  for(const k in DLSTATE) delete DLSTATE[k];
  try{ localStorage.setItem("dropit_dl", "{}"); }catch(e){}
  await DBCACHE.clear();
}

function fileRowHtml(f){
  const ft = fileTime(f.ts || f.mtime);
  return `<div class="frow"><span class="thwrap">${thumbFor(f.name)}${badgeFor(f.name)}</span>` +
    `<span class="fmeta"><span class="fname" title="${esc(f.name)}">${esc(f.name)}</span>` +
    `<span class="fsize">${fmt(f.size)}</span>` +
    (ft ? `<span class="ftime">${ft}</span>` : "") + `</span>`;
}
function showSaved(){
  const sfiles = Object.keys(DLSTATE).map(n => {
    const v = DLSTATE[n];
    return (v && typeof v === "object") ? {name:n, size:v.size, ts:v.ts} : {name:n, size:v, ts:0};
  });
  let html;
  if(!sfiles.length) html = '<p class="empty">Nothing saved on this phone yet \\u2014 tap Download on any file.</p>';
  else html = `<button class="btn danger" id="vclear" style="margin-bottom:12px;width:100%;display:flex;align-items:center;justify-content:center;gap:8px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>Clear saved files</button>` +
    sfiles.map((f,i) =>
      fileRowHtml(f) +
      `<button class="btn primary" data-sopen="${i}">Open</button>` +
      `<button class="btn danger" data-sdel="${i}">Remove</button></div>`
    ).join("");
  showViewer("Saved on this phone", html);
  document.querySelectorAll("[data-sopen]").forEach(b =>
    b.onclick = () => openBlob(sfiles[+b.dataset.sopen]));
  document.querySelectorAll("[data-sdel]").forEach(b => b.onclick = async () => {
    const f = sfiles[+b.dataset.sdel];
    delete DLSTATE[f.name];
    try{ localStorage.setItem("dropit_dl", JSON.stringify(DLSTATE)); }catch(e){}
    try{ await DBCACHE.del(f.name); }catch(e){}
    delete MEMBLOBS[f.name];
    showSaved(); lastFilesSig = null; loadFiles(); toast("Removed from this phone");
  });
  const vc = $("#vclear");
  if(vc) vc.onclick = async () => {
    if(!confirm("This will delete all files saved on this device. Continue?")) return;
    await clearPhoneCache(); closeViewer();
    lastFilesSig = null; loadFiles(); toast("Saved files cleared");
  };
}

$("#copy").onclick = async () => {
  const url = "__URL__";
  try { await navigator.clipboard.writeText(url); }
  catch(e){ const i=document.createElement("input"); i.value=url; document.body.appendChild(i);
    i.select(); document.execCommand("copy"); i.remove(); }
  toast("Link copied \\u2014 send it to your phone");
};

const qp = $("#qrpop");
document.querySelectorAll(".qrmini").forEach(b => { b.onclick = () => { if(qp) qp.classList.toggle("open"); }; });

const mpch = $("#mpchost");
if(mpch && !mpch.value) mpch.value = location.host;
function maddrNorm(){
  let v = (mpch.value || "").trim();
  const low = v.toLowerCase();
  if(low.indexOf("http://") === 0) v = v.slice(7);
  else if(low.indexOf("https://") === 0) v = v.slice(8);
  return v.split("/")[0].trim();
}
const mpcc = $("#mpcconnect");
if(mpcc) mpcc.onclick = () => {
  const v = maddrNorm();
  if(!v){ toast("Enter your PC's address"); return; }
  try{ localStorage.setItem("dropit_pchost", v); }catch(e){}
  if(v === location.host){ toast("Already connected to this PC"); return; }
  location.href = "http://" + v + "/";
};
const mcp = $("#mcopy");
if(mcp) mcp.onclick = async () => {
  const v = maddrNorm() || location.host;
  toast(await copyText("http://" + v + "/") ? "Link copied" : "Couldn't copy link");
};
const msb = $("#msavedbtn");
if(msb) msb.onclick = () => showSaved();

const of = $("#openfolder");
if(of) of.onclick = () => {
  if(hasBridge()){ try { window.pywebview.api.open_folder(); } catch(e){} return; }
  showSaved();   // phone web: the "folder" is what DropIt saved on this phone
};

const cf = $("#changefolder");
if(cf){
  // Desktop: native folder browser (Explorer/Finder) - clicks just work.
  cf.onclick = async () => {
    if(!hasBridge()) return;
    cf.disabled = true;
    try {
      const r = await window.pywebview.api.browse_directory();
      if(r && r.ok){
        const dl = $("#dirnamelabel");
        if(dl) dl.textContent = r.directory || r.dirname;
        toast("Save folder changed");
        loadFiles();
      } else if(r && r.error && r.error !== "cancelled"){
        toast("Couldn't change the folder: " + r.error);
      }
    } catch(e){ toast("Couldn't open the folder picker"); }
    cf.disabled = false;
  };
}

drop.onclick = () => picker.click();
["dragover","dragenter"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", ev => uploadFiles(ev.dataTransfer.files));
picker.addEventListener("change", () => { uploadFiles(picker.files); picker.value=""; });

function uploadFiles(files, boxSel){
  const box = $(boxSel || "#uploads");
  [...files].forEach(f => {
    const row = document.createElement("div");
    row.className = "up";
    row.innerHTML = `<div>${esc(f.name)} <span style="color:var(--muted)">(${fmt(f.size)})</span></div><div class="bar"><div></div></div>`;
    box.prepend(row);
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

async function sendSnippet(ta){
  const text = ta.value.trim();
  if(!text){ toast("Type something first"); return; }
  try {
    const r = await fetch("/api/snippet", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({text})});
    const j = await r.json();
    if(j.ok){ ta.value=""; toast("\u2705 Text shared"); loadNotes(); }
    else toast("Couldn't share that text");
  } catch(e){ toast("Couldn't share that text"); }
}
$("#sendsnippet").onclick = () => sendSnippet($("#snippet"));
$("#msendsnippet").onclick = () => sendSnippet($("#msendtext"));
$("#msendfiles").onclick = () => $("#mpicker").click();
$("#mpicker").addEventListener("change", () => { uploadFiles($("#mpicker").files, "#muploads"); $("#mpicker").value=""; });

const NOTES = {};
const EXPANDED = new Set();  // note ids the user expanded - survives the 5s re-render
function noteTime(ts){
  return new Date(ts*1000).toLocaleString([], {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
}
function fileTime(ts){ return ts ? noteTime(ts) : ""; }
// ---------- note download state: Download once, Open after ----------
const NOTEDL = {};
try { Object.assign(NOTEDL, JSON.parse(localStorage.getItem("dropit_notedl") || "{}")); } catch(e){}
function persistNotedl(){ try{ localStorage.setItem("dropit_notedl", JSON.stringify(NOTEDL)); }catch(e){} }
function paintNoteDlBtn(btn, id){
  const opened = !!NOTEDL[id];
  btn.innerHTML = opened ? SVG.open : SVG.dl;
  btn.title = opened ? "Open" : "Download";
}
function noteDlTap(btn){
  const id = btn.dataset.dl;
  if(NOTEDL[id]) openNote(id); else dlNote(id, btn);
}
async function copyText(t){
  try { await navigator.clipboard.writeText(t); return true; }
  catch(e){
    try {
      const ta = document.createElement("textarea");
      ta.value = t; ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove();
      return !!ok;
    } catch(e2){ return false; }
  }
}
async function copyNote(id){
  const t = NOTES[id] || "";
  if(!t){ toast("Nothing to copy"); return; }
  toast(await copyText(t) ? "Text copied" : "Couldn't copy text");
}
async function shareNote(id){
  const t = NOTES[id] || "";
  if(!t){ toast("Nothing to share"); return; }
  if(navigator.share){
    try { await navigator.share({text:t}); return; }
    catch(e){ if(e && e.name === "AbortError") return; }
  }
  toast(await copyText(t) ? "Share sheet unavailable \u2014 text copied" : "Couldn't copy text");
}
async function delNote(id){
  if(!confirm("Delete this text note?")) return;
  await fetch("/api/notes/delete", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id})});
  loadNotes(); toast("Shared Text Deleted");
}
async function dlNote(id, btn){
  const t = NOTES[id] || "";
  if(!t){ toast("Nothing to download"); return; }
  try {
    if(window.pywebview && window.pywebview.api && window.pywebview.api.save_note){
      const r = await window.pywebview.api.save_note({id, text: t});
      if(r && r.ok){ NOTEDL[id] = r.path || true; persistNotedl(); if(btn) paintNoteDlBtn(btn, id); toast("Note saved"); }
      else toast("Download cancelled");
      return;
    }
  } catch(e){}
  location.href = "/api/notes/dl?id=" + encodeURIComponent(id);
  NOTEDL[id] = true; persistNotedl(); if(btn) paintNoteDlBtn(btn, id);
}
async function openNote(id){
  const t = NOTES[id] || "";
  const saved = NOTEDL[id];
  try {
    if(typeof saved === "string" && window.pywebview && window.pywebview.api && window.pywebview.api.open_path){
      if(await window.pywebview.api.open_path(saved)) return;
    }
  } catch(e){}
  if(!t){ toast("Nothing to open"); return; }
  showViewer("Shared Text", "<pre>" + esc(t) + "</pre>");
}
function editNote(id, card){
  const box = card.querySelector(".sniptext");
  const cur = NOTES[id] || "";
  box.innerHTML = "";
  const ta = document.createElement("textarea");
  ta.value = cur; ta.rows = 5; ta.className = "editbox";
  const row = document.createElement("div"); row.className = "snipactions";
  const save = document.createElement("button"); save.className = "btn"; save.textContent = "Save";
  const cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "Cancel";
  row.appendChild(save); row.appendChild(cancel);
  box.appendChild(ta); box.appendChild(row);
  ta.focus();
  const closeEditor = () => { ta.remove(); row.remove(); };
  save.onclick = async () => {
    const text = ta.value.trim();
    if(!text){ toast("Shared Text Is Empty \u2014 Not Saved"); return; }
    await fetch("/api/notes/edit", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id, text})});
    closeEditor(); lastNotesSig = null; loadNotes(); toast("Shared Text Updated");
  };
  cancel.onclick = () => { closeEditor(); lastNotesSig = null; loadNotes(); };
}
function linkify(s){
  return s.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

let lastFilesSig = null;
async function loadFiles(){
  const box = $("#files");
  try {
    const r = await fetch("/api/files"); const files = await r.json();
    const sig = files.map(f => f.name + "|" + f.size).join(";");
    if(sig === lastFilesSig) return;   // unchanged: skip re-render so video thumbs never flicker
    lastFilesSig = sig;
    $("#filecount").textContent = files.length || "";
    $("#mfilecount").textContent = files.length || "";
    const dab = $("#delallbtn"); if(dab) dab.style.display = files.length ? "" : "none";
    if(!files.length){ box.innerHTML = '<p class="empty">Nothing here yet &mdash; beam something over from your phone.</p>'; return; }
    box.innerHTML = files.map((f,i) =>
      fileRowHtml(f) +
      `<button class="btn primary" data-dl="${i}" data-name="${esc(f.name)}">${dlLabel(f)}</button>` +
      `<button class="btn danger" data-del="${i}">Delete</button></div>`
    ).join("");
    box.querySelectorAll("[data-del]").forEach(b =>
      b.onclick = () => delFile(files[+b.dataset.del].name));
    box.querySelectorAll("[data-dl]").forEach(b =>
      b.onclick = () => fileTap(files[+b.dataset.dl], b));
  } catch(e){ box.innerHTML = '<p class="empty">Could not load file list.</p>'; }
}

const ACCENTS = [
  ["#22d3ee","#818cf8"], ["#a78bfa","#f472b6"], ["#f472b6","#fb7185"], ["#fbbf24","#fb923c"],
  ["#34d399","#22d3ee"], ["#60a5fa","#a78bfa"], ["#fb7185","#f472b6"], ["#a3e635","#34d399"]
];
function accentFor(id){ let h = 0; const s = String(id);
  for(let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return ACCENTS[h % ACCENTS.length]; }

function strHash(s){ let h = 0; s = String(s);
  for(let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h; }
let lastNotesSig = null;
async function loadNotes(){
  if(document.querySelector(".editbox")) return;  // don't clobber an open editor
  const snbox = $("#snips");
  try {
    const r = await fetch("/api/notes"); const notes = await r.json();
    const sig = notes.map(n => n.id + "|" + (n.created || 0) + "|" + strHash(n.text || "")).join(";");
    if(sig === lastNotesSig) return;  // unchanged: skip re-render so text selection never drops
    lastNotesSig = sig;
    $("#snipcount").textContent = notes.length || "";
    $("#mnotecount").textContent = notes.length || "";
    const danb = $("#delallnotesbtn"); if(danb) danb.style.display = notes.length ? "" : "none";
    for(const k in NOTES) delete NOTES[k];
    if(!notes.length){ snbox.innerHTML = '<p class="empty">Nothing shared yet &mdash; send text from the Send column or your phone.</p>'; return; }
    snbox.innerHTML = notes.map(n => {
      NOTES[n.id] = n.text || "";
      const t = n.text || "(empty)";
      const long = t.split("\\n").length > 5 || t.length > 280;
      const ac = accentFor(n.id);
      const isEx = EXPANDED.has(n.id);
      return `<div class="snipcard${isEx ? " expanded" : ""}" data-nid="${esc(n.id)}" style="--nac:${ac[0]};--nac2:${ac[1]}">` +
      `<div class="sniphead"><span>&#x1F4AC;</span><span class="who">Shared Text</span>` +
      `<span>&middot;</span><span>${esc(noteTime(n.created))}</span></div>` +
      `<div class="sniptext">${linkify(esc(t))}</div>` +
      (long ? `<button class="snipmore">${isEx ? "Show less" : "Show more"}</button>` : "") +
      `<div class="nact">` +
      `<button data-edit="${esc(n.id)}" style="--c1:#fbbf24;--c2:#f59e0b" title="Edit">${SVG.edit}</button>` +
      `<button data-copy="${esc(n.id)}" style="--c1:#22d3ee;--c2:#0ea5e9" title="Copy">${SVG.copy}</button>` +
      `<button data-dl="${esc(n.id)}" style="--c1:#34d399;--c2:#10b981" title="${NOTEDL[n.id] ? "Open" : "Download"}">${NOTEDL[n.id] ? SVG.open : SVG.dl}</button>` +
      `<button data-share="${esc(n.id)}" style="--c1:#a78bfa;--c2:#8b5cf6" title="Share">${SVG.share}</button>` +
      `<button data-del="${esc(n.id)}" style="--c1:#f87171;--c2:#ef4444" title="Delete">${SVG.del}</button></div></div>`;
    }).join("");
    snbox.querySelectorAll(".snipcard").forEach(card => {
      const nid = card.dataset.nid;
      const more = card.querySelector(".snipmore");
      if(more) more.onclick = () => {
        const ex = card.classList.toggle("expanded");
        if(ex) EXPANDED.add(nid); else EXPANDED.delete(nid);
        more.textContent = ex ? "Show less" : "Show more";
      };
      card.querySelector("[data-edit]").onclick = e => editNote(e.currentTarget.dataset.edit, card);
      card.querySelector("[data-copy]").onclick = e => copyNote(e.currentTarget.dataset.copy);
      card.querySelector("[data-dl]").onclick = e => noteDlTap(e.currentTarget);
      card.querySelector("[data-share]").onclick = e => shareNote(e.currentTarget.dataset.share);
      card.querySelector("[data-del]").onclick = e => delNote(e.currentTarget.dataset.del);
    });
  } catch(e){ snbox.innerHTML = '<p class="empty">Could not load shared texts.</p>'; }
}

const delallbtn = $("#delallbtn");
if(delallbtn) delallbtn.onclick = async () => {
  const n = parseInt(($("#filecount").textContent || "0"), 10) || 0;
  if(!n) return;
  if(!confirm("Delete All " + n + " File(s) From This PC?\\n\\nThis Will Delete All The Files Unless Saved On This Device.")) return;
  try {
    await fetch("/api/delete_all", {method:"POST"});
    lastFilesSig = null; loadFiles(); toast("Deleted " + n + " file(s)");
  } catch(e){ toast("Couldn't delete files"); }
};
const delallnotesbtn = $("#delallnotesbtn");
if(delallnotesbtn) delallnotesbtn.onclick = async () => {
  const n = parseInt(($("#snipcount").textContent || "0"), 10) || 0;
  if(!n) return;
  if(!confirm("Delete All " + n + " Shared Text(s)? This Can't Be Undone.")) return;
  try {
    await fetch("/api/notes/delete_all", {method:"POST"});
    loadNotes(); toast("Deleted " + n + " Shared Text(s)");
  } catch(e){ toast("Couldn't Delete Shared Texts"); }
};
async function delFile(name){
  if(!confirm("Delete "+name+"?")) return;
  await fetch("/api/delete", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name})});
  loadFiles(); toast("Deleted "+name);
}

const mdlg = $("#mdlg"), mnote = $("#mnote");
$("#maddnote").onclick = () => { mnote.value=""; mdlg.classList.add("open"); setTimeout(() => mnote.focus(), 60); };
$("#mcancel").onclick = () => mdlg.classList.remove("open");
mdlg.addEventListener("click", e => { if(e.target === mdlg) mdlg.classList.remove("open"); });
$("#msave").onclick = async () => {
  const text = mnote.value.trim();
  if(!text){ toast("Type something first"); return; }
  try {
    const r = await fetch("/api/snippet", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({text})});
    const j = await r.json();
    if(j.ok){ mnote.value=""; mdlg.classList.remove("open"); toast("\\u2705 Shared Text Added"); loadNotes(); }
    else toast("Couldn't save that note");
  } catch(e){ toast("Couldn't save that note"); }
};
const mtabs = document.querySelectorAll(".mtab");
function setTab(name){
  document.body.dataset.mtab = name;
  const qpp = document.getElementById("qrpop");
  if(qpp) qpp.classList.remove("open");
  document.querySelectorAll(".board .col").forEach(c =>
    c.classList.toggle("active", c.dataset.col === name));
  mtabs.forEach(t => t.classList.toggle("sel", t.dataset.tab === name));
  try { localStorage.setItem("dropit_tab", name); } catch(e){}
}
mtabs.forEach(t => t.onclick = () => setTab(t.dataset.tab));
try { setTab(localStorage.getItem("dropit_tab") || "send"); } catch(e){ setTab("send"); }
async function loadStatus(){
  try {
    const r = await fetch("/api/status"); const j = await r.json();
    const n = j.devices || 0;
    const t = $("#statustext"), d = $("#statusdot");
    if(!t || !d) return;
    if(n <= 0){ t.textContent = "No Devices Connected"; d.classList.add("idle"); }
    else {
      t.textContent = n + (n === 1 ? " Device Connected" : " Devices Connected");
      d.classList.remove("idle");
    }
  } catch(e){}
}
loadFiles(); loadNotes(); loadStatus();
setInterval(() => { loadFiles(); loadNotes(); loadStatus(); }, 5000);
// The pywebview bridge (desktop app) loads after first paint: once it's ready,
// file buttons must say Open, not Download — force one re-render when it appears.
let bridgeSeen = hasBridge();
window.addEventListener("pywebviewready", () => {
  if(!bridgeSeen && hasBridge()){ bridgeSeen = true; lastFilesSig = null; loadFiles(); }
});
// ---------- custom right-click menu (desktop only: pywebview shows no native menu;
// phones keep their native long-press menu, so this stays off touch devices) ----------
(function(){
  const ctx = document.createElement("div");
  ctx.id = "ctxmenu"; ctx.style.display = "none";
  document.body.appendChild(ctx);
  const hideCtx = () => { ctx.style.display = "none"; };
  document.addEventListener("click", e => { if(!ctx.contains(e.target)) hideCtx(); });
  document.addEventListener("keydown", e => { if(e.key === "Escape") hideCtx(); });
  window.addEventListener("blur", hideCtx);
  let lastMenuAt = 0;
  const isDesktop = () => {
    try { if(window.matchMedia && matchMedia("(pointer:fine)").matches) return true; } catch(e){}
    try { if(typeof hasBridge === "function" && hasBridge()) return true; } catch(e){}
    return false;
  };
  const editableAt = el => el && el.closest &&
    el.closest("textarea, input:not([type=button]):not([type=submit]):not([type=checkbox]), [contenteditable='true']");
  const selectElText = el => {
    const r = document.createRange(); r.selectNodeContents(el);
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  };
  // Read the clipboard. Desktop app: native bridge (no permission prompt, always
  // accurate). Plain browser: only read when permission is already granted — never
  // cold-prompt on a mere right-click.
  const readClip = async () => {
    try {
      if(typeof hasBridge === "function" && hasBridge()){
        const api = window.pywebview.api;
        if(api && typeof api.get_clipboard === "function"){
          const r = await api.get_clipboard();
          if(r && typeof r.text === "string") return r.text;
        }
      }
    } catch(e){}
    try {
      let granted = false;
      if(navigator.permissions && navigator.permissions.query){
        const st = await navigator.permissions.query({name: "clipboard-read"});
        granted = st.state === "granted";
      } else { granted = true; }
      if(!granted) return "";
      const r = await Promise.race([
        navigator.clipboard.readText(),
        new Promise(res => setTimeout(() => res(""), 800))
      ]);
      return r || "";
    } catch(err){ return ""; }
  };
  const showMenu = async (e) => {
    const ed = editableAt(e.target);
    const sel = (window.getSelection() || "").toString();
    const card = e.target.closest ? e.target.closest(".snipcard") : null;
    const noteTextEl = card ? card.querySelector(".sniptext") : null;
    if(!ed && !noteTextEl) return false;
    const cx = e.clientX, cy = e.clientY;
    const clipText = await readClip();
    ctx.innerHTML = "";
    const addItem = (label, fn, disabled) => {
      const b = document.createElement("button");
      b.className = "ctxitem" + (disabled ? " off" : "");
      b.textContent = label;
      if(!disabled) b.onclick = () => { hideCtx(); fn(); };
      ctx.appendChild(b);
    };
    if(ed){
      const edSel = ed.selectionStart !== undefined && ed.selectionStart !== ed.selectionEnd;
      const text = sel || (edSel ? ed.value.slice(ed.selectionStart, ed.selectionEnd) : "");
      addItem("Select all", () => { ed.focus(); ed.select(); });
      addItem("Copy", () => copyText(text), !text);
      // Paste is always shown; greyed out and unclickable when the clipboard is empty.
      addItem("Paste", () => {
        ed.focus();
        const s = ed.selectionStart !== undefined ? ed.selectionStart : ed.value.length;
        const en = ed.selectionEnd !== undefined ? ed.selectionEnd : ed.value.length;
        ed.setRangeText(clipText, s, en, "end");
        ed.dispatchEvent(new Event("input", {bubbles:true}));
      }, !clipText);
    } else {
      addItem("Select all", () => selectElText(noteTextEl));
      addItem("Copy", () => copyText(sel), !sel);
    }
    const x = Math.min(cx, window.innerWidth - 170);
    const y = Math.min(cy, window.innerHeight - ctx.children.length * 38 - 16);
    ctx.style.left = Math.max(4, x) + "px";
    ctx.style.top = Math.max(4, y) + "px";
    ctx.style.display = "block";
    return true;
  };
  // Primary trigger: the standard contextmenu event.
  document.addEventListener("contextmenu", e => {
    if(!isDesktop()) return;  // touch phones keep their native menu
    if(Date.now() - lastMenuAt < 600) return;  // already shown by the mousedown fallback
    e.preventDefault(); hideCtx();
    lastMenuAt = Date.now();
    showMenu(e);
  });
  // Fallback trigger: some embedded webviews swallow contextmenu, but the
  // right mouse button press itself still fires.
  document.addEventListener("mousedown", e => {
    if(e.button !== 2 || !isDesktop()) return;
    const ed = editableAt(e.target);
    const card = e.target.closest ? e.target.closest(".snipcard") : null;
    if(!ed && !(card && card.querySelector(".sniptext"))) return;
    e.preventDefault();
    lastMenuAt = Date.now();
    showMenu(e);
  });
})();
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


def asset_path(name: str):
    """Path to a bundled asset; works frozen (PyInstaller) and from source."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (os.path.join(base, "assets", name), os.path.join(base, name)):
            if os.path.isfile(cand):
                return cand
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "assets", name), os.path.join(here, name)):
        if os.path.isfile(cand):
            return cand
    return None


_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg"}


def _is_image(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS if "." in name else False


_THUMB_CACHE = {}


def _thumbnail(path: str):
    """Return (bytes, content_type) for a small preview of an image file."""
    try:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return None
    hit = _THUMB_CACHE.get(key)
    if hit:
        return hit
    data, ctype = None, "image/jpeg"
    try:
        from PIL import Image

        img = Image.open(path)
        img.thumbnail((384, 384), Image.LANCZOS)
        if img.mode in ("RGBA", "LA", "PA"):
            bg = Image.new("RGB", img.size, (10, 13, 22))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        import io

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=72)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001 - no PIL or unreadable: serve original
        try:
            with open(path, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        except OSError:
            return None
    if data is None:
        return None
    if len(_THUMB_CACHE) > 300:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[key] = (data, ctype)
    return data, ctype


def safe_name(name: str) -> str:
    name = os.path.basename(name).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "upload"


_DOCX_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_html(path: str):
    """Convert a .docx file to simple sanitized HTML using only the stdlib.
    Returns the HTML string, or None if the file can't be read/parsed."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
    except Exception:
        return None
    W = _DOCX_W
    body = root.find(W + "body")
    if body is None:
        return None

    # Elements whose subtree can carry visible text; recursed in document order.
    # w:del / w:delText (tracked deletions) are skipped so the preview shows final text.
    # Property elements (pPr, rPr, sectPr, ...) carry no visible text and are skipped.
    _SKIP = {W + "del", W + "delText", W + "pPr", W + "rPr", W + "sectPr",
             W + "sdtPr", W + "numPr", W + "tblPr", W + "trPr", W + "tcPr"}

    def runs(el, fmt):
        """Yield (text, fmt) for visible text under el, in document order.
        Recurses through hyperlink / tracked-insertion / content-control /
        textbox wrappers so real-world Word files don't lose their text."""
        for child in el:
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP:
                continue
            if tag == W + "t":
                yield (child.text or "", fmt)
            elif tag == W + "tab":
                yield (" ", fmt)
            elif tag == W + "br":
                yield ("\n", fmt)
            elif tag == W + "r":
                rpr = child.find(W + "rPr")
                f = fmt
                if rpr is not None:
                    f = dict(fmt)
                    if rpr.find(W + "b") is not None:
                        f["b"] = True
                    if rpr.find(W + "i") is not None:
                        f["i"] = True
                    if rpr.find(W + "u") is not None:
                        f["u"] = True
                yield from runs(child, f)
            elif tag.startswith(W):
                yield from runs(child, fmt)
            # Other namespaces (drawings, embedded objects): nothing readable.

    def styled(text, fmt):
        t = html.escape(text)
        if fmt.get("b"):
            t = "<b>" + t + "</b>"
        if fmt.get("i"):
            t = "<i>" + t + "</i>"
        if fmt.get("u"):
            t = "<u>" + t + "</u>"
        return t

    def para_html(p):
        parts = []
        for text, fmt in runs(p, {}):
            if text == "\n":
                parts.append("<br>")
            elif text.strip():
                parts.append(styled(text, fmt))
            elif text:
                parts.append(" ")
        return "".join(parts)

    def blocks(el):
        out = []
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == W + "p":
                ppr = child.find(W + "pPr")
                style, is_list = "", False
                if ppr is not None:
                    ps = ppr.find(W + "pStyle")
                    if ps is not None:
                        style = ps.get(W + "val", "") or ""
                    is_list = ppr.find(W + "numPr") is not None
                inner = para_html(child)
                if not inner.strip():
                    out.append("<p>&nbsp;</p>")
                elif style.startswith("Heading"):
                    digits = "".join(c for c in style if c.isdigit())
                    lvl = max(1, min(6, int(digits) if digits else 1))
                    out.append("<h%d>%s</h%d>" % (lvl, inner, lvl))
                elif is_list:
                    out.append("<p>&bull; " + inner + "</p>")
                else:
                    out.append("<p>" + inner + "</p>")
            elif tag == W + "tbl":
                rows = []
                for tr in child.findall(W + "tr"):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        txt = " ".join(para_html(p) for p in tc.findall(W + "p"))
                        cells.append("<td>" + txt + "</td>")
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                out.append('<table class="dx">' + "".join(rows) + "</table>")
            elif tag == W + "sdt":
                # Structured document tag (content control): render its content.
                content = child.find(W + "sdtContent")
                if content is not None:
                    out.append(blocks(content))
        return "".join(out)

    return blocks(body)


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
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _send_file(self, path, name, ctype, download=True):
        """Serve a file with Range support (video thumbnails, seeking)."""
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rh = self.headers.get("Range")
        if rh:
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rh)
            if m:
                s, e = m.groups()
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                end = min(end, size - 1)
                if 0 <= start <= end < size:
                    code = 206
                else:
                    start, end = 0, size - 1
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        _note_seen(self.client_address[0])
        path, _, query = self.path.partition("?")
        if path == "/":
            qr_block = (
                '<div class="qr"><img src="/qr.png" alt="QR code">'
                "<div>Scan with your<br>phone camera</div></div>"
                if HAS_QR else ""
            )
            qr_mini = '<img src="/qr.png" alt="QR code">' if HAS_QR else ""
            desktop = getattr(self.server, "desktop", False)
            # A phone browser hitting the desktop app's server still needs the
            # mobile header (Saved button) - the desktop flag is server-global.
            _ua = (self.headers.get("User-Agent") or "").lower()
            _mobile = ("mobi" in _ua or "android" in _ua or "iphone" in _ua or "ipad" in _ua)
            if desktop and not _mobile:
                # Desktop: one button row under the header - Open | Change | Delete all
                file_head = ""
                file_btns = (
                    '<div class="fbtnrow">'
                    '<button class="btn openf" id="openfolder">Open Folder</button>'
                    '<button class="btn changef" id="changefolder">Change Folder</button>'
                    '<button class="btn dangergrad" id="delallbtn" style="display:none">' + _SVG_TRASH + ' Delete All</button>'
                    "</div>"
                )
            else:
                # Mobile web keeps the stacked Delete all / Saved buttons in the header
                file_head = (
                    '<span style="flex:1"></span><span class="fstack">'
                    '<button class="btn dangergrad" id="delallbtn" style="display:none">' + _SVG_TRASH + ' Delete All</button>'
                    '<button class="msavedbtn" id="msavedbtn">&#x1F4F2; Saved</button>'
                    "</span>"
                )
                file_btns = ""
            page = (
                PAGE.replace("__VERSION__", VERSION)
                .replace("__URL__", html.escape(self.server.url))
                .replace("__DIRNAME__", html.escape(self.server.directory))
                .replace("__QR__", qr_block)
                .replace("__QRMINI__", qr_mini)
                .replace("__FILEHEAD__", file_head)
                .replace("__FILEBTNS__", file_btns)
            )
            self._send(200, page.encode(), headers={"Cache-Control": "no-store"})
        elif path == "/qr.png":
            try:
                self._send(200, qr_png(self.server.url), "image/png")
            except Exception:  # noqa: BLE001 - qrcode/Pillow not installed
                self._send(404, b"qr unavailable", "text/plain")
        elif path == "/manifest.json":
            self._send(200, json.dumps({
                "name": "DropIt",
                "short_name": "DropIt",
                "description": "Send files between your phone and PC over WiFi",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0a0e1a",
                "theme_color": "#0a0e1a",
                "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}],
            }).encode(), "application/manifest+json")
        elif path == "/icon.png":
            p = asset_path("icon.png")
            if p:
                with open(p, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"no icon", "text/plain")
        elif path.startswith("/thumb/"):
            name = safe_name(urllib.parse.unquote(path[len("/thumb/"):]))
            path = os.path.join(self.server.directory, name)
            if not os.path.isfile(path) or not _is_image(name):
                self._send(404, b"not found", "text/plain")
            else:
                made = _thumbnail(path)
                if made is None:
                    self._send(404, b"unreadable", "text/plain")
                else:
                    data, ctype = made
                    self._send(200, data, ctype)
        elif path == "/api/files":
            items = []
            names = [n for n in os.listdir(self.server.directory)
                     if os.path.isfile(os.path.join(self.server.directory, n))]
            names.sort(key=lambda n: os.path.getmtime(os.path.join(self.server.directory, n)),
                       reverse=True)
            for name in names:
                fp = os.path.join(self.server.directory, name)
                items.append({"name": name,
                              "size": os.path.getsize(fp),
                              "mtime": int(os.path.getmtime(fp))})
            self._json(items)
        elif path == "/api/notes":
            with _notes_lock:
                notes = load_notes()
            self._json(notes)
        elif path == "/api/status":
            self._json({"devices": _device_count()})
        elif path == "/api/notes/dl":
            qs = query
            nid = ""
            for part in qs.split("&"):
                k, _, v = part.partition("=")
                if k == "id":
                    nid = urllib.parse.unquote(v)
            with _notes_lock:
                notes = load_notes()
            note = next((n for n in notes if n.get("id") == nid), None)
            if note is None:
                self._send(404, b"not found", "text/plain")
                return
            body = (note["text"] + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             'attachment; filename="shared-text-%s.txt"' % nid)
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/docx/"):
            qs = query
            name = ""
            for part in qs.split("&"):
                k, _, v = part.partition("=")
                if k == "name":
                    name = urllib.parse.unquote(v)
            name = safe_name(name)
            if not name.lower().endswith(".docx"):
                self._send(404, b"not found", "text/plain")
                return
            fpath = os.path.join(self.server.directory, name)
            if not os.path.isfile(fpath):
                self._send(404, b"not found", "text/plain")
                return
            converted = docx_to_html(fpath)
            if not converted:
                self._send(500, b"cannot preview", "text/plain")
                return
            self._send(200, converted.encode("utf-8"), "text/html; charset=utf-8")
        elif path.startswith("/dl/"):
            name = safe_name(urllib.parse.unquote(path[len("/dl/"):]))
            path = os.path.join(self.server.directory, name)
            if not os.path.isfile(path):
                self._send(404, b"not found", "text/plain")
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send_file(path, name, ctype)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        _note_seen(self.client_address[0])
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        ctype = self.headers.get("Content-Type") or ""

        if path == "/api/upload" and "multipart/form-data" in ctype:
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
        elif path == "/api/delete":
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
        elif path == "/api/delete_all":
            try:
                n = 0
                for name in os.listdir(self.server.directory):
                    path = os.path.join(self.server.directory, name)
                    if os.path.isfile(path):
                        os.remove(path)
                        n += 1
                self._json({"ok": True, "deleted": n})
            except OSError:
                self._json({"ok": False}, 400)
        elif path == "/api/snippet":
            # Share a text note. Stored in the app's notes.json (app data dir),
            # NOT as a file in the shared folder.
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                text = data.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    self._json({"ok": False, "error": "empty"}, 400)
                    return
                text = text.strip()
                if len(text) > 100_000:
                    self._json({"ok": False, "error": "too long (100k chars max)"}, 400)
                    return
                with _notes_lock:
                    note = add_note(text)
                self.log_message("note %s (%d chars)", note["id"], len(text))
                self._json({"ok": True, "id": note["id"], "name": note["id"]})
            except (json.JSONDecodeError, OSError):
                self._json({"ok": False}, 400)
        elif path == "/api/notes/delete":
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                nid = data.get("id", "")
                with _notes_lock:
                    ok = delete_note(nid) if isinstance(nid, str) and nid else False
                self._json({"ok": ok}, 200 if ok else 404)
            except (json.JSONDecodeError, OSError):
                self._json({"ok": False}, 400)
        elif path == "/api/notes/delete_all":
            try:
                with _notes_lock:
                    n = len(load_notes())
                    save_notes([])
                self._json({"ok": True, "deleted": n})
            except OSError:
                self._json({"ok": False}, 400)
        elif path == "/api/notes/edit":
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                nid = data.get("id", "")
                text = data.get("text", "")
                ok = False
                if isinstance(nid, str) and nid and isinstance(text, str) and text.strip():
                    if len(text) > 100_000:
                        self._json({"ok": False, "error": "too long (100k chars max)"}, 400)
                        return
                    with _notes_lock:
                        ok = edit_note(nid, text)
                self._json({"ok": ok}, 200 if ok else 404)
            except (json.JSONDecodeError, OSError):
                self._json({"ok": False}, 400)
        else:
            self._send(404, b"not found", "text/plain")


def make_server(port: int, directory: str, desktop: bool = False) -> ThreadingHTTPServer:
    os.makedirs(directory, exist_ok=True)
    migrate_snippets(os.path.abspath(directory))
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
