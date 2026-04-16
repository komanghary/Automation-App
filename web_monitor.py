"""
Coroco House Monitor — Web dashboard for all bots.
Run: python web_monitor.py
Access: http://localhost:8080
"""

import asyncio
import json
import os
import sys
from collections import deque
from datetime import datetime
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, Body, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MONITOR_PASSWORD = os.getenv("MONITOR_PASSWORD", "")
SHELL_PASSWORD   = os.getenv("SHELL_PASSWORD", "")
JEGAR_DIR  = os.path.join(os.path.dirname(BASE_DIR), "Jegar jegur")
DATA_DIR   = os.path.join(BASE_DIR, "data")
UI_FILE    = os.path.join(BASE_DIR, "monitor_ui.html")

app = FastAPI(title="Coroco House Monitor")

# ── Process registry ──────────────────────────────────────────────────────────

PROCESSES = {
    "bot": {
        "label": "Bot (Discord + YouTube)",
        "cmd": [sys.executable, os.path.join(BASE_DIR, "bot.py")],
        "cwd": BASE_DIR,
    },
    "watcher": {
        "label": "Watcher (Instagram)",
        "cmd": [sys.executable, os.path.join(BASE_DIR, "watcher_worker.py")],
        "cwd": BASE_DIR,
    },
    "dashboard": {
        "label": "Dashboard Worker",
        "cmd": [sys.executable, os.path.join(BASE_DIR, "dashboard_worker.py")],
        "cwd": BASE_DIR,
    },
    "trending": {
        "label": "Trending Scraper",
        "cmd": [sys.executable, os.path.join(JEGAR_DIR, "trending_scraper.py")],
        "cwd": JEGAR_DIR,
    },
    "pty": {
        "label": "PTY Server (Terminal + Claude)",
        "cmd": [sys.executable, os.path.join(BASE_DIR, "pty_server.py")],
        "cwd": BASE_DIR,
        "port": 8081,           # used for port-based alive detection
    },
    "books": {
        "label": "1% Perday Book Worker",
        "cmd": [sys.executable, os.path.join(BASE_DIR, "book_worker.py")],
        "cwd": BASE_DIR,
    },
}

