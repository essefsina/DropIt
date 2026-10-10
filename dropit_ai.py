"""DropIt AI file search — semantic retrieval over the shared folder.

No generative AI, no chat, no cloud, no accounts. Three small local models:
  - MiniLM (text): matches prompts against file names + document contents.
  - CLIP (vision): matches prompts against what is *in* pictures.
  - RapidOCR: reads text inside images (indexed for search, shown in chat).
All models ship inside DropIt's own folder (ai_model/) — nothing to download.

Public API used by dropit.py:
    search_files(directory, data_dir, query, top_k=8) -> dict
    index_status(directory, data_dir) -> dict
    ocr_image(directory, name) -> dict   (transcribe one image for the chat)

Heavy deps (onnxruntime/tokenizers/numpy/Pillow/rapidocr) are imported
lazily so dropit.py stays light until the first AI search.
"""

import os
import pickle
import re
import sys
import threading
import time

INDEX_VERSION = 4


def _index_version():
    """Index version includes available components: installing a new
    capability (OCR/vision) invalidates the old index so files get
    re-embedded with it. Never silently serves a stale index."""
    v = INDEX_VERSION
    try:
        import rapidocr_onnxruntime  # noqa: F401
        v += 10
    except ImportError:
        pass
    try:
        import PIL.Image  # noqa: F401
        v += 20
    except ImportError:
        pass
    return v


def component_status():
    """Which AI components are actually ready on this machine."""
    st = {"search": model_ready(), "vision": False, "ocr": False,
          "translate": False, "missing": []}
    try:
        import PIL.Image  # noqa: F401
        st["vision"] = clip_ready()
        if not clip_ready():
            st["missing"].append("ai_model/clip files")
    except ImportError:
        st["missing"].append("Pillow")
    try:
        import rapidocr_onnxruntime  # noqa: F401
        st["ocr"] = True
    except ImportError:
        st["missing"].append("rapidocr-onnxruntime")
    try:
        import langdetect  # noqa: F401
        st["translate"] = True
    except ImportError:
        st["missing"].append("langdetect")
    if not model_ready():
        st["missing"].append("ai_model/minilm files")
    return st
MAX_TEXT_CHARS = 2000       # per-file text fed to the embedder
MAX_TOKENS = 256            # MiniLM input length
CLIP_TOKENS = 77            # CLIP text input length
MIN_SCORE = 0.30            # MiniLM text threshold
CLIP_MIN_SCORE = 0.25       # CLIP vision floor (adaptive cutoff above)
FILENAME_BOOST = 0.15       # exact filename substring match bonus

_sess = None
_tok = None
_clip_v = None
_clip_t = None
_clip_tok = None
_ocr = None
_model_lock = threading.Lock()
_index_lock = threading.Lock()


