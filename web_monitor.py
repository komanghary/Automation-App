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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, Body
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


def _scan_running_pids() -> Dict[str, int]:
    """Find already-running bot processes. Lock files for tracked bots, wmic for others."""
    import subprocess as _sp
    found: Dict[str, int] = {}

    # Lock-file based detection (bot, watcher, dashboard)
    for name, lock_path in LOCK_FILES.items():
        if not os.path.exists(lock_path):
            continue
        try:
            with open(lock_path) as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                found[name] = pid
        except Exception:
            pass

    # wmic-based detection for processes without lock files (trending)
    wmic_targets = {k: os.path.basename(v["cmd"][1]) for k, v in PROCESSES.items()
                    if k not in LOCK_FILES}
    if wmic_targets:
        try:
            out = _sp.check_output(
                ["wmic", "process", "where", 'name="python.exe"',
                 "get", "ProcessId,CommandLine"],
                stderr=_sp.DEVNULL, text=True, timeout=5,
            )
            for name, script in wmic_targets.items():
                if name in found:
                    continue
                for line in out.splitlines():
                    if script in line:
                        parts = line.strip().split()
                        try:
                            found[name] = int(parts[-1])
                            break
                        except (ValueError, IndexError):
                            pass
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
    """Return actual OS-level instance count per process (wmic + lock files)."""
    import subprocess as _sp
    # Single wmic call for all processes
    wmic_pids: Dict[str, list] = {k: [] for k in PROCESSES}
    try:
        out = _sp.check_output(
            ["wmic", "process", "where", 'name="python.exe"',
             "get", "ProcessId,CommandLine"],
            stderr=_sp.DEVNULL, text=True, timeout=5,
        )
        for name, cfg in PROCESSES.items():
            script = os.path.basename(cfg["cmd"][1])
            for line in out.splitlines():
                if script in line:
                    parts = line.strip().split()
                    try:
                        wmic_pids[name].append(int(parts[-1]))
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass

    result = {}
    for name in PROCESSES:
        pids = wmic_pids[name]
        # Also check lock file for orphan PID (more reliable than wmic)
        lock_path = LOCK_FILES.get(name)
        orphan_pid = None
        if lock_path and os.path.exists(lock_path):
            try:
                with open(lock_path) as f:
                    lp = int(f.read().strip())
                if _pid_alive(lp) and lp not in pids:
                    pids.append(lp)
                    orphan_pid = lp
            except Exception:
                pass
        tracked_pid = _handles[name].pid if _handles.get(name) else None
        result[name] = {
            "count": len(pids),
            "pids": pids,
            "tracked_pid": tracked_pid,
            "orphan_pid": orphan_pid or (pids[0] if pids and pids[0] != tracked_pid else None),
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


# ── Shell terminal ────────────────────────────────────────────────────────────

@app.websocket("/terminal")
async def ws_shell(websocket: WebSocket):
    await websocket.accept()
    try:
        # Layer 1+2 auth — first message must be JSON with both passwords
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        data = json.loads(raw)
        p1 = data.get("p1", "")
        p2 = data.get("p2", "")
        if not MONITOR_PASSWORD or not SHELL_PASSWORD:
            await websocket.send_text(json.dumps({"t": "auth", "ok": False, "msg": "Shell password belum dikonfigurasi di .env"}))
            await websocket.close()
            return
        if p1 != MONITOR_PASSWORD or p2 != SHELL_PASSWORD:
            await websocket.send_text(json.dumps({"t": "auth", "ok": False, "msg": "Password salah"}))
            await websocket.close()
            return
        await websocket.send_text(json.dumps({"t": "auth", "ok": True}))

        # Build full Windows PATH for subprocess so all tools (claude, npm, etc.) are accessible
        import subprocess as _sp
        _win_path_extras = [
            r"C:\Users\Administrator\AppData\Roaming\npm",
            r"C:\Users\Administrator\.local\bin",
            r"C:\Users\Administrator\.bun\bin",
            r"C:\Program Files\nodejs",
            r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Scripts",
            r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314",
            r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links",
            r"C:\WINDOWS\system32",
            r"C:\WINDOWS",
        ]
        _shell_env = os.environ.copy()
        _cur_path = _shell_env.get("PATH", "")
        _extra = ";".join(p for p in _win_path_extras if p.replace("\\", "/").lower() not in _cur_path.lower())
        if _extra:
            _shell_env["PATH"] = _extra + ";" + _cur_path

        # Command loop
        cwd = BASE_DIR
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            cmd = data.get("cmd", "").strip()
            if not cmd:
                continue

            # Handle cd separately so cwd persists
            if cmd.startswith("cd "):
                target = cmd[3:].strip().strip('"').strip("'")
                new_cwd = os.path.normpath(os.path.join(cwd, target))
                if os.path.isdir(new_cwd):
                    cwd = new_cwd
                    await websocket.send_text(json.dumps({"t": "out", "d": f"{cwd}\n"}))
                else:
                    await websocket.send_text(json.dumps({"t": "out", "d": f"cd: no such directory: {target}\n"}))
                await websocket.send_text(json.dumps({"t": "done", "code": 0, "cwd": cwd}))
                continue

            await websocket.send_text(json.dumps({"t": "out", "d": f"\x1b[90m$ {cmd}\x1b[0m\n"}))
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=_shell_env,
                )
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    await websocket.send_text(json.dumps({"t": "out", "d": line.decode("utf-8", errors="replace")}))
                await proc.wait()
                await websocket.send_text(json.dumps({"t": "done", "code": proc.returncode, "cwd": cwd}))
            except Exception as e:
                await websocket.send_text(json.dumps({"t": "out", "d": f"[ERROR] {e}\n"}))
                await websocket.send_text(json.dumps({"t": "done", "code": 1, "cwd": cwd}))

    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except Exception:
        pass


