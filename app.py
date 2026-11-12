import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
import uuid
import zipfile
import gclassfetch_ui
from urllib.parse import parse_qs, unquote, urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow localhost insecure transport for local OAuth flow
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from flask import Flask, jsonify, redirect, render_template, render_template_string, request, send_file
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
UI_BASE = Path(gclassfetch_ui.__file__).parent.resolve()
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
    "issues": [],
    "linked_files": 0,
    "run_id": "",
    "destination": "",
    "report_ready": False,
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
    temporary = CONFIG_FILE.with_name(CONFIG_FILE.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(temporary, CONFIG_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_dest(dest):
    dest = str(dest or "").strip().strip('"')
    if not dest:
        dest = str(Path.home() / "Downloads" / "Classroom")
    return dest


def extract_drive_ref(url):
    parsed = urlparse(html.unescape(str(url or "")).rstrip(".,;)>]}"))
    query = parse_qs(parsed.query)
    host = (parsed.hostname or "").lower()
    if host in {"www.google.com", "google.com"} and parsed.path == "/url":
        return extract_drive_ref((query.get("q") or query.get("url") or [""])[0])
    if host not in {"drive.google.com", "docs.google.com"}:
        return None, None
    path = unquote(parsed.path)
    if host == "docs.google.com" and (path.startswith("/forms/") or "/d/e/" in path):
        return None, None
    match = re.search(r"/(?:folders|d)/([\w-]+)", path)
    fid = match.group(1) if match else (query.get("id") or [None])[0]
    if fid and re.fullmatch(r"[\w-]+", fid):
        return fid, "folder" if "/folders/" in path or "folderview" in path else "file"
    return None, None


def file_digest(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hide_metadata(path):
    """Keep the downloader's bookkeeping out of Windows Explorer's normal view."""
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
        attributes = kernel.GetFileAttributesW(str(path))
        if attributes != -1:
            kernel.SetFileAttributesW(str(path), attributes | 2)


def flatten_course_folder(course_dir):
    """Move legacy attachments into their course folder without overwriting files.

    Back up merged text and manifests, and journal every attachment move. Only
    empty directories are removed; distinct attachments always keep their bytes.
    """
    requested = Path(course_dir)
    if requested.is_symlink() or getattr(requested, "is_junction", lambda: False)():
        raise OSError("A linked course folder needs to be organized separately.")
    course = requested.resolve()
    if not course.is_dir():
        return 0
    nested = []
    directories = []
    for root, children, filenames in os.walk(course, followlinks=False):
        for child in children[:]:
            path = Path(root) / child
            if child.startswith("."):
                children.remove(child)
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise OSError("A linked folder needs to be organized separately.")
            if not path.resolve().is_relative_to(course):
                raise OSError("A folder points outside this course.")
            directories.append(path)
        for filename in filenames:
            path = Path(root) / filename
            if path.is_symlink() or not path.resolve().is_relative_to(course):
                raise OSError("A file points outside this course.")
            if path.parent != course:
                if filename.endswith(".part"):
                    raise OSError("An unfinished download is present. Finish it before organizing this course.")
                nested.append(path)
    if not directories:
        return 0

    manifests = {}
    tracked = set()
    for path in [course / ".gclassfetch.json"] + [p for p in nested if p.name == ".gclassfetch.json"]:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise OSError("The saved file record is damaged. Keep it for recovery before organizing this course.")
        manifests[path] = data
        for entry in data.values():
            name = entry.get("filename", "")
            if name and Path(name).name == name:
                tracked.add(path.parent / name)

    text_names = {"description.txt": "Classroom notes.txt",
                  "announcements.txt": "Classroom announcements.txt",
                  "links.txt": "Classroom links.txt"}
    text_files = [p for p in nested if p.name in text_names and p not in tracked]
    other_files = [p for p in nested if p not in text_files and p.name != ".gclassfetch.json"]
    used = {p.name.casefold() for p in course.iterdir()} | {name.casefold() for name in text_names.values()}
    mapping = {}
    moves = []
    for source in sorted(other_files):
        name = source.name
        counter = 1
        while name.casefold() in used:
            name = f"{source.stem} ({counter}){source.suffix}"
            counter += 1
        target = course / name
        # Both absolute endpoints are checked before any file move.
        if not source.resolve().is_relative_to(course) or target.resolve().parent != course:
            raise OSError("A file move would leave this course folder.")
        used.add(name.casefold())
        mapping[source] = target
        moves.append({"from": source.relative_to(course).as_posix(), "to": name,
                      "sha256": file_digest(source)})

    backup_root = course.parent / ".gclassfetch-backups"
    backup_root.mkdir(exist_ok=True)
    hide_metadata(backup_root)
    backup = backup_root / (safe_name(course.name, 60) + "-" + uuid.uuid4().hex[:10])
    backup.mkdir()
    journal = backup / "moves.json"
    journal.write_text(json.dumps({"course": str(course), "moves": moves}, indent=2), encoding="utf-8")
    originals = set(text_files) | set(manifests)
    originals.update(course / name for name in set(text_names.values()) if (course / name).is_file())
    archive_path = backup / "original-text.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(originals):
            archive.write(path, path.relative_to(course).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        for path in originals:
            if archive.read(path.relative_to(course).as_posix()) != path.read_bytes():
                raise OSError("The layout backup could not be verified.")

    # Merge descriptions and announcements into one readable file per course.
    for source in text_files:
        target = course / text_names[source.name]
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        text = source.read_text(encoding="utf-8").strip()
        if text and text not in existing:
            target.write_text(existing.rstrip() + ("\n\n" if existing else "") + text + "\n", encoding="utf-8")
        source.unlink()  # Verified original is kept in the backup ZIP.
    for source, target in mapping.items():
        os.rename(source, target)
    for move in moves:
        if file_digest(course / move["to"]) != move["sha256"]:
            raise OSError("A moved file could not be verified. See the layout backup journal.")

    merged = {}
    for path, data in manifests.items():
        for fid, entry in data.items():
            source = path.parent / entry.get("filename", "")
            target = mapping.get(source, source)
            if target.parent != course or not target.is_file():
                continue
            entry = {**entry, "filename": target.name}
            previous = merged.get(fid)
            if previous is None or str(entry.get("version")) >= str(previous.get("version")):
                merged[fid] = entry
    if merged:
        manifest = course / ".gclassfetch.json"
        manifest.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        hide_metadata(manifest)
    for path in manifests:
        if path.parent != course:
            path.unlink()  # Original is preserved in the verified backup ZIP.
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()  # Never recursively delete a directory or its contents.
        except OSError:
            pass
    log(f"Simplified {course.name}: {len(moves)} files moved into the course folder", "ok")
    return len(moves)


def explain_error(error):
    if isinstance(error, HttpError):
        status = error.resp.status
        detail = (error.content or b"").decode("utf-8", "ignore").lower()
        if "exportsizelimitexceeded" in detail:
            return "This document exceeds Google's export limit. Open the original to download it manually."
        if status == 404:
            return "Google could not find this item, or this account no longer has access. Check the original link."
        if status == 401:
            return "Your Google sign-in has expired. Sign in again and retry this course."
        if status == 403:
            if "quota" in detail or "ratelimit" in detail:
                return "Google's request limit was reached. Wait a little, then retry this course."
            return "Google did not allow access or downloading. Check the original with your college account."
        if status in RETRY_CODES:
            return "Google is temporarily unavailable. Retry this course in a few minutes."
    return str(error) or type(error).__name__


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


def paginate(method, result_key, request_headers=None, cancel=True, **kwargs):
    items, token = [], None
    while not cancel or not STOP.is_set():
        params = dict(kwargs)
        if token:
            params["pageToken"] = token
        def execute():
            req = method(**params)
            req.headers.update(request_headers or {})
            return req.execute(num_retries=2)
        resp = call(execute)
        items += resp.get(result_key, []) or []
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def generate_offline_index(dest_dir):
    """Write a standalone library; its CSS and scripts work without the server."""
    try:
        dest = Path(dest_dir)
        courses = []
        total = 0
        for course_dir in sorted(dest.iterdir()):
            if not course_dir.is_dir() or course_dir.name.startswith("."):
                continue
            files = []
            # Also read older nested layouts so existing libraries stay usable.
            for root, directories, filenames in os.walk(course_dir):
                directories[:] = sorted(d for d in directories if not d.startswith("."))
                for filename in sorted(filenames):
                    if filename.startswith(".") or filename.endswith((".part", ".tmp")) or filename == "index.html":
                        continue
                    path = Path(root) / filename
                    files.append({"name": filename,
                                  "url": quote(path.relative_to(dest).as_posix(), safe="/"),
                                  "ext": path.suffix.lower().lstrip(".") or "file"})
            if files:
                files.sort(key=lambda file: file["name"].casefold())
                courses.append({"name": course_dir.name, "files": files, "count": len(files)})
                total += len(files)
        with app.app_context():
            page = render_template("library.html", courses=courses, total=total,
                                   css=(UI_BASE / "static" / "app.css").read_text(encoding="utf-8"))
        (dest / "index.html").write_text(page, encoding="utf-8")
        log("Generated searchable index.html library", "ok")
    except Exception as exc:
        raise OSError(f"Could not create offline library: {exc}") from exc


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


METADATA_FIELDS = "id,name,mimeType,size,webViewLink,shortcutDetails,resourceKey,modifiedTime,md5Checksum,capabilities(canDownload)"


def batch_metadata(api, ids, resource_keys=None, on_error=None):
    out = {}
    resource_keys = resource_keys or {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]

        def cb(request_id, response, exception):
            if exception is None and response:
                out[response["id"]] = response

        batch = api.drive.new_batch_http_request(callback=cb)
        for fid in chunk:
            req = api.drive.files().get(
                    fileId=fid,
                    fields=METADATA_FIELDS,
                    supportsAllDrives=True,
                )
            if resource_keys.get(fid):
                req.headers["X-Goog-Drive-Resource-Keys"] = f"{fid}/{resource_keys[fid]}"
            batch.add(req, request_id=fid)
        try:
            call(lambda b=batch: b.execute())
        except Exception:
            # The individual requests below recover from a batch-level failure.
            pass
        # A batch succeeds even when individual requests fail. Retry these through
        # the regular backoff path, then surface every unresolved reference.
        for fid in chunk:
            if fid in out:
                continue
            try:
                def fetch(fid=fid):
                    req = api.drive.files().get(fileId=fid, fields=METADATA_FIELDS, supportsAllDrives=True)
                    if resource_keys.get(fid):
                        req.headers["X-Goog-Drive-Resource-Keys"] = f"{fid}/{resource_keys[fid]}"
                    return req.execute(num_retries=2)
                out[fid] = call(fetch)
            except Exception as exc:
                if on_error:
                    on_error(fid, exc)
    return out


class DownloadFailed(Exception):
    def __init__(self, cause, written):
        super().__init__(str(cause))
        self.cause = cause
        self.written = written




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
        self.resource_keys = {}
        self.file_lock = threading.RLock()
        self.reserved_paths = set()
        self.report = []
        self.course_paths = {}

    def headers(self, fid):
        with self.file_lock:
            key = self.resource_keys.get(fid)
        return {"X-Goog-Drive-Resource-Keys": f"{fid}/{key}"} if key else {}

    def issue(self, cid, title, reason, url="", kind="attention"):
        if kind == "link" and not self.options.get("save_links", True):
            reason += " Link saving is switched off; use Open original here."
        row = {"cid": cid, "title": str(title), "reason": str(reason), "url": url, "kind": kind}
        with state_lock:
            if row in state["issues"]:
                return
            state["issues"].append(row)
            c = state["courses"].get(cid)
            if c:
                c["issues"] = c.get("issues", 0) + (kind != "link")
            if kind == "link":
                state["linked_files"] += 1
        log(f"{title}: {reason}", "dim" if kind == "link" else "err")

    def record_result(self, item, status, path="", reason=""):
        with self.file_lock:
            self.report.append({"course_id": item["cid"], "file_id": item["fid"],
                                "name": item["meta"].get("name", item["title"]),
                                "status": status, "path": str(path), "reason": reason})

    def reserve_file(self, item, filename):
        directory = item["dir"]
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / ".gclassfetch.json"
        with self.file_lock:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
            meta = item["meta"]
            version = [meta.get("modifiedTime"), meta.get("md5Checksum"), meta.get("size"), meta.get("mimeType")]
            entry = manifest.get(item["fid"], {})
            known_name = entry.get("filename", "")
            known = directory / known_name
            if known_name and Path(known_name).name == known_name and known.is_file() and entry.get("version") == version:
                if file_digest(known) == entry.get("sha256"):
                    return known, None, version
            candidate = directory / filename
            # Existing files from older versions are reusable only with Google's
            # actual content checksum, never merely their name or byte count.
            if candidate.is_file() and meta.get("md5Checksum") and file_digest(candidate, "md5") == meta["md5Checksum"]:
                self.commit_file(item, candidate, version)
                return candidate, None, version
            number = 0
            while candidate.exists() or str(candidate).casefold() in self.reserved_paths:
                number += 1
                p = Path(filename)
                candidate = directory / f"{p.stem} ({number}){p.suffix}"
            self.reserved_paths.add(str(candidate).casefold())
            temporary = candidate.with_name(candidate.name + "." + uuid.uuid4().hex + ".part")
            return candidate, open(temporary, "xb"), version

    def commit_file(self, item, target, version):
        with self.file_lock:
            path = item["dir"] / ".gclassfetch.json"
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
            manifest[item["fid"]] = {"filename": target.name, "version": version, "sha256": file_digest(target)}
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(temp, path)
            hide_metadata(path)

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
                    c["status"] = "done" if c.get("failed", 0) == 0 and not c.get("issues") else "done-warnings"
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
            path = dest_dir / "Classroom links.txt"
            line = f"[{kind.upper()}] {title} -> {url}\n"
            with self.file_lock:
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                if line not in existing:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(line)
        except Exception as e:
            raise OSError(f"Could not save link '{title}': {e}") from e

    def record_announcement(self, dest_dir, item):
        text = (item.get("text") or "").strip()
        if not text:
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath = dest_dir / "Classroom announcements.txt"
            date = item.get("creationTime", "")[:10]
            block = f"=== {date} ===\n{text}\n\n"
            existing = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
            if block not in existing:
                with open(filepath, "a", encoding="utf-8") as fh:
                    fh.write(block)
        except Exception as exc:
            raise OSError(f"Could not save announcement: {exc}") from exc

    def record_notes_text(self, dest_dir, title, text):
        text = str(text or "").strip()
        if not text:
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath = dest_dir / "Classroom notes.txt"
            block = f"=== {title} ===\n{text}\n\n"
            existing = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
            if block not in existing:
                with open(filepath, "a", encoding="utf-8") as fh:
                    fh.write(block)
        except Exception as exc:
            raise OSError(f"Could not save post description: {exc}") from exc

    def list_folder(self, folder_id, cid="", title="Shared folder"):
        try:
            return paginate(
                self.api.drive.files().list,
                "files",
                q=f"'{folder_id}' in parents and trashed=false",
                fields=f"nextPageToken,files({METADATA_FIELDS})",
                request_headers=self.headers(folder_id),
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        except Exception as e:
            self.issue(cid, title, f"Could not list folder. {explain_error(e)}", f"https://drive.google.com/drive/folders/{folder_id}")
            return []

    def scan_course(self, course, base_dir):
        cid = course["id"]
        name = course.get("name", cid)
        classroom = self.api.classroom

        with state_lock:
            if cid in state["courses"]:
                state["courses"][cid]["status"] = "scanning"

        section_part = f" - {course['section']}" if course.get("section") else ""
        cname = self.course_paths.get(cid, safe_name(name + section_part))
        cdir = base_dir / cname

        try:
            flatten_course_folder(cdir)
        except Exception as exc:
            self.issue(cid, f"{name}: existing folders", f"Could not simplify the old folders. {explain_error(exc)}")

        refs = []
        links = []
        pending_folders = []

        def add_url(url, d, title, kind="link"):
            url = html.unescape(str(url or "")).rstrip(".,;)>]}")
            fid, ref_type = extract_drive_ref(url)
            if fid:
                query = parse_qs(urlparse(url).query)
                key = (query.get("resourcekey") or query.get("resourceKey") or [None])[0]
                if key:
                    with self.file_lock:
                        self.resource_keys[fid] = key
                if ref_type == "folder":
                    pending_folders.append((fid, d, title))
                else:
                    refs.append((fid, d, title))
            elif url:
                self.issue(cid, title, "Online resource. Saved as a link; no offline copy.", url, "link")
            links.append((d, title, url, kind))

        def add_materials(mats, d):
            for m in mats or []:
                if "driveFile" in m:
                    df = m["driveFile"].get("driveFile") or m["driveFile"]
                    fid = df.get("id")
                    title = df.get("title") or df.get("name") or "Drive attachment"
                    if fid:
                        refs.append((fid, d, title))
                    if fid:
                        add_url(df.get("alternateLink") or f"https://drive.google.com/open?id={fid}", d, title)
                elif "link" in m:
                    lk = m["link"]
                    url = lk.get("url", "")
                    title = lk.get("title", "link")
                    add_url(url, d, title)
                elif "youtubeVideo" in m:
                    y = m["youtubeVideo"]
                    url = y.get("alternateLink") or y.get("url", "")
                    title = y.get("title", "video")
                    add_url(url or f"https://www.youtube.com/watch?v={y.get('id', '')}", d, title, "youtube")
                elif "form" in m:
                    f = m["form"]
                    url = f.get("formUrl") or f.get("responseUrl", "")
                    title = f.get("title", "Google Form")
                    add_url(url, d, title, "form")

        # 1. Scan Coursework Materials (Lecture Notes, Slides & Handouts)
        if self.options.get("include_materials", True):
            add_materials(course.get("materials"), cdir)
            try:
                for item in paginate(
                    classroom.courses().courseWorkMaterials().list,
                    "courseWorkMaterial",
                    courseId=cid,
                    pageSize=50,
                ):
                    title = item.get("title", "material")
                    d = cdir
                    add_materials(item.get("materials"), d)
                    desc = item.get("description", "") or ""
                    if desc:
                        self.record_notes_text(d, title, desc)
                        for url in re.findall(r"https?://\S+", desc):
                            add_url(url, d, title, "desc")
            except Exception as e:
                self.issue(cid, f"{name}: materials", f"Scan incomplete. {explain_error(e)}", course.get("alternateLink", ""))

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
                    d = cdir
                    add_materials(item.get("materials"), d)
                    desc = item.get("description", "") or ""
                    if desc:
                        self.record_notes_text(d, title, desc)
                        for url in re.findall(r"https?://\S+", desc):
                            add_url(url, d, title, "desc")
            except Exception as e:
                self.issue(cid, f"{name}: assignments", f"Scan incomplete. {explain_error(e)}", course.get("alternateLink", ""))

        # 3. Scan Announcements
        if self.options.get("include_announcements", True):
            try:
                for item in paginate(
                    classroom.courses().announcements().list,
                    "announcements",
                    courseId=cid,
                    pageSize=50,
                ):
                    d = cdir
                    add_materials(item.get("materials"), d)
                    self.record_announcement(d, item)
                    ann_text = item.get("text", "") or ""
                    if ann_text:
                        for url in re.findall(r"https?://\S+", ann_text):
                            add_url(url, d, "Announcement link", "announcement")
            except Exception as e:
                self.issue(cid, f"{name}: announcements", f"Scan incomplete. {explain_error(e)}", course.get("alternateLink", ""))

        for d, title, url, kind in links:
            self.write_link(d, title, url, kind)

        files = {}
        meta_cache = {}
        pending = [(fid, d, title, frozenset()) for fid, d, title in refs + pending_folders]
        seen = set()
        failed_meta = {}
        while pending and not STOP.is_set():
            ids = [fid for fid, _, _, _ in pending if fid not in meta_cache and fid not in failed_meta]
            if ids:
                meta_cache.update(batch_metadata(self.api, ids, self.resource_keys, lambda fid, exc: failed_meta.update({fid: exc})))
            nxt = []
            for fid, d, title, ancestors in pending:
                if fid in ancestors:
                    self.issue(cid, title, "Folder or shortcut points back to an ancestor. Repeated traversal stopped.", f"https://drive.google.com/open?id={fid}")
                    continue
                key = (fid, str(d).casefold())
                if key in seen:
                    continue
                seen.add(key)
                meta = meta_cache.get(fid)
                if not meta:
                    self.issue(cid, title or d.name, explain_error(failed_meta.get(fid, RuntimeError('File information is unavailable.'))), f"https://drive.google.com/open?id={fid}")
                    continue
                if meta.get("resourceKey"):
                    with self.file_lock:
                        self.resource_keys[fid] = meta["resourceKey"]

                if meta.get("mimeType") == "application/vnd.google-apps.shortcut":
                    shortcut = meta.get("shortcutDetails") or {}
                    target_id = shortcut.get("targetId")
                    if target_id:
                        if shortcut.get("targetResourceKey"):
                            with self.file_lock:
                                self.resource_keys[target_id] = shortcut["targetResourceKey"]
                        nxt.append((target_id, d, title or meta.get("name", ""), ancestors | {fid}))
                    else:
                        self.issue(cid, title or fid, "Shortcut has no accessible target.", meta.get("webViewLink", ""))
                    continue

                if meta.get("mimeType") == "application/vnd.google-apps.folder":
                    for ch in self.list_folder(fid, cid, meta.get("name", title)):
                        meta_cache[ch["id"]] = ch
                        nxt.append((ch["id"], cdir, ch.get("name", ""), ancestors | {fid}))
                else:
                    files[key] = {"fid": fid, "dir": d, "title": title, "meta": meta}
            pending = nxt

        items = []
        total_bytes = 0
        export_enabled = self.options.get("export_docs", True)

        for info in files.values():
            fid = info["fid"]
            meta = info["meta"]
            mime = meta.get("mimeType", "")
            url = meta.get("webViewLink") or f"https://drive.google.com/open?id={fid}"
            if meta.get("capabilities", {}).get("canDownload") is False:
                self.write_link(info["dir"], meta.get("name", fid), url, "restricted")
                self.issue(cid, meta.get("name", fid), "The owner has disabled downloading. Open the original or ask for access.", url)
                continue
            if mime in LINK_ONLY_MIMES or (mime.startswith("application/vnd.google-apps") and mime not in EXPORT_MIMES):
                self.write_link(
                    info["dir"],
                    meta.get("name") or info["title"],
                    meta.get("webViewLink", ""),
                    "gfile",
                )
                self.issue(cid, meta.get("name", fid), "This Google file type is available online. No offline export is supported.", url, "link")
                continue
            if mime in EXPORT_MIMES and not export_enabled:
                self.write_link(
                    info["dir"],
                    meta.get("name") or info["title"],
                    meta.get("webViewLink", ""),
                    "gdoc",
                )
                self.issue(cid, meta.get("name", fid), "Document export is switched off in download settings.", url, "link")
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
                c["status"] = "stopped" if STOP.is_set() else ("downloading" if items else ("done-warnings" if c.get("issues") else "done"))
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
            self.record_result(item, "stopped")
            return
        fid = item["fid"]
        dest_dir = item["dir"]
        title = item["title"]
        meta = item["meta"]
        cid = item["cid"]
        mime = meta.get("mimeType", "")
        raw_name = str(meta.get("name") or title or fid)
        suffix = Path(raw_name).suffix
        name = safe_name(Path(raw_name).stem, 90) + safe_name(suffix, 16) if suffix else safe_name(raw_name, 100)
        actual = 0
        saved = False

        try:
            if mime in EXPORT_MIMES:
                ext, export_mime = EXPORT_MIMES[mime]
                expected = 0
                filename = f"{name}.{ext}" if not name.lower().endswith(f".{ext}") else name
                def factory():
                    req = self.api.drive.files().export_media(fileId=fid, mimeType=export_mime)
                    req.headers.update(self.headers(fid))
                    return req
            else:
                filename = name
                expected = int(meta.get("size") or 0)
                def factory():
                    req = self.api.drive.files().get_media(fileId=fid, supportsAllDrives=True)
                    req.headers.update(self.headers(fid))
                    return req

            target, fh, version = self.reserve_file(item, filename)
            if fh is None:
                self.finish(cid, target.stat().st_size, skipped=True, name=target.name)
                self.record_result(item, "already_saved", target)
                return

            with state_lock:
                state["current_course"] = state["courses"].get(cid, {}).get("name", "")
                state["current_file"] = filename

            ok = False
            try:
                actual = self.transfer(factory, fh)
                fh.close()
                if mime not in EXPORT_MIMES:
                    if "size" in meta and Path(fh.name).stat().st_size != expected:
                        raise OSError("Downloaded size does not match the original. Run again to retry.")
                    if meta.get("md5Checksum") and file_digest(fh.name, "md5") != meta["md5Checksum"]:
                        raise OSError("Downloaded checksum does not match the original. Run again to retry.")
                os.replace(fh.name, target)
                ok = True
                saved = True
                self.commit_file(item, target, version)
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
            self.record_result(item, "saved", target)

        except DownloadFailed as e:
            self.bump_bytes(-e.written)
            if STOP.is_set():
                self.record_result(item, "stopped")
                return
            err_msg = explain_error(e.cause)
            self.finish(cid, 0, failed=True, name=title or fid, err=err_msg)
            self.issue(cid, meta.get("name") or title or fid, err_msg, meta.get("webViewLink") or f"https://drive.google.com/open?id={fid}")
            self.record_result(item, "failed", reason=err_msg)
        except Exception as e:
            if actual and not saved:
                self.bump_bytes(-actual)
            err_msg = explain_error(e)
            self.finish(cid, 0, failed=True, name=title or fid, err=err_msg)
            self.issue(cid, meta.get("name") or title or fid, err_msg, meta.get("webViewLink") or f"https://drive.google.com/open?id={fid}")
            self.record_result(item, "failed", reason=err_msg)

    def run(self, course_ids, dest_str, workers=None):
        dest = None
        try:
            dest = Path(normalize_dest(dest_str))
            dest.mkdir(parents=True, exist_ok=True)
            state["destination"] = str(dest)
            state["report_ready"] = False
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
                    self.issue("", "Course list", f"Could not list courses: {e}")
            else:
                for cid in course_ids:
                    try:
                        c_data = call(lambda c=cid: classroom.courses().get(id=c).execute(num_retries=2))
                        courses.append(c_data)
                    except Exception as e:
                        self.issue(cid, "Course unavailable", str(e), f"https://classroom.google.com/c/{cid}")

            if not courses:
                raise RuntimeError("No active subjects found to download.")

            used_names = set()
            for c in sorted(courses, key=lambda c: c["id"]):
                label = safe_name(c.get("name", c["id"]) + (f" - {c['section']}" if c.get("section") else ""))
                if label.casefold() in used_names:
                    label += f" [{c['id']}]"
                self.course_paths[c["id"]] = label
                used_names.add(label.casefold())

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
                        self.issue(c["id"], c.get("name", c["id"]), f"Course scan failed: {e}", c.get("alternateLink", ""))
                        with state_lock:
                            state["scan_done"] += 1
                        continue

                    with state_lock:
                        state["scan_done"] += 1
                    log(f"Scanned: '{c.get('name')}' ({len(items)} files)")
                    for item in items:
                        self.futures.append(self.dl_pool.submit(self.download_one, item))

            state["phase"] = "downloading"
            for future in as_completed(self.futures):
                future.result()
            warnings = any(i["kind"] != "link" for i in state["issues"])
            state["phase"] = "stopped" if STOP.is_set() else ("done-warnings" if warnings else "done")
            status_text = "Stopped by user" if STOP.is_set() else "Download complete"
            log(
                f"{status_text}: {state['done_files']} downloaded ({state['skipped_files']} skipped, {state['failed_files']} failed)",
                "info" if STOP.is_set() else "ok",
            )
            if dest and self.options.get("create_index", True) and not STOP.is_set():
                generate_offline_index(dest)
        except Exception as e:
            state["phase"] = "error"
            self.issue("", "Download could not finish", str(e))
        finally:
            self.dl_pool.shutdown(wait=True, cancel_futures=True)
            state["current_file"] = ""
            state["end_time"] = time.time()
            if STOP.is_set():
                for course in state["courses"].values():
                    if course["status"] in {"queued", "scanning", "downloading"}:
                        course["status"] = "stopped"
            if dest and dest.exists():
                try:
                    report = {"phase": state["phase"], "completed_at": state["end_time"], "options": self.options,
                              "courses": state["courses"], "files": self.report, "issues": state["issues"]}
                    (dest / "download-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                    state["report_ready"] = True
                except OSError as exc:
                    self.issue("", "Download report", f"Could not save report: {exc}")
                    if state["phase"] == "done":
                        state["phase"] = "done-warnings"
            state["running"] = False


app = Flask(__name__, template_folder=str(UI_BASE / "templates"), static_folder=str(UI_BASE / "static"))

# gclassfetch Editorial Light Cream UI
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

    return render_template("index.html", need_auth=need_auth)


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
            courseStates=["ACTIVE", "ARCHIVED"] if request.args.get("archived") == "true" else ["ACTIVE"],
            pageSize=50,
            cancel=False,
        )
        out = [
            {
                "id": c["id"],
                "name": c.get("name"),
                "section": c.get("section"),
                "alternateLink": c.get("alternateLink"),
                "courseState": c.get("courseState"),
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
            try:
                cfg["workers"] = max(1, min(16, int(body["workers"] or 8)))
            except (TypeError, ValueError):
                return jsonify({"error": "Choose a download speed between 1 and 16."}), 400
        if "options" in body and isinstance(body["options"], dict):
            if any(not isinstance(value, bool) for value in body["options"].values()):
                return jsonify({"error": "Download settings must be on or off."}), 400
            cfg["options"].update({key: value for key, value in body["options"].items() if key in cfg["options"]})
        try:
            save_config(cfg)
        except OSError:
            return jsonify({"error": "Could not save settings. Check that the app folder is writable."}), 500
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
    creds = get_current_credentials()
    if not creds:
        return jsonify({"status": "Authentication required. Please sign in with Google."}), 401

    body = request.get_json(silent=True) or {}
    ids = "all" if course_id == "all" else [c for c in course_id.split(",") if c]
    try:
        workers = max(1, min(16, int(body.get("workers") or load_config().get("workers") or 8)))
    except (TypeError, ValueError):
        return jsonify({"error": "Choose a download speed between 1 and 16."}), 400
    options = dict(load_config()["options"])
    if "options" in body:
        if not isinstance(body["options"], dict) or any(not isinstance(value, bool) for value in body["options"].values()):
            return jsonify({"error": "Download settings must be on or off."}), 400
        options.update({key: value for key, value in body["options"].items() if key in options})
    if ids != "all" and not ids:
        return jsonify({"error": "Choose at least one course."}), 400
    if not any(options.get(key) for key in ("include_materials", "include_coursework", "include_announcements")):
        return jsonify({"error": "Include notes, assignments, or announcements in download settings."}), 400

    with state_lock:
        if state["running"]:
            return jsonify({"status": "Download is already running."}), 409
        STOP.clear()
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
            "issues": [],
            "linked_files": 0,
            "run_id": uuid.uuid4().hex,
            "destination": normalize_dest(body.get("dest", "")),
            "report_ready": False,
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
            "issues": list(state["issues"]),
            "linked_files": state["linked_files"],
            "run_id": state["run_id"],
            "destination": state["destination"],
            "report_ready": state["report_ready"],
        }
    return jsonify(payload)


@app.route("/report")
def report_route():
    if state["running"]:
        return jsonify({"error": "The report will be ready when the download finishes."}), 409
    report = Path(normalize_dest(state["destination"] or load_config().get("dest"))) / "download-report.json"
    if not report.is_file():
        return jsonify({"error": "No report yet. Download courses first."}), 404
    return send_file(report, as_attachment=True, download_name="download-report.json")


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
