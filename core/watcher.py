"""
Instagram Watcher — polls IG group chats for new videos, sends to Discord.
"""

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone

import discord

from config import (
    IG_USERNAME, IG_PASSWORD, POLL_INTERVAL,
    WATCHER_CHANNEL_ID, WATCHLIST, TEMP_DIR, DISCORD_UPLOAD_LIMIT,
    FILM_CHANNEL_ID, MOTIVATIONAL_CHANNEL_ID, BOLA_GEMING_CHANNEL_ID, DATA_DIR,
)
from services.instagram import (
    ig_login, get_group_threads, extract_video,
    download_video, compress_for_preview,
)
from services.discord_views import (
    WatermarkView, FilmWatermarkView, MotivationalWatermarkView, BolaGemingWatermarkView,
)


# State
bot: discord.Client = None  # type: ignore
ig_client = None
watch_groups: list[dict] = []

SEEN_FILE = os.path.join(DATA_DIR, "watcher_seen.json")


def init(bot_instance: discord.Client):
    global bot
    bot = bot_instance


# ── Seen persistence ─────────────────────────────────────────────────────────

def _load_seen() -> dict[str, list[str]]:
    """Load persisted seen message IDs per thread."""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_seen(watch_groups: list[dict]):
    """Persist seen message IDs for all watch groups."""
    data = {}
    for wg in watch_groups:
        tid = str(wg["thread_id"])
        # Keep only last 200 IDs per thread to avoid file bloat
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


async def send_to_discord(hd_path: str, preview_path: str, channel_id: int | None = None):
    target_ch = channel_id or WATCHER_CHANNEL_ID
    if not target_ch:
        print("[Discord] Channel ID belum diset!")
        return

    channel = bot.get_channel(target_ch)
    if not channel:
        print(f"[Discord] Channel {target_ch} tidak ditemukan!")
        return

    file_size = os.path.getsize(preview_path)
    if file_size > DISCORD_UPLOAD_LIMIT:
        print(f"   [Discord] Preview terlalu besar ({file_size / (1024*1024):.1f} MB), skip")
        return

    try:
        # Pick the right view based on which channel this goes to
        if target_ch == FILM_CHANNEL_ID:
            view = FilmWatermarkView(hd_path, preview_path)
            label = "Film"
        elif target_ch == MOTIVATIONAL_CHANNEL_ID:
            view = MotivationalWatermarkView(hd_path, preview_path)
            label = "Motivational"
        elif target_ch == BOLA_GEMING_CHANNEL_ID:
            view = BolaGemingWatermarkView(hd_path, preview_path)
            label = "BolaGeming"
        else:
            view = WatermarkView(hd_path, preview_path)
            label = "Watcher"

        await channel.send(
            file=discord.File(preview_path, filename=os.path.basename(preview_path)),
            view=view,
        )
        hd_mb = os.path.getsize(hd_path) / (1024 * 1024)
        pv_mb = file_size / (1024 * 1024)
        print(f"   [Discord:{label}] Uploaded preview ({pv_mb:.1f} MB) | HD tersimpan ({hd_mb:.1f} MB)")
    except Exception as e:
        print(f"   [Discord] Gagal upload: {e}")


# ── Polling ───────────────────────────────────────────────────────────────────

