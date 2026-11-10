import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from pathlib import Path

# Allow localhost insecure transport for local OAuth flow
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from flask import Flask, jsonify, redirect, render_template_string, request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

BASE = Path(__file__).parent.resolve()
CONFIG_FILE = BASE / "config.json"
CREDENTIALS_FILE = BASE / "credentials.json"
TOKEN_FILE = BASE / "token.json"
DEFAULT_PORT = 5328

EXPORT_MIMES = {
    "application/vnd.google-apps.document": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "application/vnd.google-apps.presentation": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "application/vnd.google-apps.drawing": ("pdf", "application/pdf"),
    "application/vnd.google-apps.jam": ("pdf", "application/pdf"),
}

LINK_ONLY_MIMES = {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.map",
}

RETRY_CODES = {429, 500, 502, 503, 504}

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

DRIVE_FOLDER_RE = re.compile(
    r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|folderview\?id=)([a-zA-Z0-9_-]+)"
)
DRIVE_FILE_RE = re.compile(
    r"(?:drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)|docs\.google\.com/(?:document|presentation|spreadsheets|drawings)/d/)([a-zA-Z0-9_-]+)"
)

state = {
    "running": False,
    "phase": "idle",
    "log": [],
    "log_seq": 0,
    "workers": 8,
    "start_time": None,
    "end_time": None,
    "total_files": 0,
    "done_files": 0,
    "skipped_files": 0,
    "failed_files": 0,
    "total_bytes": 0,
    "done_bytes": 0,
    "scan_total": 0,
    "scan_done": 0,
    "current_course": "",
    "current_file": "",
    "courses": {},
    "options": {
        "include_materials": True,
        "include_coursework": True,
        "include_announcements": True,
        "save_links": True,
        "export_docs": True,
        "create_index": True,
    },
}
state_lock = threading.RLock()
log_lock = threading.Lock()
STOP = threading.Event()


def log(msg, kind="info"):
    with log_lock:
        state["log_seq"] += 1
        state["log"].append({
            "seq": state["log_seq"],
            "kind": kind,
            "msg": f"[{time.strftime('%H:%M:%S')}] {msg}",
        })
        if len(state["log"]) > 1000:
            state["log"] = state["log"][-1000:]


def load_config():
    cfg = {
        "dest": "",
        "workers": 8,
        "options": {
            "include_materials": True,
            "include_coursework": True,
            "include_announcements": True,
            "save_links": True,
            "export_docs": True,
            "create_index": True,
        },
    }
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "dest" in data:
                cfg["dest"] = data["dest"]
            if "workers" in data:
                cfg["workers"] = data["workers"]
            if "options" in data and isinstance(data["options"], dict):
                cfg["options"].update(data["options"])
    except Exception:
        pass
    if not cfg["dest"]:
        try:
            dest_file = BASE / "dest.json"
            if dest_file.exists():
                cfg["dest"] = json.loads(dest_file.read_text(encoding="utf-8")).get("dest", "")
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def normalize_dest(dest):
    dest = str(dest or "").strip().strip('"')
    if not dest:
        dest = str(Path.home() / "Downloads" / "Classroom")
    return dest


def extract_drive_ref(url):
    if not url or "google.com" not in str(url):
        return None, None
    m_folder = DRIVE_FOLDER_RE.search(url)
    if m_folder:
        return m_folder.group(1), "folder"
    m_file = DRIVE_FILE_RE.search(url)
    if m_file:
        return m_file.group(1), "file"
    return None, None


def get_current_credentials():
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            creds = None

    if creds and creds.valid and creds.has_scopes(SCOPES):
        return creds
    return None


def safe_name(name, maxlen=100):
    name = str(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name:
        return "untitled"
    stem = name.split(".")[0].upper()
    if stem in WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:maxlen]


def is_retryable(e):
    if not isinstance(e, HttpError):
        return isinstance(e, (ConnectionError, TimeoutError, OSError))
    if e.resp.status in RETRY_CODES:
        return True
    if e.resp.status == 403:
        body = (e.content or b"").decode("utf-8", "ignore").lower()
        return "ratelimit" in body or "rate limit" in body or "quota" in body
    return False


def call(fn, tries=4, delay=0.6):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if attempt < tries - 1 and is_retryable(e):
                time.sleep(delay * (2 ** attempt))
                continue
            raise


