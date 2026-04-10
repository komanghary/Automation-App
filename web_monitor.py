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
async def api_stop(name: str, x_monitor_password: str = Header(default="")):
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if name not in PROCESSES:
        return JSONResponse({"error": "unknown"}, status_code=404)
    proc = _handles.get(name)
    if proc:
        proc.terminate()
        _status[name] = "stopping"
    return {"status": "stopping"}


@app.post("/api/process/{name}/restart")
async def api_restart(name: str, x_monitor_password: str = Header(default="")):
    if MONITOR_PASSWORD and x_monitor_password != MONITOR_PASSWORD:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
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
