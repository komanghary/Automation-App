"""
Coroco House PTY Server — Persistent terminal & Claude Code PTY.
Run: python pty_server.py
Port: 8081  (independent dari web_monitor, JANGAN restart sembarangan)

Dipisah dari web_monitor.py agar restart web_monitor tidak memutus
sesi Claude Code / terminal yang sedang aktif.
"""

import asyncio
import json
import os
from collections import deque as _deque
from typing import Optional, Set

import winpty as _winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
MONITOR_PASSWORD = os.getenv("MONITOR_PASSWORD", "")
SHELL_PASSWORD   = os.getenv("SHELL_PASSWORD", "")
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN", "")
CLAUDE_NOTIFY_CHANNEL = os.getenv("CLAUDE_NOTIFY_CHANNEL_ID", os.getenv("WATCHER_CHANNEL_ID", ""))

app = FastAPI(title="Coroco PTY Server")

# Allow web_monitor (port 8080) to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import threading as _threading
import queue as _tqueue

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


# ── Shell terminal (basic command-by-command) ──────────────────────────────────

@app.websocket("/terminal")
async def ws_shell(websocket: WebSocket):
    await websocket.accept()
    try:
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

        _shell_env = _build_pty_env()
        cwd = BASE_DIR
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            cmd = data.get("cmd", "").strip()
            if not cmd:
                continue
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


# ── Interactive PTY (global persistent cmd.exe session) ───────────────────────

_g_pty:    Optional[_winpty.PtyProcess] = None
_g_pty_buf: _deque = _deque(maxlen=300)
_g_pty_clients: Set[WebSocket] = set()
_g_pty_reader_running: bool = False
_g_pty_chunk_queue: _tqueue.SimpleQueue = _tqueue.SimpleQueue()
_g_pty_thread: Optional[_threading.Thread] = None


def _pty_blocking_reader():
    while True:
        pty = _g_pty
        if pty is None or not pty.isalive():
            import time as _t; _t.sleep(0.3)
            continue
        try:
            chunk = pty.read(1024)
            if chunk:
                _g_pty_chunk_queue.put(chunk)
        except Exception:
            import time as _t; _t.sleep(0.1)


def _ensure_pty() -> _winpty.PtyProcess:
    global _g_pty, _g_pty_thread
    if _g_pty is None or not _g_pty.isalive():
        _g_pty = _winpty.PtyProcess.spawn(
            "cmd.exe", dimensions=(24, 220),
            env=_build_pty_env(), cwd=BASE_DIR,
        )
        _g_pty_buf.clear()
    if _g_pty_thread is None or not _g_pty_thread.is_alive():
        _g_pty_thread = _threading.Thread(target=_pty_blocking_reader, daemon=True)
        _g_pty_thread.start()
    return _g_pty


async def _pty_reader_loop():
    global _g_pty_reader_running, _g_pty_clients
    _g_pty_reader_running = True
    try:
        while True:
            chunks = []
            try:
                while True: chunks.append(_g_pty_chunk_queue.get_nowait())
            except _tqueue.Empty: pass
            if chunks:
                dead: Set[WebSocket] = set()
                for chunk in chunks:
                    _g_pty_buf.append(chunk)
                    msg = json.dumps({"t": "data", "d": chunk})
                    for ws in list(_g_pty_clients):
                        try: await ws.send_text(msg)
                        except Exception: dead.add(ws)
                _g_pty_clients -= dead
            else:
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
        _ensure_pty()
        if not _g_pty_reader_running:
            asyncio.create_task(_pty_reader_loop())
            await asyncio.sleep(0.1)
        for chunk in list(_g_pty_buf):
            try: await websocket.send_text(json.dumps({"t": "data", "d": chunk}))
            except Exception: break
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


# ── Claude Code PTY (persistent claude session) ───────────────────────────────

CLAUDE_EXE = r"C:\Users\Administrator\AppData\Roaming\npm\claude.exe"

_cc_pty:    Optional[_winpty.PtyProcess] = None
_cc_pty_buf: _deque = _deque(maxlen=300)
_cc_pty_clients: Set[WebSocket] = set()
_cc_pty_reader_running: bool = False
_cc_pty_chunk_queue: _tqueue.SimpleQueue = _tqueue.SimpleQueue()
_cc_pty_thread: Optional[_threading.Thread] = None
_cc_error_buf: str = ""
_cc_last_alert: float = 0.0


def _cc_blocking_reader():
    while True:
        pty = _cc_pty
        if pty is None or not pty.isalive():
            import time as _t; _t.sleep(0.3)
            continue
        try:
            chunk = pty.read(1024)
            if chunk: _cc_pty_chunk_queue.put(chunk)
        except Exception:
            import time as _t; _t.sleep(0.1)


def _ensure_cc_pty(fresh: bool = False) -> _winpty.PtyProcess:
    global _cc_pty, _cc_pty_thread
    if _cc_pty is None or not _cc_pty.isalive() or fresh:
        if _cc_pty and _cc_pty.isalive():
            try: _cc_pty.terminate(force=True)
            except Exception: pass
        _cc_pty = _winpty.PtyProcess.spawn(
            "cmd.exe", dimensions=(40, 200),
            env=_build_pty_env(), cwd=BASE_DIR,
        )
        _cc_pty_buf.clear()
    if _cc_pty_thread is None or not _cc_pty_thread.is_alive():
        _cc_pty_thread = _threading.Thread(target=_cc_blocking_reader, daemon=True)
        _cc_pty_thread.start()
    return _cc_pty


async def _send_discord_error(text: str):
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
                while True: chunks.append(_cc_pty_chunk_queue.get_nowait())
            except _tqueue.Empty: pass
            if chunks:
                dead: Set[WebSocket] = set()
                for chunk in chunks:
                    _cc_pty_buf.append(chunk)
                    msg = json.dumps({"t": "data", "d": chunk})
                    for ws in list(_cc_pty_clients):
                        try: await ws.send_text(msg)
                        except Exception: dead.add(ws)
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
        if is_new:
            await asyncio.sleep(1.0)
            _cc_pty.write("claude\r\n")

        for chunk in list(_cc_pty_buf):
            try: await websocket.send_text(json.dumps({"t": "data", "d": chunk}))
            except Exception: break

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


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Coroco PTY Server")
    print("  Port: 8081  (JANGAN restart sembarangan!)")
    print("  Endpoints: /terminal  /pty  /claude-pty")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