# ── Interactive PTY terminal (global persistent session) ──────────────────────

import winpty as _winpty
from collections import deque as _deque

_PTY_ENV_EXTRAS = [
    r"C:\Users\Administrator\AppData\Roaming\npm",
    r"C:\Users\Administrator\.local\bin",
    r"C:\Users\Administrator\.bun\bin",
    r"C:\Program Files\nodejs",
    r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Scripts",
    r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314",
    r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links",
    r"C:\WINDOWS\system32",
    r"C:\WINDOWS",
]

def _build_pty_env():
    env = os.environ.copy()
    cur = env.get("PATH", "")
    extra = ";".join(p for p in _PTY_ENV_EXTRAS if p.lower() not in cur.lower())
    env["PATH"] = extra + ";" + cur if extra else cur
    return env

# Global PTY state — survives browser tab close/reopen and reconnects
import threading as _threading
import queue as _tqueue

_g_pty:    Optional[_winpty.PtyProcess] = None
_g_pty_buf: _deque = _deque(maxlen=300)          # ring buffer of recent output chunks
_g_pty_clients: Set[WebSocket] = set()
_g_pty_reader_running: bool = False
_g_pty_chunk_queue: _tqueue.SimpleQueue = _tqueue.SimpleQueue()
_g_pty_thread: Optional[_threading.Thread] = None


def _pty_blocking_reader():
    """Daemon thread: blocking-reads PTY and enqueues chunks for the asyncio broadcaster."""
    while True:
        pty = _g_pty
        if pty is None or not pty.isalive():
            import time as _time; _time.sleep(0.3)
            continue
        try:
            chunk = pty.read(1024)
            if chunk:
                _g_pty_chunk_queue.put(chunk)
        except Exception:
            import time as _time; _time.sleep(0.1)


def _ensure_pty() -> _winpty.PtyProcess:
    global _g_pty, _g_pty_thread
    if _g_pty is None or not _g_pty.isalive():
        _g_pty = _winpty.PtyProcess.spawn(
            "cmd.exe",
            dimensions=(24, 220),
            env=_build_pty_env(),
            cwd=BASE_DIR,
        )
        _g_pty_buf.clear()
    # Start blocking reader thread if not alive
    if _g_pty_thread is None or not _g_pty_thread.is_alive():
        _g_pty_thread = _threading.Thread(target=_pty_blocking_reader, daemon=True)
        _g_pty_thread.start()
    return _g_pty