def paginate(method, result_key, **kwargs):
    items, token = [], None
    while not STOP.is_set():
        params = dict(kwargs)
        if token:
            params["pageToken"] = token
        resp = call(lambda p=params: method(**p).execute(num_retries=2))
        items += resp.get(result_key, []) or []
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def generate_offline_index(dest_dir):
    try:
        dest_path = Path(dest_dir)
        if not dest_path.exists():
            return
        courses = []
        for cdir in sorted(dest_path.iterdir()):
            if not cdir.is_dir() or cdir.name.startswith("."):
                continue
            topics = []
            for tdir in sorted(cdir.iterdir()):
                if not tdir.is_dir():
                    continue
                files = []
                for root, _, filenames in os.walk(tdir):
                    rel_root = Path(root).relative_to(dest_path)
                    for fn in sorted(filenames):
                        if fn.endswith(".part") or fn == "index.html":
                            continue
                        rel_path = rel_root / fn
                        files.append({
                            "name": fn,
                            "path": str(rel_path).replace("\\", "/"),
                            "ext": Path(fn).suffix.lower().lstrip("."),
                        })
                if files:
                    topics.append({"name": tdir.name, "files": files})
            if topics:
                courses.append({"name": cdir.name, "topics": topics})

        index_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>gclassfetch - Offline Library</title>
  <style>
    :root {{
      --bg: #faf7f2; --card: #ffffff; --border: #e8e3da;
      --text: #1f1d1a; --text-muted: #78716c; --accent: #26211c;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); padding: 40px 20px;
    }}
    .wrap {{ max-width: 900px; margin: 0 auto; }}
    header {{ margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 18px; }}
    h1 {{ font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 4px; }}
    p.sub {{ color: var(--text-muted); font-size: 14px; }}
    input.search {{
      width: 100%; padding: 11px 15px; border: 1px solid var(--border); border-radius: 8px;
      font-size: 14px; background: #fff; margin-bottom: 20px; outline: none;
    }}
    input.search:focus {{ border-color: var(--accent); }}
    .course {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px; padding: 18px 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.03); }}
    .course h2 {{ font-size: 17px; font-weight: 700; margin-bottom: 12px; }}
    .topic {{ margin-bottom: 14px; padding-left: 12px; border-left: 2px solid #e7e2d8; }}
    .topic h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 8px; }}
    .file-list {{ list-style: none; display: flex; flex-direction: column; gap: 6px; }}
    .file-item a {{
      color: var(--text); text-decoration: none; font-size: 13.5px; display: inline-flex;
      align-items: center; gap: 8px; padding: 4px 8px; border-radius: 6px;
    }}
    .file-item a:hover {{ background: #f3efe6; text-decoration: underline; }}
    .tag {{ font-size: 10px; font-weight: 700; text-transform: uppercase; background: #ede8df; padding: 2px 6px; border-radius: 4px; color: #57534e; }}
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>gclassfetch Library</h1>
    <p class="sub">Downloaded course materials, lecture notes, and assignments.</p>
  </header>
  <input type="text" class="search" id="q" placeholder="Filter files..." oninput="filter()">
  <div id="content">
"""
        for c in courses:
            index_html += f'<div class="course"><h2>{c["name"]}</h2>'
            for t in c["topics"]:
                index_html += f'<div class="topic"><h3>{t["name"]}</h3><ul class="file-list">'
                for f in t["files"]:
                    ext = f["ext"] or "file"
                    index_html += f'<li class="file-item"><a href="{f["path"]}" target="_blank"><span class="tag">{ext}</span> {f["name"]}</a></li>'
                index_html += '</ul></div>'
            index_html += '</div>'

        index_html += """
  </div>
</div>
<script>
function filter() {
  const q = document.getElementById('q').value.toLowerCase();
  document.querySelectorAll('.file-item').forEach(el => {
    const text = el.textContent.toLowerCase();
    el.style.display = text.includes(q) ? '' : 'none';
  });
}
</script>
</body>
</html>"""
        (dest_path / "index.html").write_text(index_html, encoding="utf-8")
        log("Generated index.html library", "ok")
    except Exception as e:
        log(f"Notice: Could not write offline index: {e}", "dim")


class Api:
    def __init__(self, creds):
        self.creds = creds
        self._local = threading.local()

    def _service(self, name, version):
        if not hasattr(self._local, name):
            setattr(
                self._local,
                name,
                build(name, version, credentials=self.creds, cache_discovery=False),
            )
        return getattr(self._local, name)

    @property
    def drive(self):
        return self._service("drive", "v3")

    @property
    def classroom(self):
        return self._service("classroom", "v1")


def batch_metadata(api, ids):
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]

        def cb(request_id, response, exception):
            if exception is None and response:
                out[response["id"]] = response
            elif exception:
                pass

        batch = api.drive.new_batch_http_request(callback=cb)
        for fid in chunk:
            batch.add(
                api.drive.files().get(
                    fileId=fid,
                    fields="id,name,mimeType,size,webViewLink,shortcutDetails",
                    supportsAllDrives=True,
                )
            )
        call(lambda b=batch: b.execute())
    return out


class DownloadFailed(Exception):
    def __init__(self, cause, written):
        super().__init__(str(cause))
        self.cause = cause
        self.written = written


def reserve_target(dest_dir, filename, expected):
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = Path(filename)
    stem, suffix = p.stem, p.suffix
    i = 0
    while True:
        cand = dest_dir / (filename if i == 0 else f"{stem} ({i}){suffix}")
        if cand.exists():
            try:
                size = cand.stat().st_size
                if (expected and size == expected) or (not expected and size > 0):
                    return cand, None
            except OSError:
                pass
            i += 1
            if i > 500:
                raise RuntimeError("too many duplicates")
            continue
        try:
            fh = open(cand.with_name(cand.name + ".part"), "xb")
            return cand, fh
        except FileExistsError:
            try:
                os.remove(cand.with_name(cand.name + ".part"))
            except OSError:
                i += 1
                if i > 500:
                    raise RuntimeError("too many duplicates")
                continue
            try:
                fh = open(cand.with_name(cand.name + ".part"), "xb")
                return cand, fh
            except FileExistsError:
                i += 1
                if i > 500:
                    raise RuntimeError("too many duplicates")


class Downloader:
    def __init__(self, creds, workers=8, options=None):
        self.api = Api(creds)
        self.workers = max(1, min(16, int(workers or 8)))
        self.options = options or {
            "include_materials": True,
            "include_coursework": True,
            "include_announcements": True,
            "save_links": True,
            "export_docs": True,
            "create_index": True,
        }
        self.dl_pool = ThreadPoolExecutor(max_workers=self.workers)
        self.futures = []

    def bump_bytes(self, n):
        with state_lock:
            state["done_bytes"] = max(0, state["done_bytes"] + n)
            if state["done_bytes"] > state["total_bytes"]:
                state["total_bytes"] = state["done_bytes"]

    def finish(self, cid, nbytes=0, skipped=False, failed=False, name="", err=""):
        with state_lock:
            c = state["courses"].get(cid)
            if skipped:
                state["done_files"] += 1
                state["skipped_files"] += 1
                state["done_bytes"] += max(0, nbytes)
                if state["done_bytes"] > state["total_bytes"]:
                    state["total_bytes"] = state["done_bytes"]
                if c:
                    c["done"] += 1
                    c["skipped"] = c.get("skipped", 0) + 1
            elif failed:
                state["failed_files"] += 1
                if c:
                    c["failed"] = c.get("failed", 0) + 1
            else:
                state["done_files"] += 1
                if c:
                    c["done"] += 1

            if c:
                completed_count = c["done"] + c.get("failed", 0)
                if completed_count >= c["total"]:
                    c["status"] = "done" if c.get("failed", 0) == 0 else "done-warnings"
                elif c["status"] in ("queued", "scanning"):
                    c["status"] = "downloading"

        if skipped:
            log(f"Already exists, skipped: {name}", "dim")
        elif failed:
            log(f"Failed: {name} ({err})", "err")
        elif name:
            log(f"Saved: {name}", "ok")

    def write_link(self, dest_dir, title, url, kind):
        if not url or not self.options.get("save_links", True):
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            with open(dest_dir / "links.txt", "a", encoding="utf-8") as fh:
                fh.write(f"[{kind.upper()}] {title} -> {url}\n")
        except Exception as e:
            log(f"Could not record link '{title}': {e}", "dim")

    def record_announcement(self, dest_dir, item):
        text = (item.get("text") or "").strip()
        if not text:
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath = dest_dir / "announcements.txt"
            date = item.get("creationTime", "")[:10]
            with open(filepath, "a", encoding="utf-8") as fh:
                fh.write(f"=== {date} ===\n{text}\n\n")
        except Exception:
            pass

    def record_notes_text(self, dest_dir, title, text):
        text = str(text or "").strip()
        if not text:
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath = dest_dir / "description.txt"
            with open(filepath, "a", encoding="utf-8") as fh:
                fh.write(f"=== {title} ===\n{text}\n\n")
        except Exception:
            pass

    def list_folder(self, folder_id):
        try:
            return paginate(
                self.api.drive.files().list,
                "files",
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType)",
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        except Exception as e:
            log(f"Folder listing failed: {e}", "dim")
            return []

    def scan_course(self, course, base_dir):
        cid = course["id"]
        name = course.get("name", cid)
        classroom = self.api.classroom

        with state_lock:
            if cid in state["courses"]:
                state["courses"][cid]["status"] = "scanning"

        section_part = f" - {course['section']}" if course.get("section") else ""
        cname = safe_name(name + section_part)
        cdir = base_dir / cname

        topics = {}
        try:
            for t in paginate(classroom.courses().topics().list, "topic", courseId=cid):
                topics[t["topicId"]] = t.get("name", "General / No Topic")
        except Exception as e:
            log(f"Topic notice for '{name}': {e}", "dim")

        def tdir(topic_id, sub):
            tname = safe_name(topics.get(topic_id, "General / No Topic"))
            subname = safe_name(sub)
            return cdir / tname / subname

        refs = []
        links = []
        pending_folders = []

        def add_materials(mats, d):
            for m in mats or []:
                if "driveFile" in m:
                    df = m["driveFile"].get("driveFile") or m["driveFile"]
                    fid = df.get("id")
                    title = df.get("title", "")
                    if fid:
                        refs.append((fid, d, title))
                elif "link" in m:
                    lk = m["link"]
                    url = lk.get("url", "")
                    title = lk.get("title", "link")
                    drive_id, drive_type = extract_drive_ref(url)
                    if drive_id:
                        if drive_type == "folder":
                            pending_folders.append((drive_id, d, title))
                        else:
                            refs.append((drive_id, d, title))
                    links.append((d, title, url, "link"))
                elif "youtubeVideo" in m:
                    y = m["youtubeVideo"]
                    url = y.get("alternateLink") or y.get("url", "")
                    title = y.get("title", "video")
                    links.append((d, title, url, "youtube"))
                elif "form" in m:
                    f = m["form"]
                    url = f.get("formUrl") or f.get("responseUrl", "")
                    title = f.get("title", "Google Form")
                    links.append((d, title, url, "form"))

        # 1. Scan Coursework Materials (Lecture Notes, Slides & Handouts)
        if self.options.get("include_materials", True):
            add_materials(course.get("materials"), cdir / "_course_materials")
            try:
                for item in paginate(
                    classroom.courses().courseWorkMaterials().list,
                    "courseWorkMaterial",
                    courseId=cid,
                    pageSize=50,
                ):
                    title = item.get("title", "material")
                    d = tdir(item.get("topicId"), title)
                    add_materials(item.get("materials"), d)
                    desc = item.get("description", "") or ""
                    if desc:
                        self.record_notes_text(d, title, desc)
                        for url in re.findall(r"https?://\S+", desc):
                            drive_id, drive_type = extract_drive_ref(url)
                            if drive_id:
                                if drive_type == "folder":
                                    pending_folders.append((drive_id, d, f"{title} (Drive Folder)"))
                                else:
                                    refs.append((drive_id, d, f"{title} (Drive File)"))
                            links.append((d, f"{title} (notes link)", url, "desc"))
            except Exception as e:
                log(f"Notice: Could not fetch materials for '{name}' ({e})", "err")

        # 2. Scan Coursework (Assignments & Questions)
        if self.options.get("include_coursework", True):
            try:
                for item in paginate(
                    classroom.courses().courseWork().list,
                    "courseWork",
                    courseId=cid,
                    pageSize=50,
                ):
                    title = item.get("title", "assignment")
                    d = tdir(item.get("topicId"), title)
                    add_materials(item.get("materials"), d)
                    desc = item.get("description", "") or ""
                    if desc:
                        self.record_notes_text(d, title, desc)
                        for url in re.findall(r"https?://\S+", desc):
                            drive_id, drive_type = extract_drive_ref(url)
                            if drive_id:
                                if drive_type == "folder":
                                    pending_folders.append((drive_id, d, f"{title} (Drive Folder)"))
                                else:
                                    refs.append((drive_id, d, f"{title} (Drive File)"))
                            links.append((d, f"{title} (assignment link)", url, "desc"))
            except Exception as e:
                log(f"Notice: Could not fetch coursework for '{name}' ({e})", "err")

        # 3. Scan Announcements
        if self.options.get("include_announcements", True):
            try:
                for item in paginate(
                    classroom.courses().announcements().list,
                    "announcements",
                    courseId=cid,
                    pageSize=50,
                ):
                    d = cdir / "_announcements"
                    add_materials(item.get("materials"), d)
                    self.record_announcement(d, item)
                    ann_text = item.get("text", "") or ""
                    if ann_text:
                        for url in re.findall(r"https?://\S+", ann_text):
                            drive_id, drive_type = extract_drive_ref(url)
                            if drive_id:
                                if drive_type == "folder":
                                    pending_folders.append((drive_id, d, "Announcement Drive Folder"))
                                else:
                                    refs.append((drive_id, d, "Announcement Drive File"))
                            links.append((d, "Announcement link", url, "announcement"))
            except Exception as e:
                log(f"Notice: Could not fetch announcements for '{name}' ({e})", "dim")

        for d, title, url, kind in links:
            self.write_link(d, title, url, kind)

        files = {}
        meta_cache = {}
        pending = list(refs)
        depth = 0

        for fold_id, fold_dir, fold_title in pending_folders:
            sub = fold_dir / safe_name(fold_title or "Shared Folder")
            for ch in self.list_folder(fold_id):
                pending.append((ch["id"], sub, ch.get("name", "")))

        while pending and depth < 8 and not STOP.is_set():
            ids = [fid for fid, _, _ in pending if fid not in meta_cache]
            if ids:
                meta_cache.update(batch_metadata(self.api, ids))
            nxt = []
            for fid, d, title in pending:
                meta = meta_cache.get(fid)
                if not meta:
                    continue

                if meta.get("mimeType") == "application/vnd.google-apps.shortcut":
                    shortcut = meta.get("shortcutDetails") or {}
                    target_id = shortcut.get("targetId")
                    if target_id:
                        nxt.append((target_id, d, title or meta.get("name", "")))
                    continue

                if meta.get("mimeType") == "application/vnd.google-apps.folder":
                    sub = d / safe_name(meta.get("name") or title or "folder")
                    for ch in self.list_folder(fid):
                        nxt.append((ch["id"], sub, ch.get("name", "")))
                elif fid not in files:
                    files[fid] = {"dir": d, "title": title, "meta": meta}
            pending, depth = nxt, depth + 1

        items = []
        total_bytes = 0
        export_enabled = self.options.get("export_docs", True)

        for fid, info in files.items():
            meta = info["meta"]
            mime = meta.get("mimeType", "")
            if mime in LINK_ONLY_MIMES or (mime.startswith("application/vnd.google-apps") and mime not in EXPORT_MIMES):
                self.write_link(
                    info["dir"],
                    meta.get("name") or info["title"],
                    meta.get("webViewLink", ""),
                    "gfile",
                )
                continue
            if mime in EXPORT_MIMES and not export_enabled:
                self.write_link(
                    info["dir"],
                    meta.get("name") or info["title"],
                    meta.get("webViewLink", ""),
                    "gdoc",
                )
                continue

            size = int(meta.get("size") or 0)
            total_bytes += size
            items.append({
                "fid": fid,
                "dir": info["dir"],
                "title": info["title"],
                "meta": meta,
                "cid": cid,
            })

        with state_lock:
            c = state["courses"].get(cid)
            if c:
                c["total"] = len(items)
                c["status"] = "downloading" if items else "done"
            state["total_files"] += len(items)
            state["total_bytes"] += total_bytes
        return items

    def transfer(self, factory, fh):
        written = 0
        tries = 4
        for attempt in range(tries):
            if STOP.is_set():
                raise DownloadFailed(RuntimeError("Download stopped by user"), written)
            attempt_bytes = 0
            try:
                fh.seek(0)
                fh.truncate()
                dl = MediaIoBaseDownload(fh, factory(), chunksize=8 * 1024 * 1024)
                prev, done = 0, False
                while not done:
                    if STOP.is_set():
                        raise RuntimeError("Download stopped by user")
                    st, done = dl.next_chunk()
                    prog = int(st.resumable_progress or 0)
                    delta = prog - prev
                    prev = prog
                    if delta > 0:
                        attempt_bytes += delta
                        self.bump_bytes(delta)
                written += attempt_bytes
                return written
            except Exception as e:
                self.bump_bytes(-attempt_bytes)
                if attempt < tries - 1 and is_retryable(e):
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise DownloadFailed(e, written)

    def download_one(self, item):
        if STOP.is_set():
            return
        fid = item["fid"]
        dest_dir = item["dir"]
        title = item["title"]
        meta = item["meta"]
        cid = item["cid"]
        mime = meta.get("mimeType", "")
        name = safe_name(meta.get("name") or title or fid)

        try:
            if mime in EXPORT_MIMES:
                ext, export_mime = EXPORT_MIMES[mime]
                filename = f"{name}.{ext}"
                expected = 0
                factory = lambda: self.api.drive.files().export_media(fileId=fid, mimeType=export_mime)
            else:
                filename = name
                expected = int(meta.get("size") or 0)
                factory = lambda: self.api.drive.files().get_media(fileId=fid, supportsAllDrives=True)

            target, fh = reserve_target(dest_dir, filename, expected)
            if fh is None:
                self.finish(cid, expected, skipped=True, name=filename)
                return

            with state_lock:
                state["current_course"] = state["courses"].get(cid, {}).get("name", "")
                state["current_file"] = filename

            ok = False
            try:
                actual = self.transfer(factory, fh)
                fh.close()
                os.replace(fh.name, target)
                ok = True
            finally:
                if not ok:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    try:
                        os.remove(fh.name)
                    except OSError:
                        pass

            self.finish(cid, 0, name=filename)

        except DownloadFailed as e:
            self.bump_bytes(-e.written)
            err_msg = str(e.cause) or e.cause.__class__.__name__
            self.finish(cid, 0, failed=True, name=title or fid, err=err_msg)
        except Exception as e:
            err_msg = str(e) or e.__class__.__name__
            self.finish(cid, 0, failed=True, name=title or fid, err=err_msg)

    def run(self, course_ids, dest_str, workers=None):
        dest = None
        try:
            dest = Path(normalize_dest(dest_str))
            dest.mkdir(parents=True, exist_ok=True)
            save_config({
                "dest": str(dest),
                "workers": self.workers,
                "options": self.options,
            })
            log(f"Destination: {dest} ({self.workers} workers)")

            classroom = self.api.classroom
            courses = []
            if course_ids == "all":
                try:
                    courses = paginate(
                        classroom.courses().list,
                        "courses",
                        courseStates=["ACTIVE"],
                        pageSize=50,
                    )
                except Exception as e:
                    log(f"Could not list subjects: {e}", "err")
            else:
                for cid in course_ids:
                    try:
                        c_data = call(lambda c=cid: classroom.courses().get(id=c).execute(num_retries=2))
                        courses.append(c_data)
                    except Exception as e:
                        log(f"Subject {cid} inaccessible: {e}", "err")

            if not courses:
                raise RuntimeError("No active subjects found to download.")

            with state_lock:
                state["scan_total"] = len(courses)
                state["phase"] = "scanning"
                for c in courses:
                    state["courses"][c["id"]] = {
                        "name": c.get("name", c["id"]),
                        "section": c.get("section"),
                        "status": "queued",
                        "total": 0,
                        "done": 0,
                        "skipped": 0,
                        "failed": 0,
                    }

            log(f"Scanning {len(courses)} course(s)...")

            with ThreadPoolExecutor(max_workers=min(6, max(2, self.workers))) as scan_pool:
                fut_map = {scan_pool.submit(self.scan_course, c, dest): c for c in courses}
                for fut in as_completed(fut_map):
                    c = fut_map[fut]
                    try:
                        items = fut.result()
                    except Exception as e:
                        with state_lock:
                            if c["id"] in state["courses"]:
                                state["courses"][c["id"]]["status"] = "error"
                        log(f"Scan failed for '{c.get('name')}': {e}", "err")
                        continue

                    with state_lock:
                        state["scan_done"] += 1
                        state["phase"] = "downloading"
                    log(f"Scanned: '{c.get('name')}' ({len(items)} files)")
                    for item in items:
                        self.futures.append(self.dl_pool.submit(self.download_one, item))

            wait(self.futures)
            state["phase"] = "stopped" if STOP.is_set() else "done"
            status_text = "Stopped by user" if STOP.is_set() else "Download complete"
            log(
                f"{status_text}: {state['done_files']} downloaded ({state['skipped_files']} skipped, {state['failed_files']} failed)",
                "info" if STOP.is_set() else "ok",
            )
            if dest and self.options.get("create_index", True) and not STOP.is_set():
                generate_offline_index(dest)
        except Exception as e:
            state["phase"] = "error"
            log(f"Error: {e}", "err")
        finally:
            state["running"] = False
            state["current_file"] = ""
            state["end_time"] = time.time()
            self.dl_pool.shutdown(wait=False, cancel_futures=True)


app = Flask(__name__)

# gclassfetch Editorial Light Cream UI
CREAM_UI_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>gclassfetch</title>
  <style>
    :root {
      --bg: #faf7f2;
      --bg-subtle: #f4efe6;
      --card-bg: #ffffff;
      --border: #e9e3d8;
      --border-hover: #d5cdbf;
      --text-main: #1f1d1a;
      --text-muted: #736c64;
      --text-light: #a69f95;
      --primary: #26211c;
      --primary-hover: #3d362e;
      --accent: #b45309;
      --accent-soft: #fef3c7;
      --success: #15803d;
      --danger: #b91c1c;
      --shadow-sm: 0 1px 3px rgba(35, 25, 15, 0.04);
      --shadow-md: 0 4px 14px rgba(35, 25, 15, 0.05);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text-main);
      min-height: 100vh;
      line-height: 1.5;
      padding-bottom: 80px;
      -webkit-font-smoothing: antialiased;
    }

    .wrap {
      max-width: 980px;
      margin: 0 auto;
      padding: 0 24px;
    }

    .nav {
      padding: 24px 0 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 32px;
    }
    .nav-inner {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-mark {
      width: 32px;
      height: 32px;
      background: var(--primary);
      border-radius: 9px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
    }
    .brand-mark svg { width: 18px; height: 18px; fill: currentColor; }
    .brand-title {
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-main);
    }
    .brand-pill {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      padding: 2px 8px;
      border-radius: 99px;
      margin-left: 6px;
    }
    .nav-links {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 8px 16px;
      font-size: 13.5px;
      font-weight: 600;
      border-radius: var(--radius-sm);
      border: 1px solid transparent;
      cursor: pointer;
      text-decoration: none;
      transition: all 0.15s ease;
      font-family: inherit;
    }
    .btn-primary {
      background: var(--primary);
      color: #ffffff;
    }
    .btn-primary:hover:not(:disabled) {
      background: var(--primary-hover);
      transform: translateY(-1px);
    }
    .btn-ghost {
      background: #ffffff;
      border-color: var(--border);
      color: var(--text-main);
      box-shadow: var(--shadow-sm);
    }
    .btn-ghost:hover:not(:disabled) {
      background: var(--bg-subtle);
      border-color: var(--border-hover);
    }
    .btn-danger {
      background: var(--danger);
      color: #ffffff;
    }
    .btn-sm {
      padding: 6px 12px;
      font-size: 12.5px;
    }
    button:disabled, .btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none !important;
    }

    .hero-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 26px 30px;
      margin-bottom: 24px;
      box-shadow: var(--shadow-sm);
    }
    .hero-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 12px;
      gap: 16px;
      flex-wrap: wrap;
    }
    .hero-title {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin-bottom: 4px;
    }
    .hero-desc {
      font-size: 13.5px;
      color: var(--text-muted);
    }
    .pct-tag {
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -0.03em;
      font-variant-numeric: tabular-nums;
      color: var(--primary);
    }

    .prog-track {
      height: 7px;
      background: var(--bg-subtle);
      border-radius: 99px;
      overflow: hidden;
      margin: 16px 0 14px;
      border: 1px solid rgba(0,0,0,0.03);
    }
    .prog-fill {
      height: 100%;
      width: 0%;
      background: var(--primary);
      border-radius: 99px;
      transition: width 0.3s ease;
    }
    .prog-fill.indet {
      width: 25% !important;
      animation: indet 1.4s infinite ease-in-out;
    }
    @keyframes indet { 0% { margin-left: -25%; } 100% { margin-left: 100%; } }

    .stats-row {
      display: flex;
      gap: 20px;
      flex-wrap: wrap;
      align-items: center;
      font-size: 12.5px;
      color: var(--text-muted);
    }
    .stats-row span b {
      color: var(--text-main);
      font-weight: 600;
    }
    .current-line {
      margin-top: 14px;
      padding: 8px 12px;
      background: var(--bg);
      border-radius: var(--radius-sm);
      font-size: 12.5px;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pulse-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
      flex-shrink: 0;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

    .settings-bar {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px 18px;
      margin-bottom: 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
      box-shadow: var(--shadow-sm);
    }
    .dest-group {
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 280px;
    }
    .dest-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .dest-input {
      flex: 1;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 7px 11px;
      font-size: 13px;
      color: var(--text-main);
      outline: none;
    }
    .dest-input:focus { border-color: var(--primary); }

    .dropdown-btn {
      font-size: 12.5px;
      color: var(--text-muted);
      background: transparent;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .dropdown-btn:hover { color: var(--text-main); }

    .options-drawer {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 16px 20px;
      margin-bottom: 24px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 14px;
      box-shadow: var(--shadow-sm);
    }
    .opt-label {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-main);
      cursor: pointer;
    }
    .opt-label input {
      accent-color: var(--primary);
      width: 15px;
      height: 15px;
    }

    .toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .tool-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .search-input {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 8px 14px;
      font-size: 13.5px;
      color: var(--text-main);
      width: 250px;
      outline: none;
      box-shadow: var(--shadow-sm);
    }
    .search-input:focus { border-color: var(--primary); }

    .courses-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .course-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 15px 20px;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      gap: 14px;
      transition: border-color 0.15s, box-shadow 0.15s;
      box-shadow: var(--shadow-sm);
    }
    .course-card:hover {
      border-color: var(--border-hover);
      box-shadow: var(--shadow-md);
    }
    .course-check {
      accent-color: var(--primary);
      width: 17px;
      height: 17px;
      cursor: pointer;
    }
    .course-main {
      flex: 1;
      min-width: 0;
    }
    .course-title-line {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 3px;
    }
    .course-title {
      font-size: 14.5px;
      font-weight: 600;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .sec-tag {
      font-size: 11px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 1px 7px;
      border-radius: 99px;
      flex-shrink: 0;
    }
    .course-sub {
      font-size: 12px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .course-bar {
      width: 70px;
      height: 4px;
      background: var(--bg-subtle);
      border-radius: 99px;
      overflow: hidden;
    }
    .course-bar-fill {
      height: 100%;
      width: 0%;
      background: var(--primary);
      border-radius: 99px;
    }

    .status-tag {
      font-size: 11px;
      font-weight: 600;
      padding: 3px 9px;
      border-radius: 99px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .status-tag.idle { background: var(--bg-subtle); color: var(--text-muted); }
    .status-tag.queued { background: #fef3c7; color: #92400e; }
    .status-tag.scanning { background: #e0f2fe; color: #0369a1; }
    .status-tag.downloading { background: #e0e7ff; color: #3730a3; }
    .status-tag.done { background: #dcfce7; color: #166534; }
    .status-tag.done-warnings { background: #fef3c7; color: #92400e; }
    .status-tag.error { background: #fee2e2; color: #991b1b; }

    .icon-link {
      color: var(--text-muted);
      padding: 4px;
      border-radius: 6px;
      display: inline-flex;
    }
    .icon-link:hover { color: var(--text-main); background: var(--bg-subtle); }
    .icon-link svg { width: 15px; height: 15px; fill: currentColor; }

    .log-strip {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      margin-top: 24px;
      box-shadow: var(--shadow-sm);
      overflow: hidden;
    }
    .log-header {
      padding: 12px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: pointer;
      user-select: none;
      font-size: 13px;
      font-weight: 600;
      background: var(--bg-subtle);
    }
    .log-box {
      padding: 12px 18px;
      height: 180px;
      overflow-y: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.6;
      color: var(--text-muted);
      background: #faf8f5;
      border-top: 1px solid var(--border);
    }
    .log-box div { word-break: break-all; }
    .log-box .ok { color: var(--success); }
    .log-box .err { color: var(--danger); font-weight: 600; }
    .log-box .dim { color: var(--text-light); }

    .hidden { display: none !important; }
    .skeleton {
      height: 48px;
      background: var(--bg-subtle);
      border-radius: var(--radius-md);
      margin-bottom: 8px;
      animation: pulse 1.2s infinite ease-in-out;
    }
  </style>
</head>
<body>

<div class="wrap">
  <nav class="nav">
    <div class="nav-inner">
      <div class="brand">
        <div class="brand-mark">
          <svg viewBox="0 0 24 24"><path d="M12 2L1 21h22L12 2zm0 3.5L20.5 19H3.5L12 5.5zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z"/></svg>
        </div>
        <div>
          <span class="brand-title">gclassfetch</span>
          <span class="brand-pill" id="connBadge">Ready</span>
        </div>
      </div>
      <div class="nav-links">
        <button class="btn btn-ghost btn-sm" onclick="openFolder()" title="Open destination directory">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
          Folder
        </button>
        <a class="btn btn-ghost btn-sm" href="/auth/login" title="Switch or re-authorize Google Account">
          Account
        </a>
      </div>
    </div>
  </nav>

  {% if need_auth %}
  <div class="hero-card" style="text-align: center; padding: 36px 20px;">
    <h2 class="hero-title" style="margin-bottom: 8px;">Google Classroom Sign-in Required</h2>
    <p class="hero-desc" style="max-width: 520px; margin: 0 auto 20px;">
      To download your lecture notes, study materials, and coursework, sign in with your Google account.
    </p>
    <a href="/auth/login" class="btn btn-primary" style="padding: 10px 24px; font-size: 14.5px;">
      Sign in with Google &rarr;
    </a>
  </div>
  {% endif %}

  <div class="hero-card">
    <div class="hero-header">
      <div>
        <div class="hero-title" id="phaseTitle">Ready to Download</div>
        <div class="hero-desc" id="phaseDesc">Select courses below to fetch notes, slides, and coursework.</div>
      </div>
      <div class="pct-tag" id="pctText">0%</div>
    </div>

    <div class="prog-track">
      <div class="prog-fill" id="progFill"></div>
    </div>

    <div class="stats-row">
      <span>Files: <b id="statFiles">0 / 0</b></span>
      <span>Size: <b id="statSize">0 B / 0 B</b></span>
      <span>Speed: <b id="statSpeed">0 B/s</b></span>
      <span>ETA: <b id="statEta">--</b></span>
      <span>Time: <b id="statTime">0:00</b></span>
    </div>

    <div class="current-line hidden" id="currentBox">
      <div class="pulse-dot"></div>
      <span style="font-weight:600;" id="currCourse"></span>
      <span style="color:var(--text-muted);">&rsaquo;</span>
      <span id="currFile" style="overflow:hidden;text-overflow:ellipsis;"></span>
    </div>
  </div>

  <div class="settings-bar">
    <div class="dest-group">
      <span class="dest-label">Destination</span>
      <input type="text" id="destInput" class="dest-input" placeholder="C:\Users\you\Downloads\Classroom" onchange="saveConfig()">
      <button class="btn btn-ghost btn-sm" onclick="selectFolderDialog()">Browse...</button>
    </div>
    <div style="display:flex;align-items:center;gap:10px;">
      <select id="workersSelect" style="background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:6px 10px;font-size:12.5px;" onchange="saveConfig()">
        <option value="4">4 Workers</option>
        <option value="8" selected>8 Workers (Fast)</option>
        <option value="12">12 Workers (Ultra)</option>
        <option value="16">16 Workers (Max)</option>
      </select>
      <button class="dropdown-btn" onclick="toggleDrawer()">Options &#9662;</button>
    </div>
  </div>

  <div class="options-drawer hidden" id="optDrawer">
    <label class="opt-label">
      <input type="checkbox" id="optMaterials" checked onchange="saveConfig()">
      <span>Lecture Notes & Materials</span>
    </label>
    <label class="opt-label">
      <input type="checkbox" id="optCoursework" checked onchange="saveConfig()">
      <span>Coursework & Assignments</span>
    </label>
    <label class="opt-label">
      <input type="checkbox" id="optAnnouncements" checked onchange="saveConfig()">
      <span>Announcements & Notices</span>
    </label>
    <label class="opt-label">
      <input type="checkbox" id="optExportDocs" checked onchange="saveConfig()">
      <span>Export Docs & Slides (Office/PDF)</span>
    </label>
    <label class="opt-label">
      <input type="checkbox" id="optSaveLinks" checked onchange="saveConfig()">
      <span>Save Links & Forms (links.txt)</span>
    </label>
    <label class="opt-label">
      <input type="checkbox" id="optCreateIndex" checked onchange="saveConfig()">
      <span>Generate Offline Library (index.html)</span>
    </label>
  </div>

  <div class="toolbar">
    <div class="tool-actions">
      <button class="btn btn-primary" id="btnAll" onclick="startDownload('all')">
        Fetch ALL Courses
      </button>
      <button class="btn btn-ghost" id="btnSel" onclick="startDownloadSelected()">
        Fetch Selected (<span id="selCount">0</span>)
      </button>
      <button class="btn btn-ghost btn-sm" onclick="selectAll()">Select All</button>
      <button class="btn btn-ghost btn-sm" onclick="deselectAll()">None</button>
      <button class="btn btn-danger btn-sm hidden" id="btnStop" onclick="stopDownload()">
        Stop
      </button>
    </div>
    <div>
      <input type="search" class="search-input" id="searchBox" placeholder="Filter courses..." oninput="filterCourses()">
    </div>
  </div>

  <div class="courses-header">
    <span>Enrolled Courses</span>
    <span id="courseSummary"></span>
  </div>

  <div id="courseList">
    <div class="skeleton"></div>
    <div class="skeleton"></div>
    <div class="skeleton"></div>
  </div>

  <div class="log-strip">
    <div class="log-header" onclick="toggleLog()">
      <span>Activity Log</span>
      <span id="logArrow" style="font-size:11px;color:var(--text-muted);">&#9662;</span>
    </div>
    <div class="log-box hidden" id="logBox"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let logSeq = 0, lastPolls = [], allCourses = [];

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}
function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}` : `${m}:${String(x).padStart(2,'0')}`;
}

function poll() {
  fetch('/status?since=' + logSeq)
    .then(r => r.json())
    .then(s => {
      render(s);
      if (s.reset) { $('logBox').innerHTML = ''; logSeq = 0; }
      (s.log || []).forEach(l => {
        if (l.seq <= logSeq) return;
        logSeq = l.seq;
        const line = document.createElement('div');
        if (l.kind !== 'info') line.className = l.kind;
        line.textContent = l.msg;
        $('logBox').appendChild(line);
      });
      while ($('logBox').childNodes.length > 400) $('logBox').removeChild($('logBox').firstChild);
      $('logBox').scrollTop = 1e9;
    });
}
setInterval(poll, 1000);
poll();

function render(s) {
  const isRunning = !!s.running;
  const phaseTitles = {
    idle: 'Ready to Download',
    scanning: 'Scanning Courses...',
    downloading: 'Downloading Files...',
    stopping: 'Stopping...',
    done: 'Download Complete',
    stopped: 'Download Stopped',
    error: 'Notice'
  };
  $('phaseTitle').textContent = phaseTitles[s.phase] || s.phase;
  $('connBadge').textContent = isRunning ? 'Active' : 'Ready';

  const now = Date.now() / 1000;
  lastPolls.push({ t: now, b: s.done_bytes || 0 });
  lastPolls = lastPolls.filter(p => now - p.t <= 5);
  let speed = 0;
  if (lastPolls.length > 1) {
    const a = lastPolls[0], z = lastPolls[lastPolls.length - 1], dt = z.t - a.t;
    if (dt > 0.4) speed = Math.max(0, (z.b - a.b) / dt);
  }

  const fileRatio = s.total_files > 0 ? (s.done_files / s.total_files) : 0;
  const byteRatio = s.total_bytes > 0 ? (s.done_bytes / s.total_bytes) : 0;
  let ratio = 0;
  if (s.phase === 'scanning' && s.scan_total > 0) {
    ratio = s.scan_done / s.scan_total;
  } else if (fileRatio && byteRatio) {
    ratio = (fileRatio + byteRatio) / 2;
  } else {
    ratio = Math.max(fileRatio, byteRatio);
  }
  const pct = Math.min(100, Math.round(ratio * 100));
  $('pctText').textContent = pct + '%';

  const pf = $('progFill');
  if (s.phase === 'scanning' && !s.total_files) {
    pf.className = 'prog-fill indet';
  } else {
    pf.className = 'prog-fill';
    pf.style.width = pct + '%';
  }

  $('statFiles').textContent = `${s.done_files || 0} / ${s.total_files || 0}`;
  $('statSize').textContent = `${fmtBytes(s.done_bytes || 0)} / ${fmtBytes(s.total_bytes || 0)}`;
  $('statSpeed').textContent = `${fmtBytes(speed)}/s`;
  const remain = Math.max(0, (s.total_bytes || 0) - (s.done_bytes || 0));
  $('statEta').textContent = (speed > 15000 && s.total_bytes) ? fmtTime(remain / speed) : '--';
  $('statTime').textContent = s.start_time ? fmtTime((s.end_time || now) - s.start_time) : '0:00';

  const cbox = $('currentBox');
  if (isRunning && (s.current_course || s.current_file)) {
    cbox.classList.remove('hidden');
    $('currCourse').textContent = s.current_course || '';
    $('currFile').textContent = s.current_file || '';
  } else {
    cbox.classList.add('hidden');
  }

  const courseStates = s.courses || {};
  document.querySelectorAll('.course-card').forEach(el => {
    const c = courseStates[el.dataset.cid];
    if (!c) return;
    const tag = el.querySelector('.status-tag');
    tag.textContent = c.status;
    tag.className = 'status-tag ' + c.status;
    el.querySelector('.c-count').textContent = c.total ? `${c.done}/${c.total}` : (c.status === 'scanning' ? '...' : '0');
    const fill = el.querySelector('.course-bar-fill');
    fill.style.width = c.total ? Math.min(100, Math.round(c.done / c.total * 100)) + '%' : '0%';
  });

  $('btnAll').disabled = isRunning;
  $('btnSel').disabled = isRunning;
  $('btnStop').classList.toggle('hidden', !isRunning);
  $('destInput').disabled = isRunning;
  $('workersSelect').disabled = isRunning;
}

function startDownload(ids) {
  if (ids !== 'all' && !ids.length) {
    alert('Please select at least one course.');
    return;
  }
  const payload = {
    dest: $('destInput').value,
    workers: Number($('workersSelect').value),
    options: {
      include_materials: $('optMaterials').checked,
      include_coursework: $('optCoursework').checked,
      include_announcements: $('optAnnouncements').checked,
      export_docs: $('optExportDocs').checked,
      save_links: $('optSaveLinks').checked,
      create_index: $('optCreateIndex').checked,
    }
  };
  fetch('/start/' + ids, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(res => {
    if (res.status === 'ok') {
      lastPolls = [];
      logSeq = 0;
      $('logBox').innerHTML = '';
      poll();
    } else {
      alert(res.status);
    }
  });
}

function startDownloadSelected() {
  const checked = [...document.querySelectorAll('.course-card input[type="checkbox"]:checked')].map(cb => cb.dataset.cid);
  startDownload(checked.join(','));
}

function stopDownload() {
  fetch('/stop', { method: 'POST' });
}

function selectAll() {
  document.querySelectorAll('.course-card:not(.hidden) input[type="checkbox"]').forEach(c => c.checked = true);
  updateCount();
}
function deselectAll() {
  document.querySelectorAll('.course-card input[type="checkbox"]').forEach(c => c.checked = false);
  updateCount();
}
function updateCount() {
  $('selCount').textContent = document.querySelectorAll('.course-card input[type="checkbox"]:checked').length;
}

function filterCourses() {
  const q = ($('searchBox').value || '').toLowerCase().trim();
  let cnt = 0;
  document.querySelectorAll('.course-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const sec = (card.dataset.section || '').toLowerCase();
    const match = !q || name.includes(q) || sec.includes(q);
    card.classList.toggle('hidden', !match);
    if (match) cnt++;
  });
  $('courseSummary').textContent = `${cnt} of ${allCourses.length} courses`;
}

function toggleDrawer() {
  $('optDrawer').classList.toggle('hidden');
}

function toggleLog() {
  const box = $('logBox');
  box.classList.toggle('hidden');
  $('logArrow').innerHTML = box.classList.contains('hidden') ? '&#9662;' : '&#9652;';
}

function openFolder() {
  fetch('/open-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: $('destInput').value })
  });
}

function selectFolderDialog() {
  fetch('/select-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current: $('destInput').value })
  })
  .then(r => r.json())
  .then(res => {
    if (res.status === 'ok' && res.path) {
      $('destInput').value = res.path;
      saveConfig();
    }
  });
}

function saveConfig() {
  const cfg = {
    dest: $('destInput').value,
    workers: Number($('workersSelect').value),
    options: {
      include_materials: $('optMaterials').checked,
      include_coursework: $('optCoursework').checked,
      include_announcements: $('optAnnouncements').checked,
      export_docs: $('optExportDocs').checked,
      save_links: $('optSaveLinks').checked,
      create_index: $('optCreateIndex').checked,
    }
  };
  fetch('/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg)
  });
}

fetch('/config')
  .then(r => r.json())
  .then(c => {
    $('destInput').value = c.dest || '';
    $('workersSelect').value = String(c.workers || 8);
    if (c.options) {
      if (c.options.include_materials !== undefined) $('optMaterials').checked = c.options.include_materials;
      if (c.options.include_coursework !== undefined) $('optCoursework').checked = c.options.include_coursework;
      if (c.options.include_announcements !== undefined) $('optAnnouncements').checked = c.options.include_announcements;
      if (c.options.export_docs !== undefined) $('optExportDocs').checked = c.options.export_docs;
      if (c.options.save_links !== undefined) $('optSaveLinks').checked = c.options.save_links;
      if (c.options.create_index !== undefined) $('optCreateIndex').checked = c.options.create_index;
    }
  });

fetch('/courses')
  .then(r => r.json())
  .then(cs => {
    const list = $('courseList');
    if (cs.auth_required) {
      list.innerHTML = '<div style="color:var(--text-muted);padding:24px;text-align:center;">Please click <b>Sign in with Google</b> above to connect.</div>';
      return;
    }
    list.innerHTML = '';
    if (!Array.isArray(cs) || !cs.length) {
      list.innerHTML = '<div style="color:var(--text-muted);padding:20px;text-align:center;">No active courses found.</div>';
      return;
    }
    allCourses = cs;
    $('courseSummary').textContent = `${cs.length} courses`;

    cs.forEach(c => {
      const card = document.createElement('div');
      card.className = 'course-card';
      card.dataset.cid = c.id;
      card.dataset.name = c.name || '';
      card.dataset.section = c.section || '';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'course-check';
      cb.dataset.cid = c.id;
      cb.addEventListener('change', updateCount);

      const main = document.createElement('div');
      main.className = 'course-main';

      const line = document.createElement('div');
      line.className = 'course-title-line';
      const nm = document.createElement('span');
      nm.className = 'course-title';
      nm.textContent = c.name || 'Untitled Course';
      line.appendChild(nm);

      if (c.section) {
        const sec = document.createElement('span');
        sec.className = 'sec-tag';
        sec.textContent = c.section;
        line.appendChild(sec);
      }

      const sub = document.createElement('div');
      sub.className = 'course-sub';
      const cnt = document.createElement('span');
      cnt.className = 'c-count';
      cnt.textContent = '0 files';
      const bar = document.createElement('div');
      bar.className = 'course-bar';
      const fill = document.createElement('div');
      fill.className = 'course-bar-fill';
      bar.appendChild(fill);

      sub.appendChild(cnt);
      sub.appendChild(bar);

      main.appendChild(line);
      main.appendChild(sub);

      const actions = document.createElement('div');
      actions.style.display = 'flex';
      actions.style.alignItems = 'center';
      actions.style.gap = '8px';

      if (c.alternateLink) {
        const a = document.createElement('a');
        a.className = 'icon-link';
        a.href = c.alternateLink;
        a.target = '_blank';
        a.title = 'Open in Google Classroom';
        a.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 19H5V5h7V3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>';
        actions.appendChild(a);
      }

      const tag = document.createElement('span');
      tag.className = 'status-tag idle';
      tag.textContent = 'idle';
      actions.appendChild(tag);

      card.appendChild(cb);
      card.appendChild(main);
      card.appendChild(actions);
      list.appendChild(card);
    });
    updateCount();
  });
</script>
</body>
</html>
"""

SETUP_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Setup - gclassfetch</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #faf7f2; color: #1f1d1a;
      display: flex; justify-content: center; align-items: center; min-height: 100vh;
      margin: 0; padding: 24px;
    }
    .box {
      background: #ffffff; border: 1px solid #e9e3d8; border-radius: 16px;
      padding: 36px 40px; max-width: 600px; box-shadow: 0 4px 20px rgba(35,25,15,0.06);
    }
    h1 { font-size: 22px; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.02em; }
    p { font-size: 14px; color: #736c64; line-height: 1.6; margin-bottom: 20px; }
    ol { margin-left: 20px; margin-bottom: 24px; font-size: 13.5px; line-height: 1.8; color: #44403c; }
    .btn {
      background: #26211c; color: #ffffff; border: none; padding: 11px 22px;
      font-size: 14px; font-weight: 600; border-radius: 8px; cursor: pointer; width: 100%;
    }
  </style>
</head>
<body>
  <div class="box">
    <h1>Connect Google Cloud Credentials</h1>
    <p>To connect directly to your Google Classroom classes, add your private desktop OAuth credentials.</p>
    <ol>
      <li>Go to <a href="https://console.cloud.google.com" target="_blank" style="color:#b45309">Google Cloud Console</a> and create a project.</li>
      <li>Enable <b>Google Classroom API</b> and <b>Google Drive API</b>.</li>
      <li>In OAuth Consent Screen: Add your email under <b>Test users</b>.</li>
      <li>In Credentials: Create an <b>OAuth Client ID (Desktop app)</b>.</li>
      <li>Download the JSON, rename it to <code>credentials.json</code>, and place it in this folder.</li>
    </ol>
    <button class="btn" onclick="location.reload()">I Added credentials.json - Continue</button>
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    code = request.args.get("code")
    if code:
        try:
            flow = Flow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                scopes=SCOPES,
                redirect_uri=f"http://localhost:{request.host.split(':')[-1]}/",
            )
            flow.fetch_token(code=code)
            creds = flow.credentials
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            return redirect("/")
        except Exception as e:
            return f"<h3>Authentication Notice: {e}</h3><p><a href='/auth/login'>Click here to try again</a></p>", 400

    if not CREDENTIALS_FILE.exists():
        return render_template_string(SETUP_PAGE)

    creds = get_current_credentials()
    need_auth = creds is None

    return render_template_string(CREAM_UI_PAGE, need_auth=need_auth)


@app.route("/auth/login")
def auth_login():
    if not CREDENTIALS_FILE.exists():
        return redirect("/")
    port = request.host.split(":")[-1]
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=f"http://localhost:{port}/",
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return redirect(auth_url)


@app.route("/courses")
def courses_route():
    creds = get_current_credentials()
    if not creds:
        return jsonify({"auth_required": True, "error": "Google authentication required"}), 401
    try:
        classroom = build("classroom", "v1", credentials=creds, cache_discovery=False)
        courses = paginate(
            classroom.courses().list,
            "courses",
            courseStates=["ACTIVE"],
            pageSize=50,
        )
        out = [
            {
                "id": c["id"],
                "name": c.get("name"),
                "section": c.get("section"),
                "alternateLink": c.get("alternateLink"),
            }
            for c in courses
        ]
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 401 if "401" in str(e) else 500


@app.route("/config", methods=["GET", "POST"])
def config_route():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        cfg = load_config()
        if "dest" in body:
            cfg["dest"] = str(body["dest"] or "")
        if "workers" in body:
            cfg["workers"] = max(1, min(16, int(body["workers"] or 8)))
        if "options" in body and isinstance(body["options"], dict):
            cfg["options"].update(body["options"])
        save_config(cfg)
        return jsonify({"status": "ok"})
    return jsonify(load_config())


@app.route("/select-folder", methods=["POST"])
def select_folder_route():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        current = (request.get_json(silent=True) or {}).get("current", "") or load_config().get("dest", "")
        folder = filedialog.askdirectory(
            initialdir=current or os.path.expanduser("~"),
            title="Select Download Destination Folder",
        )
        root.destroy()
        if folder:
            return jsonify({"status": "ok", "path": os.path.normpath(folder)})
        return jsonify({"status": "cancelled"})
    except Exception as e:
        return jsonify({"status": f"Folder dialog unavailable: {e}"})


@app.route("/start/<course_id>", methods=["POST"])
def start_route(course_id):
    if state["running"]:
        return jsonify({"status": "Download is already running."})
    creds = get_current_credentials()
    if not creds:
        return jsonify({"status": "Authentication required. Please sign in with Google."}), 401

    body = request.get_json(silent=True) or {}
    ids = "all" if course_id == "all" else [c for c in course_id.split(",") if c]
    workers = max(1, min(16, int(body.get("workers") or load_config().get("workers") or 8)))
    options = body.get("options") or load_config().get("options")

    STOP.clear()
    with state_lock:
        state.update({
            "running": True,
            "phase": "scanning",
            "start_time": time.time(),
            "end_time": None,
            "workers": workers,
            "options": options,
            "log": [],
            "log_seq": 0,
            "total_files": 0,
            "done_files": 0,
            "skipped_files": 0,
            "failed_files": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "scan_total": 0,
            "scan_done": 0,
            "current_course": "",
            "current_file": "",
            "courses": {},
        })

    threading.Thread(
        target=Downloader(creds, workers=workers, options=options).run,
        args=(ids, body.get("dest", "")),
        daemon=True,
    ).start()

    return jsonify({"status": "ok"})


@app.route("/stop", methods=["POST"])
def stop_route():
    if state["running"]:
        STOP.set()
        state["phase"] = "stopping"
        log("Stopping downloads per user request...")
    return jsonify({"status": "ok"})


@app.route("/reauth", methods=["GET", "POST"])
def reauth_route():
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
    except Exception:
        pass
    return redirect("/auth/login")


@app.route("/status")
def status_route():
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0

    with state_lock:
        reset = since > state["log_seq"]
        lines = state["log"] if reset else [l for l in state["log"] if l["seq"] > since]
        payload = {
            "running": state["running"],
            "phase": state["phase"],
            "start_time": state["start_time"],
            "end_time": state["end_time"],
            "total_files": state["total_files"],
            "done_files": state["done_files"],
            "skipped_files": state["skipped_files"],
            "failed_files": state["failed_files"],
            "total_bytes": state["total_bytes"],
            "done_bytes": state["done_bytes"],
            "scan_total": state["scan_total"],
            "scan_done": state["scan_done"],
            "current_course": state["current_course"],
            "current_file": state["current_file"],
            "courses": {cid: dict(c) for cid, c in state["courses"].items()},
            "log_seq": state["log_seq"],
            "log": list(lines),
            "reset": reset,
        }
    return jsonify(payload)


@app.route("/open-folder", methods=["POST"])
def open_folder_route():
    path = (request.get_json(silent=True) or {}).get("path") or load_config().get("dest")
    try:
        path = normalize_dest(path)
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": f"Could not open directory: {e}"})


def run_cli_mode(args):
    print("=" * 60)
    print("  gclassfetch - CLI Mode")
    print("=" * 60)
    creds = get_current_credentials()
    if not creds:
        print("[ERROR] No valid authentication token found.")
        print("Run 'python app.py' first to authorize your Google account in a browser.")
        sys.exit(1)

    dest = args.dest or load_config().get("dest") or str(Path.home() / "Downloads" / "Classroom")
    workers = args.workers or load_config().get("workers") or 8
    course_ids = "all" if args.all else (args.courses.split(",") if args.courses else "all")

    print(f"Destination: {dest}")
    print(f"Workers:     {workers}")
    print(f"Courses:     {course_ids}")
    print("Starting download...\n")

    dl = Downloader(creds, workers=workers)
    dl.run(course_ids, dest)


def main():
    parser = argparse.ArgumentParser(description="gclassfetch - Google Classroom Downloader")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to run local server on")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode instead of web server")
    parser.add_argument("--all", action="store_true", help="Download all enrolled courses (CLI mode)")
    parser.add_argument("--courses", type=str, help="Comma-separated course IDs (CLI mode)")
    parser.add_argument("--dest", type=str, help="Destination directory (CLI mode)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel worker threads")
    args = parser.parse_args()

    if args.cli:
        run_cli_mode(args)
        return

    port = args.port
    url = f"http://localhost:{port}"
    print("\n" + "=" * 60)
    print("  gclassfetch")
    print(f"  Dashboard available at: {url}")
    print("=" * 60 + "\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