async def poll_new_videos(wg: dict):
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

    new_videos = 0
    new_msgs = [msg for msg in thread.messages if str(msg.id) not in seen]
    if new_msgs:
        print(f"[Watcher:{group_name}] {len(new_msgs)} pesan baru terdeteksi")

    for msg in thread.messages:
        msg_id = str(msg.id)
        if msg_id in seen:
            continue

        seen.add(msg_id)

        url, label = await loop.run_in_executor(
            None, extract_video, ig_client, msg
        )
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
        compressed = await loop.run_in_executor(
            None, compress_for_preview, hd_path, preview_path
        )
        if not compressed:
            print(f"   [Watcher] Gagal compress, upload HD langsung")
            shutil.copy2(hd_path, preview_path)

        await send_to_discord(hd_path, preview_path, channel_id)
        new_videos += 1

    # Persist seen set after each poll cycle
    if new_msgs:
        _save_seen(watch_groups)

    if new_videos > 0:
        print(f"[Watcher:{group_name}] {new_videos} video baru diproses")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def ig_watcher_loop():
    global ig_client, watch_groups

    await bot.wait_until_ready()
    await asyncio.sleep(2)

    if not IG_USERNAME or not IG_PASSWORD:
        print("[Watcher] IG_USERNAME/IG_PASSWORD kosong, watcher tidak aktif.")
        return

    if not WATCHLIST and not WATCHER_CHANNEL_ID:
        print("[Watcher] WATCHLIST dan WATCHER_CHANNEL_ID kosong, watcher tidak aktif.")
        return

    print("\n[Watcher] Memulai Instagram login...")
    try:
        ig_client = await asyncio.get_event_loop().run_in_executor(
            None, ig_login, IG_USERNAME, IG_PASSWORD
        )
    except Exception:
        print("[Watcher] Gagal login Instagram. Watcher berhenti.")
        return

    groups = await asyncio.get_event_loop().run_in_executor(
        None, get_group_threads, ig_client
    )
    if not groups:
        print("[Watcher] Tidak ada group chat ditemukan.")
        return

    # Load persisted seen IDs
    persisted_seen = _load_seen()

    if WATCHLIST:
        print(f"\n[Watcher] {len(groups)} group chat ditemukan di IG:")
        for i, (tid, name) in enumerate(groups, 1):
            print(f"  [{i}] {name}")
        print()

        watch_groups = []
        for tid, name in groups:
            ch_id = _match_group_channel(name)
            if ch_id:
                # Restore persisted seen IDs for this thread
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
            restored = len(wg["seen"])
            print(f"  [v] {wg['name']}  ->  channel {wg['channel_id']}  ({restored} seen dari disk)")
        print()

    else:
        print(f"\n[Watcher] {len(groups)} group chat ditemukan:\n")
        for i, (_, name) in enumerate(groups, 1):
            print(f"  [{i}] {name}")
        print()

        choice = await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("Pilih nomor grup untuk di-watch: ").strip()
        )
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(groups):
                tid, name = groups[idx]
            else:
                print("[Watcher] Nomor tidak valid.")
                return
        except ValueError:
            print("[Watcher] Input tidak valid.")
            return

        watch_groups = [{
            "thread_id": tid,
            "name": name,
            "channel_id": WATCHER_CHANNEL_ID,
            "seen": set(persisted_seen.get(str(tid), [])),
        }]

    print(f"[Watcher] Poll interval: {POLL_INTERVAL} detik")
    print(f"[Watcher] Tekan Ctrl+C untuk berhenti\n")

    # Mark all current messages as seen — only process messages posted AFTER bot starts
    print("[Watcher] Menandai semua pesan saat ini sebagai sudah dibaca...")
    total_marked = 0
    for wg in watch_groups:
        try:
            thread = await asyncio.get_event_loop().run_in_executor(
                None, lambda tid=wg["thread_id"]: ig_client.direct_thread(tid, amount=20)
            )
            for msg in thread.messages:
                wg["seen"].add(str(msg.id))
            total_marked += len(thread.messages)
        except Exception as e:
            print(f"[Watcher] Gagal membaca pesan awal ({wg['name']}): {e}")

    _save_seen(watch_groups)
    print(f"[Watcher] {total_marked} pesan di-skip, hanya ambil yang baru setelah bot nyala\n")

    # Polling loop
    while not bot.is_closed():
        for wg in watch_groups:
            try:
                await poll_new_videos(wg)
            except Exception as e:
                print(f"[Watcher] Error polling '{wg['name']}': {e}")
        await asyncio.sleep(POLL_INTERVAL)
