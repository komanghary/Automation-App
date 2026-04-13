"""
Dashboard Worker — Standalone YouTube dashboard + copyright checker.
Run separately: python dashboard_worker.py
"""

import io
import os
import sys

# Force UTF-8 stdout/stderr so emojis don't crash on Windows (cp1252)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# ── Single-instance lock ──────────────────────────────────────────────────────
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dashboard.lock")


def _acquire_lock():
    import atexit
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            import ctypes, subprocess as _sp
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                try:
                    out = _sp.check_output(
                        ['wmic', 'process', 'where', f'ProcessId={old_pid}', 'get', 'CommandLine'],
                        stderr=_sp.DEVNULL, text=True, timeout=3
                    )
                    if 'dashboard_worker.py' not in out:
                        raise ValueError("PID reused by different process")
                except Exception:
                    pass
                else:
                    print(f"[ERROR] Dashboard worker sudah berjalan (PID {old_pid}). Hentikan instance lama dulu.")
                    sys.exit(1)
        except SystemExit:
            raise
        except Exception:
            pass  # stale lock — overwrite
    with open(_LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

    def _release():
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass
    atexit.register(_release)


_acquire_lock()

import discord
from config import DISCORD_TOKEN, DASHBOARD_CHANNEL_ID, DASHBOARD_INTERVAL
from core import dashboard


intents = discord.Intents.default()
bot = discord.Client(intents=intents)

dashboard.init(bot)


@bot.event
async def on_ready():
    print(f"\n[Dashboard] Online sebagai {bot.user} (ID: {bot.user.id})")
    if DASHBOARD_CHANNEL_ID:
        print(f"[Dashboard] Channel : {DASHBOARD_CHANNEL_ID}")
    print(f"[Dashboard] Interval: {DASHBOARD_INTERVAL}s")
    bot.loop.create_task(dashboard.dashboard_loop())


def main():
    print("=" * 55)
    print("  Dashboard + Copyright Worker")
    print("=" * 55)

    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN harus diisi di .env!")
        return

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