async def _pty_reader_loop():
    """Asyncio task: drains the chunk queue and broadcasts to all WS clients."""
    global _g_pty_reader_running, _g_pty_clients
    _g_pty_reader_running = True
    try:
        while True:
            # Drain everything currently in the queue (non-blocking)
            chunks = []
            try:
                while True:
                    chunks.append(_g_pty_chunk_queue.get_nowait())
            except _tqueue.Empty:
                pass

            if chunks:
                dead: Set[WebSocket] = set()
                for chunk in chunks:
                    _g_pty_buf.append(chunk)
                    msg = json.dumps({"t": "data", "d": chunk})
                    for ws in list(_g_pty_clients):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.add(ws)
                _g_pty_clients -= dead
            else:
                # No data — sleep briefly so we don't busy-spin
                await asyncio.sleep(0.02)
    finally:
        _g_pty_reader_running = False


@app.websocket("/pty")
async def ws_pty(websocket: WebSocket):
    global _g_pty_reader_running
    await websocket.accept()
    try:
        raw  = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        data = json.loads(raw)
        if data.get("p1") != MONITOR_PASSWORD or data.get("p2") != SHELL_PASSWORD:
            await websocket.send_text(json.dumps({"t": "auth", "ok": False, "msg": "Password salah"}))
            await websocket.close()
            return
        await websocket.send_text(json.dumps({"t": "auth", "ok": True}))

        # Ensure PTY exists and reader is running
        _ensure_pty()
        if not _g_pty_reader_running:
            asyncio.create_task(_pty_reader_loop())
            await asyncio.sleep(0.1)

        # Replay buffered output so user sees recent history on reconnect
        for chunk in list(_g_pty_buf):
            try:
                await websocket.send_text(json.dumps({"t": "data", "d": chunk}))
            except Exception:
                break

        _g_pty_clients.add(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                if msg.get("t") == "input" and _g_pty and _g_pty.isalive():
                    _g_pty.write(msg["d"])
                elif msg.get("t") == "resize" and _g_pty and _g_pty.isalive():
                    _g_pty.setwinsize(int(msg.get("rows", 24)), int(msg.get("cols", 220)))
                elif msg.get("t") == "kill":
                    # Explicit kill request from UI
                    if _g_pty:
                        try: _g_pty.terminate(force=True)
                        except Exception: pass
                    break
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            _g_pty_clients.discard(websocket)

    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except Exception:
        pass


# ── Claude Code PTY terminal ──────────────────────────────────────────────────

CLAUDE_EXE = r"C:\Users\Administrator\AppData\Roaming\npm\claude.exe"
CLAUDE_NOTIFY_CHANNEL = os.getenv("CLAUDE_NOTIFY_CHANNEL_ID", os.getenv("WATCHER_CHANNEL_ID", ""))

_cc_pty:    Optional[_winpty.PtyProcess] = None
_cc_pty_buf: _deque = _deque(maxlen=300)
_cc_pty_clients: Set[WebSocket] = set()
_cc_pty_reader_running: bool = False
_cc_pty_chunk_queue: _tqueue.SimpleQueue = _tqueue.SimpleQueue()
_cc_pty_thread: Optional[_threading.Thread] = None
_cc_error_buf: str = ""
_cc_last_alert: float = 0.0


def _cc_blocking_reader():
    """Daemon thread: blocking-reads Claude Code PTY."""
    while True:
        pty = _cc_pty
        if pty is None or not pty.isalive():
            import time as _time; _time.sleep(0.3)
            continue
        try:
            chunk = pty.read(1024)
            if chunk:
                _cc_pty_chunk_queue.put(chunk)
        except Exception:
            import time as _time; _time.sleep(0.1)


def _ensure_cc_pty(fresh: bool = False) -> _winpty.PtyProcess:
    global _cc_pty, _cc_pty_thread
    if _cc_pty is None or not _cc_pty.isalive() or fresh:
        if _cc_pty and _cc_pty.isalive():
            try: _cc_pty.terminate(force=True)
            except Exception: pass
        _cc_pty = _winpty.PtyProcess.spawn(
            "cmd.exe",
            dimensions=(40, 200),
            env=_build_pty_env(),
            cwd=BASE_DIR,
        )
        _cc_pty_buf.clear()
    if _cc_pty_thread is None or not _cc_pty_thread.is_alive():
        _cc_pty_thread = _threading.Thread(target=_cc_blocking_reader, daemon=True)
        _cc_pty_thread.start()
    return _cc_pty


async def _send_discord_error(text: str):
    """Send Claude Code error notification to Discord via HTTP API."""
    if not DISCORD_TOKEN or not CLAUDE_NOTIFY_CHANNEL:
        return
    import urllib.request as _ur
    payload = json.dumps({"content": f"⚠️ **Claude Code Error Detected:**\n```\n{text[:1800]}\n```"}).encode()
    req = _ur.Request(
        f"https://discord.com/api/v10/channels/{CLAUDE_NOTIFY_CHANNEL}/messages",
        data=payload,
        headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        await asyncio.to_thread(_ur.urlopen, req, timeout=10)
    except Exception:
        pass


async def _cc_reader_loop():
    global _cc_pty_reader_running, _cc_pty_clients, _cc_error_buf, _cc_last_alert
    import re as _re, time as _time
    _ANSI = _re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJM]')
    _cc_pty_reader_running = True
    try:
        while True:
            chunks = []
            try:
                while True:
                    chunks.append(_cc_pty_chunk_queue.get_nowait())
            except _tqueue.Empty:
                pass

            if chunks:
                dead: Set[WebSocket] = set()
                for chunk in chunks:
                    _cc_pty_buf.append(chunk)
                    msg = json.dumps({"t": "data", "d": chunk})
                    for ws in list(_cc_pty_clients):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.add(ws)
                    # Error detection → Discord notification
                    _cc_error_buf = (_cc_error_buf + chunk)[-3000:]
                    now = _time.time()
                    if now - _cc_last_alert > 60:
                        clean = _ANSI.sub('', _cc_error_buf)
                        if _re.search(r'(Error|Traceback|FATAL|uncaught exception|crashed)', clean, _re.IGNORECASE):
                            _cc_last_alert = now
                            asyncio.create_task(_send_discord_error(clean[-600:]))
                _cc_pty_clients -= dead
            else:
                await asyncio.sleep(0.02)
    finally:
        _cc_pty_reader_running = False


@app.websocket("/claude-pty")
async def ws_claude_pty(websocket: WebSocket):
    global _cc_pty_reader_running
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        data = json.loads(raw)
        if data.get("p1") != MONITOR_PASSWORD or data.get("p2") != SHELL_PASSWORD:
            await websocket.send_text(json.dumps({"t": "auth", "ok": False, "msg": "Password salah"}))
            await websocket.close()
            return
        await websocket.send_text(json.dumps({"t": "auth", "ok": True}))

        is_new = _cc_pty is None or not _cc_pty.isalive()
        _ensure_cc_pty()
        if not _cc_pty_reader_running:
            asyncio.create_task(_cc_reader_loop())
            await asyncio.sleep(0.1)
        # Auto-launch claude if this is a fresh PTY session
        if is_new:
            await asyncio.sleep(1.0)  # wait for cmd.exe prompt
            _cc_pty.write(f"claude\r\n")

        for chunk in list(_cc_pty_buf):
            try:
                await websocket.send_text(json.dumps({"t": "data", "d": chunk}))
            except Exception:
                break

        _cc_pty_clients.add(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                if msg.get("t") == "input" and _cc_pty and _cc_pty.isalive():
                    _cc_pty.write(msg["d"])
                elif msg.get("t") == "resize" and _cc_pty and _cc_pty.isalive():
                    _cc_pty.setwinsize(int(msg.get("rows", 40)), int(msg.get("cols", 200)))
                elif msg.get("t") == "kill":
                    if _cc_pty:
                        try: _cc_pty.terminate(force=True)
                        except Exception: pass
                    _ensure_cc_pty(fresh=True)
                    await asyncio.sleep(1.0)
                    _cc_pty.write("claude\r\n")
                    break
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            _cc_pty_clients.discard(websocket)

    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except Exception:
        pass


# ── Auto-start on launch ──────────────────────────────────────────────────────

# Bots started automatically when web_monitor.py runs
AUTO_START = ["bot", "watcher", "dashboard"]


@app.on_event("startup")
async def on_startup():
    """On launch: detect already-running bots, then auto-start missing ones."""
    running = _scan_running_pids()

    for name in PROCESSES:
        if name in running:
            pid = running[name]
            orphan = OrphanProcess(pid)
            _handles[name] = orphan
            _status[name]  = "running"
            entry = f"[{datetime.now().strftime('%H:%M:%S')}] --- Detected already running (PID {pid}) ---"
            _logs[name].append(entry)
            asyncio.create_task(_wait_proc(name, orphan))
            asyncio.create_task(_tail_log_file(name))   # stream log file
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
