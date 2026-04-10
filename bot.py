"""
IG Watcher + Watermark + YouTube Bot
=====================================
Entry point — thin file that wires all modules together.

Setup:
    1. pip install -r requirements.txt
    2. Copy .env.example to .env and fill in your values
    3. Place client_secret.json (YouTube OAuth) in this folder
    4. python bot.py
"""

import io
import os
import sys

# Force UTF-8 stdout/stderr so emojis in print() don't crash on Windows (cp1252)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# ── Single-instance lock ──────────────────────────────────────────────────────
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.lock")

def _acquire_lock():
    """Prevent multiple bot instances from running at the same time."""
    import atexit
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if that PID is still alive
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                print(f"[ERROR] Bot sudah berjalan (PID {old_pid}). Hentikan instance lama dulu.")
                sys.exit(1)
        except Exception:
            pass  # stale lock — overwrite it
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

from config import (
    DISCORD_TOKEN, IG_USERNAME,
    WATCHER_CHANNEL_ID, WATERMARK_CHANNEL_ID, WATERMARK_PATH, WM_USERNAME,
    DASHBOARD_CHANNEL_ID,
    GEMINI_API_KEYS, YT_CLIENT_SECRET, POLL_INTERVAL, WATCHLIST,
    VIDEO_EXTENSIONS, MAX_FILE_MB, DISCORD_UPLOAD_LIMIT, TEMP_DIR,
)
from services import discord_views
from services.discord_views import ConfirmView, pending_jobs, restore_persistent_views, save_pending_jobs, run_ffmpeg_queued
from services.youtube import (
    load_upload_queue, save_upload_queue,
    upload_to_youtube, get_next_schedule_time, add_to_history, is_quota_exceeded,
)
from core import watcher
from processor import process_video

WATCHER_QUEUE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "watcher_queue.json")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD CLIENT
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# Inject bot instance into modules
discord_views.init(bot)
watcher.init(bot)


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOAD QUEUE PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

async def upload_queue_loop():
    """
    Background loop — processes the YouTube upload queue every hour.
    YouTube quota resets at midnight PST (~15:00–16:00 WITA).
    Notifies DASHBOARD_CHANNEL_ID when items are uploaded from queue.
    """
    import asyncio as _asyncio
    await bot.wait_until_ready()
    await _asyncio.sleep(10)  # small delay after startup

    while not bot.is_closed():
        queue = load_upload_queue()
        if queue:
            print(f"[Queue] {len(queue)} item di antrian, mencoba upload...")
            loop = _asyncio.get_event_loop()
            remaining = []
            succeeded = []

            for item in queue:
                hq_path = item.get("hq_path", "")
                if not hq_path or not os.path.exists(hq_path):
                    print(f"[Queue] File tidak ada, skip: {hq_path}")
                    continue  # drop item — file gone

                try:
                    schedule_time = await loop.run_in_executor(
                        None, get_next_schedule_time,
                        item.get("schedule_file"),
                        item.get("start_hour", 0),
                        item.get("channel_type"),
                    )
                    video_id = await loop.run_in_executor(
                        None, upload_to_youtube,
                        hq_path, item["title"], item["description"],
                        schedule_time, item.get("yt_category_id", "22"),
                        item.get("yt_client_secret"), item.get("yt_token_file"),
                    )
                    add_to_history(video_id, item["title"], schedule_time, item.get("channel_type", "wanderingwithme"))
                    try:
                        os.remove(hq_path)
                    except OSError:
                        pass
                    succeeded.append((item["title"], video_id, schedule_time, item.get("channel_type", "")))
                    print(f"[Queue] Upload berhasil: {item['title']}")

                except Exception as exc:
                    if is_quota_exceeded(exc):
                        print(f"[Queue] Quota masih habis, coba lagi nanti: {item['title']}")
                        remaining.append(item)
                    else:
                        print(f"[Queue] Upload gagal (non-quota), drop: {item['title']} — {exc}")

            save_upload_queue(remaining)

            # Notify Discord if any uploads succeeded
            if succeeded and DASHBOARD_CHANNEL_ID:
                ch = bot.get_channel(DASHBOARD_CHANNEL_ID)
                if ch:
                    lines = [f"✅ **Queue upload selesai** — {len(succeeded)} video berhasil diupload:\n"]
                    for title, vid, sched, ch_type in succeeded:
                        sched_str = sched.strftime("%d %b %Y, %H:%M WITA")
                        lines.append(f"• **{title}** [{ch_type}] — https://youtu.be/{vid} (Scheduled: {sched_str})")
                    try:
                        await ch.send("\n".join(lines))
                    except Exception:
                        pass

        await _asyncio.sleep(3600)  # check every hour


# ══════════════════════════════════════════════════════════════════════════════
#  WATCHER QUEUE READER
# ══════════════════════════════════════════════════════════════════════════════

async def queue_reader_loop():
    """
    Background loop — reads videos queued by watcher_worker.py every 10 seconds
    and sends them to Discord with the appropriate watermark view.
    """
    import asyncio as _asyncio
    import json as _json
    await bot.wait_until_ready()
    await _asyncio.sleep(5)

    while not bot.is_closed():
        try:
            if os.path.exists(WATCHER_QUEUE_FILE):
                with open(WATCHER_QUEUE_FILE, 'r') as f:
                    queue = _json.load(f)

                if queue:
                    remaining = []
                    for item in queue:
                        hd_path = item.get("hd_path", "")
                        preview_path = item.get("preview_path", "")
                        channel_id = item.get("channel_id")
                        group_name = item.get("group_name", "?")

                        if not hd_path or not os.path.exists(hd_path):
                            print(f"[QueueReader] File tidak ada, skip: {hd_path}")
                            continue
                        if not preview_path or not os.path.exists(preview_path):
                            print(f"[QueueReader] Preview tidak ada, skip: {preview_path}")
                            continue

                        try:
                            await watcher.send_to_discord(hd_path, preview_path, channel_id)
                            print(f"[QueueReader] Dikirim ke Discord: {group_name}")
                        except Exception as e:
                            print(f"[QueueReader] Gagal kirim ke Discord ({group_name}): {e}")
                            remaining.append(item)

                    with open(WATCHER_QUEUE_FILE, 'w') as f:
                        _json.dump(remaining, f, indent=2)

        except Exception as e:
            print(f"[QueueReader] Error: {e}")

        await _asyncio.sleep(10)


# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"\n[Bot] Online sebagai {bot.user} (ID: {bot.user.id})")
    if WATCHER_CHANNEL_ID:
        print(f"[Bot] Watcher channel : {WATCHER_CHANNEL_ID}")
    if WATERMARK_CHANNEL_ID:
        print(f"[Bot] Watermark channel: {WATERMARK_CHANNEL_ID}")
    if GEMINI_API_KEYS:
        print(f"[Bot] Gemini API      : {len(GEMINI_API_KEYS)} keys (rotasi)")
    if os.path.exists(YT_CLIENT_SECRET):
        print(f"[Bot] YouTube         : client_secret.json found")
    if DASHBOARD_CHANNEL_ID:
        print(f"[Bot] Dashboard channel: {DASHBOARD_CHANNEL_ID}")

    # Restore persistent views (watermark buttons survive restart)
    restore_persistent_views()

    bot.loop.create_task(queue_reader_loop())
    bot.loop.create_task(upload_queue_loop())


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not WATERMARK_CHANNEL_ID:
        return
    if message.channel.id != WATERMARK_CHANNEL_ID:
        return

    # Find video attachment
    video_att = None
    for att in message.attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext in VIDEO_EXTENSIONS or (att.content_type and "video" in att.content_type):
            video_att = att
            break

    if video_att is None:
        return

    title = message.content.strip()
    if not title:
        await message.reply(
            "Ketik **title** bersamaan dengan video.\n"
            "Contoh:  `Swiss in Summer`  *(lalu attach video)*"
        )
        return

    if video_att.size > MAX_FILE_MB * 1024 * 1024:
        await message.reply(f"Video terlalu besar (max {MAX_FILE_MB} MB).")
        return

    status = await message.reply("⏳ Menunggu antrian...")

    ext_str = os.path.splitext(video_att.filename)[1] or ".mp4"
    input_path = os.path.join(TEMP_DIR, f"wm_{message.id}_in{ext_str}")
    compressed_path = os.path.join(TEMP_DIR, f"wm_{message.id}_preview.mp4")

    try:
        await video_att.save(input_path)

        import asyncio
        loop = asyncio.get_event_loop()
        await run_ffmpeg_queued(
            loop, process_video,
            input_path, compressed_path, title,
            WATERMARK_PATH, WM_USERNAME, 32,
            status_msg=status, label="watermark preview",
        )

        file_size = os.path.getsize(compressed_path)
        view = ConfirmView(message.id, message.author.id)

        await status.delete()

        wm_msg_ids = [message.id]

        if file_size <= DISCORD_UPLOAD_LIMIT:
            preview_msg = await message.channel.send(
                f"**{title}** — Sudah sesuai?",
                file=discord.File(compressed_path, filename="preview.mp4"),
                view=view,
                reference=message,
            )
            wm_msg_ids.append(preview_msg.id)
        else:
            size_mb = file_size / (1024 * 1024)
            preview_msg = await message.channel.send(
                f"**{title}** ({size_mb:.1f} MB) — Sudah sesuai?\n"
                "Preview terlalu besar untuk ditampilkan.",
                view=view,
                reference=message,
            )
            wm_msg_ids.append(preview_msg.id)

        pending_jobs[message.id] = {
            "input_path": input_path,
            "title": title,
            "compressed_path": compressed_path,
            "wm_msg_ids": wm_msg_ids,
            "author_id": message.author.id,
            "view_type": "confirm",
        }
        save_pending_jobs()

    except Exception as exc:
        error_msg = str(exc)[:1500]
        await status.edit(content=f"Error:\n```\n{error_msg}\n```")
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(compressed_path):
            os.remove(compressed_path)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print("  IG Watcher + Watermark + YouTube Bot")
    print("=" * 55)

    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN harus diisi di .env!")
        return

    print(f"\n[Config] IG User        : {IG_USERNAME or '(kosong)'}")
    print(f"[Config] Watcher Ch     : {WATCHER_CHANNEL_ID or '(kosong)'}")
    print(f"[Config] Watermark Ch   : {WATERMARK_CHANNEL_ID or '(kosong)'}")
    if GEMINI_API_KEYS:
        print(f"[Config] Gemini API     : {len(GEMINI_API_KEYS)} keys (2.5 Flash)")
    else:
        print(f"[Config] Gemini API     : (kosong)")
    print(f"[Config] YouTube        : {'Ready' if os.path.exists(YT_CLIENT_SECRET) else '(no client_secret.json)'}")
    print(f"[Config] Poll Interval  : {POLL_INTERVAL}s")
    print(f"[Config] Queue Reader   : polling setiap 10s dari watcher_worker.py")
    if WATCHLIST:
        print(f"[Config] Watchlist      : {len(WATCHLIST)} grup")
        for name, ch in WATCHLIST.items():
            print(f"          -> {name}  ->  ch:{ch}")
    print()

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