def _model_roots():
    """Candidate ai_model locations (source / exe / PyInstaller bundle)."""
    roots = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ai_model")]
    exe = sys.executable
    if exe:
        roots.append(os.path.join(os.path.dirname(os.path.abspath(exe)),
                                  "ai_model"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(os.path.join(meipass, "ai_model"))
    return roots


def _find(sub, *files):
    for r in _model_roots():
        d = os.path.join(r, sub)
        if all(os.path.isfile(os.path.join(d, f)) for f in files):
            return d
    return None


def _minilm_dir():
    return _find("minilm", "model.onnx", "tokenizer.json")


def _clip_dir():
    return _find("clip", "vision_model_quantized.onnx",
                 "text_model_quantized.onnx", "tokenizer.json")


def model_ready():
    return _minilm_dir() is not None


def _get_model():
    global _sess, _tok
    if _sess is None:
        with _model_lock:
            if _sess is None:
                import onnxruntime as ort
                from tokenizers import Tokenizer
                md = _minilm_dir()
                _sess = ort.InferenceSession(
                    os.path.join(md, "model.onnx"),
                    providers=["CPUExecutionProvider"])
                _tok = Tokenizer.from_file(os.path.join(md, "tokenizer.json"))
                _tok.enable_truncation(max_length=MAX_TOKENS)
                _tok.enable_padding(length=MAX_TOKENS)
    return _sess, _tok


def _encode(texts):
    """MiniLM: texts -> L2-normalized float32 vectors."""
    import numpy as np
    sess, tok = _get_model()
    batch = tok.encode_batch(texts)
    ids = np.array([e.ids for e in batch], dtype=np.int64)
    mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in [i.name for i in sess.get_inputs()]:
        feeds["token_type_ids"] = np.array(
            [e.type_ids for e in batch], dtype=np.int64)
    last = sess.run(None, feeds)[0]
    m = mask[..., None].astype(np.float32)
    summed = (last * m).sum(axis=1)
    counts = np.maximum(m.sum(axis=1), 1e-9)
    v = summed / counts
    norms = np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return (v / norms).astype(np.float32)


# ---------------------------------------------------------- CLIP (vision)
CLIP_MEAN = None  # filled lazily to keep numpy import lazy
CLIP_STD = None


def _clip_models():
    global _clip_v, _clip_t, _clip_tok
    if _clip_v is None:
        with _model_lock:
            if _clip_v is None:
                import onnxruntime as ort
                from tokenizers import Tokenizer
                md = _clip_dir()
                if md is None:
                    raise RuntimeError("clip model missing")
                _clip_v = ort.InferenceSession(
                    os.path.join(md, "vision_model_quantized.onnx"),
                    providers=["CPUExecutionProvider"])
                _clip_t = ort.InferenceSession(
                    os.path.join(md, "text_model_quantized.onnx"),
                    providers=["CPUExecutionProvider"])
                _clip_tok = Tokenizer.from_file(
                    os.path.join(md, "tokenizer.json"))
                _clip_tok.enable_truncation(max_length=CLIP_TOKENS)
                _clip_tok.enable_padding(length=CLIP_TOKENS)
    return _clip_v, _clip_t, _clip_tok


def _clip_prep(path):
    """Image file -> CLIP pixel_values (1,3,224,224) float32."""
    import numpy as np
    from PIL import Image
    im = Image.open(path).convert("RGB").resize((224, 224))
    a = np.asarray(im, dtype=np.float32) / 255.0
    mean = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
    a = (a - mean) / std
    return a.transpose(2, 0, 1)[None].astype(np.float32)


def _clip_encode_images(paths):
    import numpy as np
    vsess, _, _ = _clip_models()
    batch = np.concatenate([_clip_prep(p) for p in paths], axis=0)
    v = vsess.run(None, {"pixel_values": batch})[0]
    norms = np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return (v / norms).astype(np.float32)


def _clip_encode_text(texts):
    import numpy as np
    _, tsess, tok = _clip_models()
    batch = tok.encode_batch(texts)
    ids = np.array([e.ids for e in batch], dtype=np.int64)
    v = tsess.run(None, {"input_ids": ids})[0]
    norms = np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return (v / norms).astype(np.float32)


def clip_ready():
    return _clip_dir() is not None


# ------------------------------------------------------------------- OCR
def _get_ocr():
    global _ocr
    if _ocr is None:
        with _model_lock:
            if _ocr is None:
                from rapidocr_onnxruntime import RapidOCR
                _ocr = RapidOCR()
    return _ocr


def _ocr_text(fpath):
    """Transcribed text in an image. Empty string on failure/blank."""
    try:
        ocr = _get_ocr()
        res, _ = ocr(fpath)
        if not res:
            return ""
        return " ".join(t[1] for t in res if len(t) > 1 and t[1]).strip()
    except Exception:
        return ""


def _ocr_text_strict(fpath):
    """Like _ocr_text but raises with the real error (for diagnostics)."""
    ocr = _get_ocr()
    res, _ = ocr(fpath)
    if not res:
        return ""
    return " ".join(t[1] for t in res if len(t) > 1 and t[1]).strip()


TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json",
    ".py", ".js", ".ts", ".html", ".htm", ".css", ".xml", ".yaml",
    ".yml", ".ini", ".cfg", ".toml", ".sh", ".bat", ".ps1", ".java",
    ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".sql", ".rst",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _index_path(data_dir):
    return os.path.join(data_dir, "ai_index.pkl")


def extract_text(fpath):
    """Best-effort searchable text for one file. Never raises.
    Images contribute their OCR transcription."""
    try:
        ext = os.path.splitext(fpath)[1].lower()
        if ext in IMAGE_EXTS:
            return _ocr_text(fpath)
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(fpath)
                parts = []
                for page in reader.pages[:20]:
                    parts.append(page.extract_text() or "")
                    if sum(len(p) for p in parts) > MAX_TEXT_CHARS:
                        break
                return " ".join(parts)[:MAX_TEXT_CHARS]
            except Exception:
                return ""
        if ext == ".docx":
            try:
                import docx
                doc = docx.Document(fpath)
                text = "\n".join(p.text for p in doc.paragraphs)
                return text[:MAX_TEXT_CHARS]
            except Exception:
                return ""
        if ext in TEXT_EXTS:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                return f.read(MAX_TEXT_CHARS)
        return ""
    except Exception:
        return ""


def _describe(name, text):
    """Single string the text embedder sees for a file."""
    base = os.path.splitext(name)[0].replace("_", " ").replace("-", " ")
    if text and text.strip():
        return "%s. %s" % (base, text.strip()[:MAX_TEXT_CHARS])
    return base


def _load_index(data_dir):
    p = _index_path(data_dir)
    if not os.path.isfile(p):
        return {"v": _index_version(), "files": {}, "order": []}
    try:
        with open(p, "rb") as f:
            idx = pickle.load(f)
        if idx.get("v") != _index_version():
            return {"v": _index_version(), "files": {}, "order": []}
        return idx
    except Exception:
        return {"v": _index_version(), "files": {}, "order": []}


def _save_index(data_dir, index):
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_index_path(data_dir), "wb") as f:
            pickle.dump(index, f)
    except Exception:
        pass


def build_index(directory, data_dir, progress_cb=None):
    """(Re)build the embedding index. Only new/changed files are embedded."""
    with _index_lock:
        index = _load_index(data_dir)
        files = index.get("files", {})

        try:
            names = [n for n in os.listdir(directory)
                     if os.path.isfile(os.path.join(directory, n))]
        except OSError:
            names = []

        for n in list(files):
            if n not in names:
                del files[n]

        todo = []
        for n in names:
            fp = os.path.join(directory, n)
            try:
                mtime = os.path.getmtime(fp)
                size = os.path.getsize(fp)
            except OSError:
                continue
            rec = files.get(n)
            if rec and rec.get("mtime") == mtime and rec.get("size") == size \
                    and "vec" in rec:
                continue
            todo.append(n)

        total = len(todo)
        # text embeddings (all files; images contribute OCR transcription)
        texts = [extract_text(os.path.join(directory, n)) for n in todo]
        descs = [_describe(n, t) for n, t in zip(todo, texts)]
        vecs = _encode(descs) if descs else []
        # vision embeddings (images only)
        img_idx = [i for i, n in enumerate(todo)
                   if is_image(n) and clip_ready()]
        cvecs = {}
        if img_idx:
            try:
                embs = _clip_encode_images(
                    [os.path.join(directory, todo[i]) for i in img_idx])
                for k, i in enumerate(img_idx):
                    cvecs[i] = embs[k]
            except Exception:
                pass

        for i, n in enumerate(todo):
            fp = os.path.join(directory, n)
            try:
                mtime = os.path.getmtime(fp)
                size = os.path.getsize(fp)
            except OSError:
                continue
            rec = {"vec": vecs[i], "mtime": mtime, "size": size}
            if i in cvecs:
                rec["vec_clip"] = cvecs[i]
            if is_image(n):
                rec["has_ocr"] = bool(texts[i].strip())
            else:
                rec["has_text"] = bool(texts[i].strip())
            files[n] = rec
            if progress_cb:
                progress_cb(i + 1, total)

        index["v"] = _index_version()
        index["files"] = files
        index["order"] = sorted(files)
        _save_index(data_dir, index)
        return {"indexed": len(files), "embedded_now": total}


def _stem(w):
    """Crude English stemming so 'screenshots' matches 'screenshot'."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _parse_date_filter(query):
    """Return (cleaned_query, since_ts|None, until_ts|None).

    Handles relative ("last week", "yesterday") and absolute
    ("oct 2", "october 5", "10/2") dates. Absolute dates become a
    single-day range; relative dates run until now.
    """
    import calendar
    now = time.time()
    day = 86400
    q = query.lower()
    since = until = None
    patterns = [
        (r"\bfrom yesterday\b|\byesterday\b", now - day, now),
        (r"\bfrom today\b|\btoday\b", now - day, now),
        (r"\bfrom last week\b|\blast week\b|\bpast week\b",
         now - 7 * day, now),
        (r"\bfrom last month\b|\blast month\b|\bpast month\b",
         now - 30 * day, now),
        (r"\bfrom last year\b|\blast year\b|\bpast year\b",
         now - 365 * day, now),
    ]
    for pat, s, u in patterns:
        if re.search(pat, q):
            since, until = s, u
            q = re.sub(pat, " ", q)
            break
    # absolute: "oct 2", "october 5", "oct. 2"
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    if since is None:
        m = re.search(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b", q)
        if m:
            mon = months[m.group(1)[:3]]
            daynum = int(m.group(2))
            yr = time.localtime(now).tm_year
            ts = None
            try:
                ts = time.mktime((yr, mon, daynum, 0, 0, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                pass
            if ts and ts > now:  # hasn't happened yet → last year
                try:
                    ts = time.mktime((yr - 1, mon, daynum, 0, 0, 0, 0, 0, -1))
                except (OverflowError, ValueError):
                    ts = None
            if ts:
                since, until = ts, ts + day
                q = q.replace(m.group(0), " ")
    # absolute numeric: "10/2", "10-2" (month/day)
    if since is None:
        m = re.search(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])\b", q)
        if m:
            mon, daynum = int(m.group(1)), int(m.group(2))
            yr = time.localtime(now).tm_year
            ts = None
            try:
                ts = time.mktime((yr, mon, daynum, 0, 0, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                pass
            if ts and ts > now:
                try:
                    ts = time.mktime((yr - 1, mon, daynum, 0, 0, 0, 0, 0, -1))
                except (OverflowError, ValueError):
                    ts = None
            if ts:
                since, until = ts, ts + day
                q = q.replace(m.group(0), " ")
    m = re.search(r"\b(19|20)\d{2}\b", q)
    if m and since is None:
        year = int(m.group(0))
        since = calendar.timegm((year, 1, 1, 0, 0, 0))
        until = calendar.timegm((year + 1, 1, 1, 0, 0, 0))
        q = q.replace(m.group(0), " ")
    q = re.sub(r"\s+", " ", q).strip(" ,.")
    return q, since, until


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def search_files(directory, data_dir, query, top_k=50):
    """Prompt in -> ranked file list out. Never raises."""
    import numpy as np

    try:
        if not model_ready():
            return {"ok": False, "results": [], "message": "no_model"}
        build_index(directory, data_dir)
        index = _load_index(data_dir)
        files = index.get("files", {})
        if not files:
            return {"ok": True, "results": [], "message": "empty"}

        q, since, until = _parse_date_filter(query or "")
        if not q:
            # pure date query ("oct 2") → list everything on that date
            if since:
                out = [
                    {"name": n, "size": files[n].get("size", 0),
                     "mtime": int(files[n].get("mtime", 0))}
                    for n in index.get("order", [])
                    if n in files and since <= files[n].get("mtime", 0)
                    and (until is None or files[n].get("mtime", 0) < until)
                ]
                out.sort(key=lambda r: r["mtime"], reverse=True)
                return {"ok": True, "results": out[:top_k],
                        "message": f"{len(out)} files"}
            return {"ok": True, "results": [], "message": "empty_query"}

        qvec = _encode([q])[0]
        qclip = _clip_encode_text([q])[0] if clip_ready() else None
        qlow = q.lower()

        scored = []
        for name in index.get("order", []):
            rec = files.get(name)
            if not rec or "vec" not in rec:
                continue
            if since and rec.get("mtime", 0) < since:
                continue
            if until and rec.get("mtime", 0) >= until:
                continue
            img = is_image(name)
            # word-level filename match (stemmed: "screenshots" hits
            # "screenshot"; not substring: "logo" != "catalogue")
            namewords = set(re.findall(r"[a-z0-9]+", name.lower()))
            qwords = set(re.findall(r"[a-z0-9]+", qlow))
            qstems = set(_stem(w) for w in qwords)
            nstems = set(_stem(w) for w in namewords)
            word_hit = bool(qstems & nstems)
            text_s = float(np.dot(qvec, rec["vec"]))
            if img and not rec.get("has_ocr") and not word_hit:
                # filename-only image descriptions are noise: ignore unless
                # the user literally typed (part of) the file's name
                text_s = -1.0
            elif word_hit:
                text_s += FILENAME_BOOST
            clip_s = -1.0
            if qclip is not None and "vec_clip" in rec:
                clip_s = float(np.dot(qclip, rec["vec_clip"]))
            # separate signals: text needs 0.30, vision needs 0.23
            ok_text = text_s >= MIN_SCORE
            ok_clip = clip_s >= CLIP_MIN_SCORE
            if not (ok_text or ok_clip):
                continue
            # best score + which signal won (for adaptive cutoff below)
            if ok_clip and clip_s >= text_s:
                scored.append((clip_s, name, rec, "clip"))
            else:
                scored.append((text_s, name, rec, "text"))

        scored.sort(reverse=True, key=lambda t: t[0])
        # Adaptive cutoff for vision: when a photo matches strongly, drop the
        # weak tail. (Fixed bars fail: "beach" scores 0.25 while random photos
        # score 0.23 — one bar either misses beach or lets in junk.)
        clip_scores = [s for s, _, _, k in scored if k == "clip"]
        if clip_scores:
            clip_cutoff = max(CLIP_MIN_SCORE, max(clip_scores) * 0.85)
            scored = [t for t in scored
                      if t[3] == "text" or t[0] >= clip_cutoff]
        results = []
        for score, name, rec, _ in scored[:top_k]:
            results.append({
                "name": name,
                "size": rec.get("size", 0),
                "mtime": int(rec.get("mtime", 0)),
                "score": round(score, 3),
                "image": is_image(name),
            })
        return {"ok": True, "results": results,
                "message": "none" if not results else "ok"}
    except Exception as e:  # never break the server
        msg = str(e)
        if "onnxruntime" in msg or "tokenizers" in msg \
                or "No module named" in msg:
            return {"ok": False, "results": [], "message": "needs_setup"}
        return {"ok": False, "results": [], "message": "error",
                "error": msg[:200]}


def ocr_image(directory, name):
    """Transcribe one image's text for the chat box. Never raises."""
    try:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            return {"ok": False, "text": "", "message": "needs_setup"}
        safe = re.sub(r"[^\w.\-() ]", "", name).strip()
        fp = os.path.join(directory, safe)
        if not os.path.isfile(fp) or not is_image(safe):
            return {"ok": False, "text": "", "message": "not_found"}
        try:
            text = _ocr_text_strict(fp)
        except Exception as e:
            return {"ok": False, "text": "", "message": "error",
                    "note": str(e)[:200]}
        if not text:
            return {"ok": True, "text": "",
                    "message": "blank",
                    "note": "No readable text found in this image."}
        return {"ok": True, "text": text, "message": "ok"}
    except Exception as e:
        msg = str(e)
        if "rapidocr" in msg or "No module named" in msg:
            return {"ok": False, "text": "", "message": "needs_setup"}
        return {"ok": False, "text": "", "message": "error"}


def index_status(directory, data_dir):
    """Light status for progress UI."""
    index = _load_index(data_dir)
    try:
        names = [n for n in os.listdir(directory)
                 if os.path.isfile(os.path.join(directory, n))]
    except OSError:
        names = []
    files = index.get("files", {})
    pending = sum(1 for n in names if n not in files)
    st = {"indexed": len(files), "files": len(names), "pending": pending,
          "model_ready": model_ready(), "clip_ready": clip_ready()}
    st.update(component_status())
    return st


def ai_eligible(directory, data_dir, name):
    """Fast check for the Shared Files AI button: True if the file has
    transcribable text. Uses the index only (no on-demand OCR); documents
    not yet indexed get a quick text peek."""
    try:
        index = _load_index(data_dir)
        rec = index.get("files", {}).get(name)
        if rec:
            return bool(rec.get("has_ocr") or rec.get("has_text"))
        if is_image(name):
            return False
        t = extract_text(os.path.join(directory, name))
        return bool(t.strip())
    except Exception:
        return False


def file_has_text(directory, data_dir, name):
    """True if the file has transcribable text (document text or image OCR).
    Used to decide whether the AI button shows for a file."""
    index = _load_index(data_dir)
    rec = index.get("files", {}).get(name)
    if rec:
        if rec.get("has_ocr") or rec.get("has_text"):
            return True
        # indexed but flagged empty → no button
        if "has_ocr" in rec or "has_text" in rec:
            return False
    # not indexed yet: documents are eligible, images need an OCR check
    if not is_image(name):
        try:
            t = extract_text(os.path.join(directory, name))
            return bool(t.strip())
        except Exception:
            return False
    try:
        t = extract_text(os.path.join(directory, name))
        return bool(t.strip())
    except Exception:
        return False


def summarize_file(directory, data_dir, name, max_sentences=5):
    """Extractive summary: top sentences by word-frequency score.
    Retrieval-based, not generative. Never raises."""
    try:
        index = _load_index(data_dir)
        fp = os.path.join(directory, name)
        if not os.path.isfile(fp):
            return {"ok": False, "text": "", "message": "not_found"}
        text = extract_text(fp)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return {"ok": False, "text": "", "message": "no_text"}
        sents = re.split(r"(?<=[.!?])\s+", text)
        sents = [s.strip() for s in sents if len(s.strip()) > 20]
        if len(sents) <= max_sentences:
            return {"ok": True, "text": text, "message": "ok"}
        words = re.findall(r"[a-z0-9']+", text.lower())
        freq = {}
        for w in words:
            if len(w) > 3:
                freq[w] = freq.get(w, 0) + 1
        scored = []
        for i, s in enumerate(sents):
            sw = re.findall(r"[a-z0-9']+", s.lower())
            score = sum(freq.get(w, 0) for w in sw) / max(len(sw), 1)
            # slight preference for early sentences
            score *= 1.0 / (1 + i * 0.05)
            scored.append((score, i, s))
        scored.sort(reverse=True)
        top = sorted(scored[:max_sentences], key=lambda t: t[1])
        return {"ok": True, "text": " ".join(s for _, _, s in top),
                "message": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "message": "error",
                "detail": str(e)[:200]}
