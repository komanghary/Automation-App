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


async def _read_stream(name: str, stream):
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
        except Exception:
            break


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
_g_pty:    Optional[_winpty.PtyProcess] = None
_g_pty_buf: _deque = _deque(maxlen=300)          # ring buffer of recent output chunks
_g_pty_clients: Set[WebSocket] = set()
_g_pty_reader_running: bool = False


def _ensure_pty() -> _winpty.PtyProcess:
    global _g_pty
    if _g_pty is None or not _g_pty.isalive():
        _g_pty = _winpty.PtyProcess.spawn(
            "cmd.exe",
            dimensions=(24, 220),
            env=_build_pty_env(),
            cwd=BASE_DIR,
        )
        _g_pty_buf.clear()
    return _g_pty


async def _pty_reader_loop():
    """Single background task that reads PTY output and broadcasts to all clients."""
    global _g_pty_reader_running
    _g_pty_reader_running = True
    loop = asyncio.get_event_loop()
    try:
        while True:
            pty = _g_pty
            if pty is None or not pty.isalive():
                await asyncio.sleep(0.5)
                continue
            try:
                chunk = await loop.run_in_executor(None, lambda: pty.read(4096))
            except Exception:
                await asyncio.sleep(0.2)
                continue
            if not chunk:
                await asyncio.sleep(0.05)
                continue
            _g_pty_buf.append(chunk)
            msg = json.dumps({"t": "data", "d": chunk})
            dead: Set[WebSocket] = set()
            for ws in list(_g_pty_clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            _g_pty_clients -= dead
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


# ── Auto-start on launch ──────────────────────────────────────────────────────

# Bots started automatically when web_monitor.py runs
AUTO_START = ["bot", "watcher", "dashboard"]


@app.on_event("startup")
async def on_startup():
    """Auto-start core bots when the monitor launches."""
    for name in AUTO_START:
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