class OrphanProcess:
    """Wraps an already-running process by PID (no stdout capture)."""
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None

    def terminate(self):
        self._send_signal(False)

    def kill(self):
        self._send_signal(True)

    def _send_signal(self, force: bool):
        import ctypes as _ct
        access = 0x1F0FFF if force else 0x0001
        h = _ct.windll.kernel32.OpenProcess(access, False, self.pid)
        if h:
            _ct.windll.kernel32.TerminateProcess(h, 1)
            _ct.windll.kernel32.CloseHandle(h)
        self.returncode = -1

    async def wait(self):
        import ctypes as _ct
        STILL_ACTIVE = 259
        while True:
            h = _ct.windll.kernel32.OpenProcess(0x0400, False, self.pid)
            if not h:
                break
            code = _ct.c_ulong(0)
            _ct.windll.kernel32.GetExitCodeProcess(h, _ct.byref(code))
            _ct.windll.kernel32.CloseHandle(h)
            if code.value != STILL_ACTIVE:
                self.returncode = code.value
                break
            await asyncio.sleep(2)
        self.returncode = self.returncode or 0


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running on Windows."""
    import ctypes as _ct
    STILL_ACTIVE = 259
    h = _ct.windll.kernel32.OpenProcess(0x0400, False, pid)
    if not h:
        return False
    code = _ct.c_ulong(0)
    _ct.windll.kernel32.GetExitCodeProcess(h, _ct.byref(code))
    _ct.windll.kernel32.CloseHandle(h)
    return code.value == STILL_ACTIVE


# Map process name → lock file path (relative to BASE_DIR)
LOCK_FILES: Dict[str, str] = {
    "bot":       os.path.join(BASE_DIR, "data", "bot.lock"),
    "watcher":   os.path.join(BASE_DIR, "data", "watcher.lock"),
    "dashboard": os.path.join(BASE_DIR, "data", "dashboard.lock"),
    "trending":  os.path.join(os.path.dirname(BASE_DIR), "Jegar jegur", "trending_scraper.lock"),
}


def _get_python_pids_by_script(script_name: str) -> list:
    """Return all PIDs of python.exe processes running a given script (PowerShell-based)."""
    import subprocess as _sp
    try:
        out = _sp.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json"],
            stderr=_sp.DEVNULL, text=True, timeout=8,
        )
        data = json.loads(out.strip())
        if isinstance(data, dict):
            data = [data]
        return [
            int(p["ProcessId"]) for p in data
            if script_name in (p.get("CommandLine") or "")
            and _pid_alive(int(p["ProcessId"]))
        ]
    except Exception:
        return []


def _get_pid_listening_on_port(port: int) -> Optional[int]:
    """Return PID listening on the given TCP port via netstat."""
    import subprocess as _sp
    try:
        out = _sp.check_output(["netstat", "-ano"], stderr=_sp.DEVNULL, text=True, timeout=5)
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                pid = int(line.strip().split()[-1])
                if _pid_alive(pid):
                    return pid
    except Exception:
        pass
    return None


def _scan_running_pids() -> Dict[str, int]:
    """Find already-running processes via lock files, netstat, and PowerShell."""
    found: Dict[str, int] = {}

    # 1. Lock-file based detection — also cleans stale files
    for name, lock_path in LOCK_FILES.items():
        if not os.path.exists(lock_path):
            continue
        try:
            with open(lock_path) as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                found[name] = pid
            else:
                os.remove(lock_path)   # clean stale lock
        except Exception:
            pass

    # 2. Port-based detection (pty_server on 8081)
    for name, cfg in PROCESSES.items():
        if name not in found and "port" in cfg:
            pid = _get_pid_listening_on_port(cfg["port"])
            if pid:
                found[name] = pid

    # 3. PowerShell-based detection for remaining processes
    ps_targets = {k: os.path.basename(v["cmd"][1]) for k, v in PROCESSES.items()
                  if k not in found and "port" not in v}
    if ps_targets:
        try:
            import subprocess as _sp
            out = _sp.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | "
                 "Select-Object ProcessId,CommandLine | ConvertTo-Json"],
                stderr=_sp.DEVNULL, text=True, timeout=8,
            )
            data = json.loads(out.strip())
            if isinstance(data, dict):
                data = [data]
            for name, script in ps_targets.items():
                for p in data:
                    if script in (p.get("CommandLine") or ""):
                        pid = int(p["ProcessId"])
                        if _pid_alive(pid):
                            found[name] = pid
                            break
        except Exception:
            pass

    return found


# Per-process state
_handles: Dict[str, Optional[asyncio.subprocess.Process]] = {k: None for k in PROCESSES}
_logs:    Dict[str, deque] = {k: deque(maxlen=600) for k in PROCESSES}
_status:  Dict[str, str]   = {k: "stopped" for k in PROCESSES}
_clients: Dict[str, Set[WebSocket]] = {k: set() for k in PROCESSES}


# ── Process lifecycle ─────────────────────────────────────────────────────────

async def _broadcast(name: str, line: str):
    dead = set()
    for ws in _clients[name]:
        try:
            await ws.send_text(line)
        except Exception:
            dead.add(ws)
    _clients[name] -= dead


LOG_DIR = os.path.join(BASE_DIR, "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

def _log_file(name: str) -> str:
    return os.path.join(LOG_DIR, f"{name}.log")


async def _read_stream(name: str, stream):
    log_path = _log_file(name)
    try:
        log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception:
        log_fh = None
    try:
        while True:
            try:
                raw = await stream.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                ts = datetime.now().strftime("%H:%M:%S")
                entry = f"[{ts}] {text}"
                _logs[name].append(entry)
                await _broadcast(name, entry + "\n")
                if log_fh:
                    log_fh.write(entry + "\n")
                    log_fh.flush()
            except Exception:
                break
    finally:
        if log_fh:
            log_fh.close()


async def _tail_log_file(name: str):
    """Tail an existing log file for orphan processes."""
    log_path = _log_file(name)
    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Load last 100 lines into buffer
            lines = f.readlines()[-100:]
            for line in lines:
                line = line.rstrip()
                if line:
                    _logs[name].append(line)
            # Now tail from current position
            while _status[name] == "running":
                line = f.readline()
                if line:
                    line = line.rstrip()
                    if line:
                        _logs[name].append(line)
                        await _broadcast(name, line + "\n")
                else:
                    await asyncio.sleep(0.5)
    except Exception:
        pass


async def _wait_proc(name: str, proc: asyncio.subprocess.Process):
    await proc.wait()
    _status[name] = "stopped"
    _handles[name] = None
    entry = f"[{datetime.now().strftime('%H:%M:%S')}] --- Process exited (code: {proc.returncode}) ---"
    _logs[name].append(entry)
    await _broadcast(name, entry + "\n")


async def _start(name: str):
    cfg = PROCESSES[name]
    if not os.path.exists(cfg["cmd"][1]):
        entry = f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] File tidak ditemukan: {cfg['cmd'][1]}"
        _logs[name].append(entry)
        await _broadcast(name, entry + "\n")
        return

    _status[name] = "starting"
    entry = f"[{datetime.now().strftime('%H:%M:%S')}] --- Starting {cfg['label']} ---"
    _logs[name].append(entry)
    await _broadcast(name, entry + "\n")

    proc = await asyncio.create_subprocess_exec(
        *cfg["cmd"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cfg["cwd"],
    )
    _handles[name] = proc
    _status[name] = "running"

    asyncio.create_task(_read_stream(name, proc.stdout))
    asyncio.create_task(_wait_proc(name, proc))


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(UI_FILE, encoding="utf-8") as f:
        return f.read()


@app.post("/api/auth/check")
async def api_auth_check(x_monitor_password: str = Header(default="")):
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True}


@app.get("/api/status/all")
async def status_all():
    return {
        name: {
            "label": PROCESSES[name]["label"],
            "status": _status[name],
            "pid": _handles[name].pid if _handles.get(name) else None,
        }
        for name in PROCESSES
    }


@app.post("/api/process/{name}/start")
async def api_start(name: str):
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    if _status[name] == "running":
        return {"status": "already_running"}
    asyncio.create_task(_start(name))
    return {"status": "starting"}


@app.post("/api/process/{name}/stop")
async def api_stop(name: str):
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    proc = _handles.get(name)
    if proc:
        proc.terminate()
        _status[name] = "stopping"
    return {"status": "stopping"}


@app.post("/api/process/{name}/kill")
async def api_kill(name: str, x_monitor_password: str = Header(default="")):
    """Force kill (SIGKILL) the process tracked by web_monitor."""
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    proc = _handles.get(name)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
        _status[name] = "stopped"
        _handles[name] = None
    return {"status": "killed"}


@app.post("/api/process/{name}/restart")
async def api_restart(name: str):
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    proc = _handles.get(name)
    if proc:
        proc.terminate()
        await asyncio.sleep(2)
    asyncio.create_task(_start(name))
    return {"status": "restarting"}


@app.get("/api/process/instances")
async def api_instances():
    """Return actual OS-level instance count per process (PowerShell + lock files + netstat)."""
    import subprocess as _sp

    # Single PowerShell call to get all Python process info
    ps_pids: Dict[str, list] = {k: [] for k in PROCESSES}
    try:
        out = _sp.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json"],
            stderr=_sp.DEVNULL, text=True, timeout=8,
        )
        data = json.loads(out.strip())
        if isinstance(data, dict):
            data = [data]
        for name, cfg in PROCESSES.items():
            script = os.path.basename(cfg["cmd"][1])
            for p in data:
                if script in (p.get("CommandLine") or ""):
                    pid = int(p["ProcessId"])
                    if _pid_alive(pid) and pid not in ps_pids[name]:
                        ps_pids[name].append(pid)
    except Exception:
        pass

    result = {}
    for name in PROCESSES:
        pids = list(ps_pids[name])

        # Add lock-file PID if not already in list
        lock_path = LOCK_FILES.get(name)
        if lock_path and os.path.exists(lock_path):
            try:
                with open(lock_path) as f:
                    lp = int(f.read().strip())
                if _pid_alive(lp) and lp not in pids:
                    pids.append(lp)
            except Exception:
                pass

        # Add port-based PID if not already in list
        cfg_port = PROCESSES[name].get("port")
        if cfg_port:
            port_pid = _get_pid_listening_on_port(cfg_port)
            if port_pid and port_pid not in pids:
                pids.append(port_pid)

        # Tracked PID from web_monitor handle (exclude sentinel -1)
        raw_tracked = _handles[name].pid if _handles.get(name) else None
        tracked_pid = raw_tracked if (raw_tracked and raw_tracked > 0) else (pids[0] if pids else None)

        result[name] = {
            "count": len(pids),
            "pids": pids,
            "tracked_pid": tracked_pid,
            "orphan_pid": next((p for p in pids if p != tracked_pid), None),
        }
    return result


@app.post("/api/process/{name}/kill_orphan")
async def api_kill_orphan(name: str, x_monitor_password: str = Header(default="")):
    """Kill all OS instances of a process (including orphans not tracked by web_monitor)."""
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    import subprocess as _sp
    cfg = PROCESSES[name]
    script = os.path.basename(cfg["cmd"][1])
    killed = []
    try:
        out = _sp.check_output(
            ["wmic", "process", "where", 'name="python.exe"',
             "get", "ProcessId,CommandLine"],
            stderr=_sp.DEVNULL, text=True, timeout=5,
        )
        for line in out.splitlines():
            if script in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    import ctypes as _ct
                    h = _ct.windll.kernel32.OpenProcess(0x1F0FFF, False, pid)
                    if h:
                        _ct.windll.kernel32.TerminateProcess(h, 1)
                        _ct.windll.kernel32.CloseHandle(h)
                        killed.append(pid)
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        return {"killed": killed, "error": str(e)}
    # Also clean lock file
    lock_path = LOCK_FILES.get(name)
    if lock_path and os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass
    _status[name] = "stopped"
    _handles[name] = None
    return {"killed": killed}


@app.post("/api/process/{name}/kill_duplicates")
async def api_kill_duplicates(name: str, x_monitor_password: str = Header(default="")):
    """Kill all OS instances of a process except the one tracked by web_monitor."""
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)

    import subprocess as _sp, signal as _sig
    cfg = PROCESSES[name]
    script = os.path.basename(cfg["cmd"][1])
    keep_pid = _handles[name].pid if _handles.get(name) else None

    killed = []
    try:
        out = _sp.check_output(
            ["wmic", "process", "where", 'name="python.exe"',
             "get", "ProcessId,CommandLine"],
            stderr=_sp.DEVNULL, text=True, timeout=5,
        )
        for line in out.splitlines():
            if script in line:
                parts = line.strip().split()
                try:
                    pid = int(parts[-1])
                    if pid != keep_pid:
                        _sp.run(["taskkill", "/F", "/PID", str(pid)],
                                capture_output=True, timeout=5)
                        killed.append(pid)
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        return {"killed": killed, "error": str(e)}
    return {"killed": killed, "keep": keep_pid}


@app.websocket("/ws/{name}")
async def ws_logs(websocket: WebSocket, name: str):
    if name not in PROCESSES:
        await websocket.close()
        return
    await websocket.accept()
    _clients[name].add(websocket)
    # Replay buffered logs
    for line in list(_logs[name]):
        try:
            await websocket.send_text(line + "\n")
        except Exception:
            break
    try:
        while True:
            await websocket.receive_text()  # keep-alive ping
    except WebSocketDisconnect:
        _clients[name].discard(websocket)


# ── YouTube data ──────────────────────────────────────────────────────────────

@app.get("/api/youtube/dashboard")
async def yt_dashboard():
    def _load(path):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    history = _load(os.path.join(DATA_DIR, "upload_history.json"))
    queue   = _load(os.path.join(DATA_DIR, "upload_queue.json"))
    return {"history": history, "queue": queue}


# ── Scraper results ───────────────────────────────────────────────────────────

@app.get("/api/scraper/files")
async def scraper_files():
    """List all Excel files available from scrapers."""
    search_dirs = [
        JEGAR_DIR,
        os.path.join(JEGAR_DIR, "output"),
        os.path.join(JEGAR_DIR, "scrapers"),
        os.path.join(JEGAR_DIR, "scrapers", "output"),
        BASE_DIR,
        os.path.join(BASE_DIR, "scrapers", "output"),
    ]
    files = []
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith(".xlsx"):
                fpath = os.path.join(d, fname)
                files.append({
                    "name": fname,
                    "path": fpath,
                    "size_kb": round(os.path.getsize(fpath) / 1024, 1),
                    "mtime": os.path.getmtime(fpath),
                    "mtime_str": datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%d %b %Y %H:%M"),
                })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files}


@app.get("/api/scraper/read")
async def scraper_read(path: str):
    """Parse an Excel file and return its sheets as JSON."""
    if not path.endswith(".xlsx") or not os.path.exists(path):
        return JSONResponse({"error": "file not found"}, status_code=404)

    import openpyxl
    result: dict = {"sheets": {}}
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            headers = []
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                cleaned = [str(c) if c is not None else "" for c in row]
                if i == 0:
                    headers = cleaned
                elif any(c for c in cleaned):
                    rows.append(cleaned)
                if i >= 300:
                    break
            result["sheets"][sheet_name] = {"headers": headers, "rows": rows}
        wb.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return result


# ── Books / 1% Perday ─────────────────────────────────────────────────────────

from fastapi.responses import FileResponse

@app.get("/api/books")
async def books_list():
    from book_processor import list_books
    return list_books()

@app.get("/api/books/{book_id}")
async def book_detail(book_id: str):
    from book_processor import get_book
    book = get_book(book_id)
    if not book:
        return JSONResponse({"error": "not found"}, status_code=404)
    return book

@app.get("/api/books/{book_id}/cover")
async def book_cover(book_id: str):
    from book_processor import get_book_cover_path
    path = get_book_cover_path(book_id)
    if not path:
        return JSONResponse({"error": "no cover"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")

@app.get("/api/books/{book_id}/audio/{part}")
async def book_audio(book_id: str, part: int):
    from book_processor import get_book_audio_path
    path = get_book_audio_path(book_id, part)
    if not path:
        return JSONResponse({"error": "no audio"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg")

@app.get("/api/books/{book_id}/script/{part}")
async def book_script(book_id: str, part: int):
    from book_processor import get_book_script
    script = get_book_script(book_id, part)
    if script is None:
        return JSONResponse({"error": "no script"}, status_code=404)
    return {"script": script}

@app.post("/api/books/process")
async def books_process(body: dict = Body(...)):
    """Trigger book processing from a PDF path (called by Discord bot)."""
    pdf_path = body.get("pdf_path", "")
    if not os.path.exists(pdf_path):
        return JSONResponse({"error": "pdf not found"}, status_code=404)
    upload_id = f"discord_{os.path.basename(pdf_path)}"
    asyncio.create_task(_run_book_processing(pdf_path, upload_id))
    return {"status": "processing_started", "upload_id": upload_id}

@app.post("/api/books/upload")
async def books_upload(file: UploadFile = File(...)):
    """Accept PDF upload from dashboard and process it."""
    import uuid
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files are supported"}, status_code=400)

    upload_id = str(uuid.uuid4())
    temp_dir = os.path.join(DATA_DIR, "tmp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    pdf_path = os.path.join(temp_dir, f"{upload_id}.pdf")

    content = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(content)

    # Write to queue for book_worker.py visibility, also process in-process for WS progress
    _enqueue_book_job(pdf_path, upload_id)
    asyncio.create_task(_run_book_processing(pdf_path, upload_id))
    return {"status": "processing_started", "upload_id": upload_id}


def _enqueue_book_job(pdf_path: str, upload_id: str):
    """Add a job to data/book_queue.json for book_worker.py visibility."""
    queue_file = os.path.join(DATA_DIR, "book_queue.json")
    try:
        queue = json.loads(open(queue_file, encoding="utf-8").read()) if os.path.exists(queue_file) else []
    except Exception:
        queue = []
    if not any(j.get("upload_id") == upload_id for j in queue):
        queue.append({"pdf_path": pdf_path, "upload_id": upload_id})
        with open(queue_file, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)

_book_status: Dict[str, str] = {}
_book_progress: Dict[str, list] = {}  # upload_id -> list of progress messages
_book_progress_subs: Dict[str, Set[WebSocket]] = {}  # upload_id -> set of websockets

async def _broadcast_progress(upload_id: str, msg: str):
    _book_progress.setdefault(upload_id, []).append(msg)
    subs = _book_progress_subs.get(upload_id, set())
    dead = set()
    for ws in subs:
        try:
            await ws.send_text(json.dumps({"type": "progress", "msg": msg}))
        except Exception:
            dead.add(ws)
    for ws in dead:
        subs.discard(ws)

async def _run_book_processing(pdf_path: str, upload_id: str):
    async def progress_cb(msg: str):
        await _broadcast_progress(upload_id, msg)

    try:
        from book_processor import process_book
        book_id = await process_book(pdf_path, progress_cb=progress_cb)
        _book_status[upload_id] = f"done:{book_id}"
        await _broadcast_progress(upload_id, f"✅ Selesai! book_id={book_id}")
        await _broadcast_book_done(upload_id, book_id)
    except Exception as e:
        _book_status[upload_id] = f"error:{e}"
        await _broadcast_progress(upload_id, f"❌ Error: {e}")
        print(f"[Books] Processing error: {e}")

async def _broadcast_book_done(upload_id: str, book_id: str):
    subs = _book_progress_subs.get(upload_id, set())
    for ws in list(subs):
        try:
            await ws.send_text(json.dumps({"type": "done", "book_id": book_id}))
        except Exception:
            pass

@app.get("/api/books/status/{upload_id}")
async def book_process_status(upload_id: str):
    return {"status": _book_status.get(upload_id, "processing"),
            "log": _book_progress.get(upload_id, [])}

@app.websocket("/ws/books/progress/{upload_id}")
async def ws_book_progress(websocket: WebSocket, upload_id: str):
    await websocket.accept()
    _book_progress_subs.setdefault(upload_id, set()).add(websocket)
    # Send existing log if any
    for msg in _book_progress.get(upload_id, []):
        try:
            await websocket.send_text(json.dumps({"type": "progress", "msg": msg}))
        except Exception:
            break
    # Check if already done
    status = _book_status.get(upload_id, "")
    if status.startswith("done:"):
        book_id = status.split(":", 1)[1]
        try:
            await websocket.send_text(json.dumps({"type": "done", "book_id": book_id}))
        except Exception:
            pass
    try:
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        _book_progress_subs.get(upload_id, set()).discard(websocket)


# ── Auto-start on launch ──────────────────────────────────────────────────────

# Bots started automatically when web_monitor.py runs
AUTO_START = ["bot", "watcher", "dashboard", "pty", "books"]


def _port_in_use(port: int) -> bool:
    """Return True if something is already listening on the given port."""
    import socket as _sock
    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@app.on_event("startup")
async def on_startup():
    """On launch: detect already-running bots, then auto-start missing ones."""
    running = _scan_running_pids()

    # Port-based detection already handled in _scan_running_pids() via _get_pid_listening_on_port

    for name in PROCESSES:
        if name in running:
            pid = running[name]
            orphan = OrphanProcess(pid)
            _handles[name] = orphan
            _status[name]  = "running"
            entry = f"[{datetime.now().strftime('%H:%M:%S')}] --- Detected already running (PID {pid}) ---"
            _logs[name].append(entry)
            asyncio.create_task(_wait_proc(name, orphan))
            asyncio.create_task(_tail_log_file(name))
            print(f"[Monitor] Detected: {PROCESSES[name]['label']} (PID {pid})")

    for name in AUTO_START:
        if _status[name] != "running":
            print(f"[Monitor] Auto-starting: {PROCESSES[name]['label']}")
            asyncio.create_task(_start(name))


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Coroco House Monitor")
    print("  http://localhost:8080")
    print("  Auto-start: bot, watcher, dashboard")
    print("  Tekan Ctrl+C untuk berhenti")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
