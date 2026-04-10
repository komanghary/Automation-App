"""
Instagram Watcher Worker — Standalone IG poller.
Polls IG group chats and writes new videos to data/watcher_queue.json.
Run separately: python watcher_worker.py
"""

import asyncio
import io
import json
import os
import shutil
import sys

# Force UTF-8 stdout/stderr so emojis don't crash on Windows (cp1252)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# ── Single-instance lock ──────────────────────────────────────────────────────
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "watcher.lock")


def _acquire_lock():
    import atexit
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                print(f"[ERROR] Watcher sudah berjalan (PID {old_pid}). Hentikan instance lama dulu.")
                sys.exit(1)
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

from config import (
    IG_USERNAME, IG_PASSWORD, POLL_INTERVAL,
    WATCHER_CHANNEL_ID, WATCHLIST, TEMP_DIR, DATA_DIR,
    FILM_CHANNEL_ID, MOTIVATIONAL_CHANNEL_ID, BOLA_GEMING_CHANNEL_ID,
    WITA,
)
from services.instagram import (
    ig_login, get_group_threads, extract_video,
    download_video, compress_for_preview,
)

SEEN_FILE = os.path.join(DATA_DIR, "watcher_seen.json")
QUEUE_FILE = os.path.join(DATA_DIR, "watcher_queue.json")


# ── Queue helpers ─────────────────────────────────────────────────────────────

def _load_queue() -> list[dict]:
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_queue(queue: list[dict]):
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, 'w') as f:
        json.dump(queue, f, indent=2)


def _enqueue_video(hd_path: str, preview_path: str, channel_id: int, group_name: str):
    from datetime import datetime
    queue = _load_queue()
    queue.append({
        "hd_path": hd_path,
        "preview_path": preview_path,
        "channel_id": channel_id,
        "group_name": group_name,
        "queued_at": datetime.now(WITA).isoformat(),
    })
    _save_queue(queue)
    print(f"   [Queue] Ditambahkan: {os.path.basename(hd_path)} -> ch:{channel_id}")


# ── Seen persistence ──────────────────────────────────────────────────────────

def _load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_seen(watch_groups: list[dict]):
    data = {}
    for wg in watch_groups:
        tid = str(wg["thread_id"])
        seen_list = list(wg["seen"])
        data[tid] = seen_list[-200:] if len(seen_list) > 200 else seen_list
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    with open(SEEN_FILE, 'w') as f:
        json.dump(data, f)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _match_group_channel(group_name: str) -> int | None:
    name_lower = group_name.lower()
    for pattern, ch_id in WATCHLIST.items():
        if pattern in name_lower or name_lower in pattern:
            return ch_id
    return None


# ── Polling ───────────────────────────────────────────────────────────────────

async def poll_new_videos(ig_client, wg: dict, watch_groups: list[dict]):
    loop = asyncio.get_event_loop()
    thread_id = wg["thread_id"]
    group_name = wg["name"]
    channel_id = wg["channel_id"]
    seen = wg["seen"]

    try:
        thread = await loop.run_in_executor(
            None, lambda: ig_client.direct_thread(thread_id, amount=20)
        )
    except Exception as e:
        print(f"[Watcher:{group_name}] Gagal ambil pesan: {e}")
        return

    new_msgs = [msg for msg in thread.messages if str(msg.id) not in seen]
    if new_msgs:
        print(f"[Watcher:{group_name}] {len(new_msgs)} pesan baru terdeteksi")

    new_videos = 0
    for msg in thread.messages:
        msg_id = str(msg.id)
        if msg_id in seen:
            continue

        seen.add(msg_id)

        url, label = await loop.run_in_executor(None, extract_video, ig_client, msg)
        if not url:
            continue

        try:
            ts = msg.timestamp.strftime("%Y%m%d_%H%M%S")
        except Exception:
            ts = f"msg_{msg_id[:8]}"

        sender = str(msg.user_id or "unknown")
        base_name = f"{ts}_{sender}"
        hd_path = os.path.join(TEMP_DIR, f"{base_name}_HD.mp4")
        preview_path = os.path.join(TEMP_DIR, f"{base_name}_preview.mp4")

        print(f"\n[Watcher:{group_name}] Video baru! [{label}] {base_name}")

        success = await loop.run_in_executor(None, download_video, url, hd_path)
        if not success:
            continue

        print(f"   [Watcher] Membuat preview...")
        compressed = await loop.run_in_executor(None, compress_for_preview, hd_path, preview_path)
        if not compressed:
            print(f"   [Watcher] Gagal compress, copy HD sebagai preview")
            shutil.copy2(hd_path, preview_path)

        _enqueue_video(hd_path, preview_path, channel_id, group_name)
        new_videos += 1

    if new_msgs:
        _save_seen(watch_groups)
    if new_videos > 0:
        print(f"[Watcher:{group_name}] {new_videos} video ditambahkan ke antrian")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not IG_USERNAME or not IG_PASSWORD:
        print("[Watcher] IG_USERNAME/IG_PASSWORD kosong, watcher tidak aktif.")
        return

    if not WATCHLIST and not WATCHER_CHANNEL_ID:
        print("[Watcher] WATCHLIST dan WATCHER_CHANNEL_ID kosong, watcher tidak aktif.")
        return

    print("\n[Watcher] Memulai Instagram login...")
    loop = asyncio.get_event_loop()
    try:
        ig_client = await loop.run_in_executor(None, ig_login, IG_USERNAME, IG_PASSWORD)
    except Exception:
        print("[Watcher] Gagal login Instagram. Watcher berhenti.")
        return

    groups = await loop.run_in_executor(None, get_group_threads, ig_client)
    if not groups:
        print("[Watcher] Tidak ada group chat ditemukan.")
        return

    persisted_seen = _load_seen()

    print(f"\n[Watcher] {len(groups)} group chat ditemukan di IG:")
    for i, (tid, name) in enumerate(groups, 1):
        print(f"  [{i}] {name}")
    print()

    watch_groups = []
    for tid, name in groups:
        ch_id = _match_group_channel(name)
        if ch_id:
            old_seen = set(persisted_seen.get(str(tid), []))
            watch_groups.append({
                "thread_id": tid,
                "name": name,
                "channel_id": ch_id,
                "seen": old_seen,
            })

    if not watch_groups:
        print("[Watcher] Tidak ada grup yang cocok dengan WATCHLIST.")
        return

    print(f"[Watcher] {len(watch_groups)} grup di-watch:\n")
    for wg in watch_groups:
        print(f"  [v] {wg['name']}  ->  channel {wg['channel_id']}  ({len(wg['seen'])} seen dari disk)")
    print()

    # Mark existing messages as seen — only process new ones after startup
    print("[Watcher] Menandai semua pesan saat ini sebagai sudah dibaca...")
    total_marked = 0
    for wg in watch_groups:
        try:
            thread = await loop.run_in_executor(
                None, lambda tid=wg["thread_id"]: ig_client.direct_thread(tid, amount=20)
            )
            for msg in thread.messages:
                wg["seen"].add(str(msg.id))
            total_marked += len(thread.messages)
        except Exception as e:
            print(f"[Watcher] Gagal membaca pesan awal ({wg['name']}): {e}")

    _save_seen(watch_groups)
    print(f"[Watcher] {total_marked} pesan di-skip, hanya ambil yang baru setelah worker nyala")
    print(f"[Watcher] Poll interval: {POLL_INTERVAL} detik")
    print(f"[Watcher] Queue file   : {QUEUE_FILE}")
    print(f"[Watcher] Tekan Ctrl+C untuk berhenti\n")

    while True:
        for wg in watch_groups:
            try:
                await poll_new_videos(ig_client, wg, watch_groups)
            except Exception as e:
                print(f"[Watcher] Error polling '{wg['name']}': {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    print("=" * 55)
    print("  Instagram Watcher Worker")
    print("=" * 55)
    print(f"\n[Config] IG User    : {IG_USERNAME or '(kosong)'}")
    if WATCHLIST:
        print(f"[Config] Watchlist  : {len(WATCHLIST)} grup")
        for name, ch in WATCHLIST.items():
            print(f"          -> {name}  ->  ch:{ch}")
    else:
        print(f"[Config] Watchlist  : (kosong)")
    print(f"[Config] Queue File : {QUEUE_FILE}")
    print()
    asyncio.run(main())
