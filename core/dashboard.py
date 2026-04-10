"""
YouTube Dashboard — real-time embed auto-update in Discord.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import discord

from config import (
    DASHBOARD_CHANNEL_ID, DASHBOARD_INTERVAL, DASHBOARD_FILE, WITA,
    WATERMARK_CHANNEL_ID, COPYRIGHT_CHANNEL_ID,
    SCHEDULE_FILE, SCHEDULE_FILM_FILE, SCHEDULE_MOTIVATIONAL_FILE, SCHEDULE_BOLA_GEMING_FILE,
    YT_BOLA_GEMING_CLIENT_SECRET, YT_BOLA_GEMING_TOKEN_FILE,
)
from services.youtube import load_upload_history, save_upload_history, fetch_video_stats, check_copyright_blocks, delete_video, load_upload_queue, reschedule_video, get_next_schedule_time


# State
bot: discord.Client = None  # type: ignore
dashboard_msg_ids: list[int] = []


def init(bot_instance: discord.Client):
    global bot
    bot = bot_instance


# ── Persistence ───────────────────────────────────────────────────────────────

def load_dashboard_msg_ids() -> list[int]:
    if os.path.exists(DASHBOARD_FILE):
        try:
            with open(DASHBOARD_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_dashboard_msg_ids(ids: list[int]):
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, 'w') as f:
        json.dump(ids, f)


# ── Build embeds ──────────────────────────────────────────────────────────────

CHANNEL_CONFIG = {
    "wanderingwithme": {"title": "Wanderingwithme", "color": 0x1DB954, "emoji": "🌍"},
    "film":            {"title": "Film",             "color": 0xE50914, "emoji": "🎬"},
    "motivational":    {"title": "Motivational",     "color": 0xFFD700, "emoji": "💪"},
    "bola_geming":     {"title": "Bola Geming",      "color": 0x00AA44, "emoji": "⚽"},
}


def _format_time_remaining(now, scheduled_at) -> str:
    delta = scheduled_at - now
    total_sec = int(delta.total_seconds())
    if total_sec < 0:
        return "sebentar lagi"
    elif total_sec < 3600:
        return f"{total_sec // 60}m lagi"
    elif total_sec < 86400:
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        return f"{h}j {m}m lagi"
    else:
        d = total_sec // 86400
        h = (total_sec % 86400) // 3600
        return f"{d}h {h}j lagi"


def _build_channel_embed(channel_type: str, entries: list[dict], now: datetime) -> discord.Embed:
    cfg = CHANNEL_CONFIG.get(channel_type, {"title": channel_type.title(), "color": 0x2B2D31, "emoji": ""})

    upcoming = [e for e in entries if e["scheduled_at"] > now]
    published = [e for e in entries if e["scheduled_at"] <= now]
    upcoming.sort(key=lambda x: x["scheduled_at"])
    published.sort(key=lambda x: x["views"], reverse=True)

    total_views = sum(e["views"] for e in published)
    total_likes = sum(e["likes"] for e in published)
    total_comments = sum(e["comments"] for e in published)

    embed = discord.Embed(
        title=f"{cfg['emoji']} {cfg['title']}",
        color=cfg["color"],
    )
    embed.description = f"**{len(entries)}** video"

    embed.add_field(name="Views", value=f"**{total_views:,}**", inline=True)
    embed.add_field(name="Likes", value=f"**{total_likes:,}**", inline=True)
    embed.add_field(name="Comments", value=f"**{total_comments:,}**", inline=True)

    # Top published videos
    if published:
        lines = []
        for i, e in enumerate(published[:5], 1):
            lines.append(
                f"`{i}.` **{e['title']}** -- "
                f"{e['views']:,} views | {e['likes']:,} likes"
            )
        embed.add_field(
            name=f"Top Videos ({len(published)})",
            value="\n".join(lines),
            inline=False,
        )

    # Upcoming
    if upcoming:
        lines = []
        for i, e in enumerate(upcoming[:5], 1):
            sched_str = e["scheduled_at"].strftime("%d/%m %H:%M")
            time_str = _format_time_remaining(now, e["scheduled_at"])
            lines.append(
                f"`{i}.` **{e['title']}**\n"
                f"    {sched_str} WITA -- *{time_str}*"
            )
        embed.add_field(
            name=f"Akan Tayang ({len(upcoming)})",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Akan Tayang",
            value="*Tidak ada video dijadwalkan.*",
            inline=False,
        )

    return embed


def build_dashboard_embeds() -> list[discord.Embed]:
    history = load_upload_history()
    now = datetime.now(WITA)
    embeds = []

    if not history:
        embed = discord.Embed(
            title="YouTube Dashboard",
            description=(
                "Belum ada video yang di-upload ke YouTube.\n\n"
                f"*Last updated: {now.strftime('%d %b %Y, %H:%M:%S WITA')}*"
            ),
            color=0x2B2D31,
        )
        queue = load_upload_queue()
        return [embed, _build_queue_embed(queue, now)]

    # Group by channel_type
    by_channel: dict[str, list[dict]] = {}
    for h in history:
        scheduled_at = datetime.fromisoformat(h["scheduled_at"])
        entry = {
            "video_id": h["video_id"],
            "title": h["title"],
            "scheduled_at": scheduled_at,
            "uploaded_at": datetime.fromisoformat(h["uploaded_at"]),
            "views": h.get("views", 0),
            "likes": h.get("likes", 0),
            "comments": h.get("comments", 0),
        }
        ct = h.get("channel_type", "wanderingwithme")
        by_channel.setdefault(ct, []).append(entry)

    # Summary embed
    total_videos = len(history)
    total_views = sum(h.get("views", 0) for h in history)
    total_likes = sum(h.get("likes", 0) for h in history)
    total_comments = sum(h.get("comments", 0) for h in history)

    summary = discord.Embed(title="YouTube Dashboard", color=0xFF0000)
    summary.description = (
        f"**{total_videos}** video total | "
        f"**{len(by_channel)}** channel\n"
        f"*Last updated: {now.strftime('%d %b %Y, %H:%M:%S WITA')}*"
    )
    summary.add_field(name="Total Views", value=f"**{total_views:,}**", inline=True)
    summary.add_field(name="Total Likes", value=f"**{total_likes:,}**", inline=True)
    summary.add_field(name="Total Comments", value=f"**{total_comments:,}**", inline=True)

    # Per-channel summary in the overview
    for ct in ("wanderingwithme", "film", "motivational", "bola_geming"):
        entries = by_channel.get(ct, [])
        cfg = CHANNEL_CONFIG.get(ct, {"emoji": "", "title": ct.title()})
        views = sum(e["views"] for e in entries)
        summary.add_field(
            name=f"{cfg['emoji']} {cfg['title']}",
            value=f"{len(entries)} video | {views:,} views",
            inline=True,
        )

    embeds.append(summary)

    # Per-channel embeds
    for ct in ("wanderingwithme", "film", "motivational", "bola_geming"):
        entries = by_channel.get(ct, [])
        embeds.append(_build_channel_embed(ct, entries, now))

    # Queue embed — always shown (shows "kosong" if empty)
    queue = load_upload_queue()
    embeds.append(_build_queue_embed(queue, now))

    return embeds


def _build_queue_embed(queue: list[dict], now: datetime) -> discord.Embed:
    embed = discord.Embed(
        title="⏳ Upload Queue — Menunggu Quota Reset",
        color=0xFF8C00,
    )

    # Estimate next quota reset: 16:00 WITA today, or tomorrow if already past
    reset_today = now.replace(hour=16, minute=0, second=0, microsecond=0)
    reset_time = reset_today if reset_today > now else reset_today + timedelta(days=1)
    time_to_reset = _format_time_remaining(now, reset_time)
    reset_str = reset_time.strftime("%d %b %Y, %H:%M WITA")

    embed.description = (
        f"**{len(queue)}** video menunggu quota reset\n"
        f"Estimasi reset: **{reset_str}** (*{time_to_reset}*)"
    )

    lines = []
    for i, item in enumerate(queue, 1):
        ch_type = item.get("channel_type", "wanderingwithme")
        cfg = CHANNEL_CONFIG.get(ch_type, {"emoji": "📹", "title": ch_type.title()})
        queued_at_str = ""
        try:
            queued_at = datetime.fromisoformat(item["queued_at"])
            queued_at_str = queued_at.strftime("%d/%m %H:%M")
        except Exception:
            pass
        lines.append(
            f"`{i}.` {cfg['emoji']} **{item['title']}**\n"
            f"    Channel: {cfg['title']} | Masuk antrian: {queued_at_str}"
        )

    embed.add_field(
        name="Video dalam Antrian",
        value="\n".join(lines) if lines else "*Kosong*",
        inline=False,
    )
    embed.set_footer(text=f"Queue diproses otomatis setiap jam • Updated {now.strftime('%H:%M:%S WITA')}")
    return embed


# ── Refresh & update ──────────────────────────────────────────────────────────

async def refresh_dashboard_stats():
    history = load_upload_history()
    if not history:
        return

    video_ids = [h["video_id"] for h in history]
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(None, fetch_video_stats, video_ids)

    if not stats:
        return

    changed = False
    for h in history:
        s = stats.get(h["video_id"])
        if s:
            h["views"] = s.get("views", h.get("views", 0))
            h["likes"] = s.get("likes", h.get("likes", 0))
            h["comments"] = s.get("comments", h.get("comments", 0))
            changed = True

    if changed:
        save_upload_history(history)


async def update_dashboard():
    global dashboard_msg_ids

    if not DASHBOARD_CHANNEL_ID:
        return

    channel = bot.get_channel(DASHBOARD_CHANNEL_ID)
    if not channel:
        return

    embeds = build_dashboard_embeds()

    if dashboard_msg_ids:
        try:
            existing_msgs = []
            for msg_id in dashboard_msg_ids:
                try:
                    msg = await channel.fetch_message(msg_id)
                    existing_msgs.append(msg)
                except discord.NotFound:
                    existing_msgs = []
                    break

            if existing_msgs and len(existing_msgs) == len(embeds):
                for i, (msg, embed) in enumerate(zip(existing_msgs, embeds)):
                    await msg.edit(embed=embed)
                    if i < len(embeds) - 1:
                        await asyncio.sleep(2)
                print(f"[Dashboard] Updated ({datetime.now(WITA).strftime('%H:%M:%S')})")
                return
            else:
                # Count mismatch, delete old and resend
                for msg in existing_msgs:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                dashboard_msg_ids = []

        except Exception as e:
            print(f"[Dashboard] Gagal edit, kirim ulang: {e}")

    # Send new
    try:
        async for old_msg in channel.history(limit=20):
            if old_msg.author == bot.user:
                try:
                    await old_msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

    dashboard_msg_ids = []
    for embed in embeds:
        msg = await channel.send(embed=embed)
        dashboard_msg_ids.append(msg.id)

    save_dashboard_msg_ids(dashboard_msg_ids)
    print(f"[Dashboard] Created ({len(embeds)} embeds)")


async def check_and_handle_copyright():
    """Check all videos for copyright blocks, delete them, notify watermark channel."""
    loop = asyncio.get_event_loop()
    history = load_upload_history()
    if not history:
        return

    blocked = await loop.run_in_executor(None, check_copyright_blocks, history)
    if not blocked:
        return

    print(f"[Copyright] {len(blocked)} video terdeteksi blocked!")

    alert_ch_id = COPYRIGHT_CHANNEL_ID or WATERMARK_CHANNEL_ID
    wm_channel = bot.get_channel(alert_ch_id) if alert_ch_id else None
    removed_ids = set()

    for entry in blocked:
        vid = entry["video_id"]
        title = entry.get("title", vid)
        channel_type = entry.get("channel_type", "wanderingwithme")
        reason = entry.get("block_reason", "unknown")

        print(f"[Copyright] Blocked: {title} ({vid}) — reason: {reason}")

        # Choose credentials per channel
        from config import YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE, YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE
        if channel_type == "film":
            cs, tf = YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE
        elif channel_type == "motivational":
            cs, tf = YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE
        elif channel_type == "bola_geming":
            cs, tf = YT_BOLA_GEMING_CLIENT_SECRET, YT_BOLA_GEMING_TOKEN_FILE
        else:
            cs, tf = None, None

        should_delete = entry.get("auto_delete", False) or reason == "not_found"
        privacy_status = entry.get("privacy_status", "")

        # Delete from YouTube only if: already gone, auto_delete flagged (unpublished + restricted)
        deleted = False
        if reason == "not_found":
            deleted = True  # already gone
        elif should_delete:
            deleted = await loop.run_in_executor(None, delete_video, vid, cs, tf)
            if deleted:
                print(f"[Copyright] Auto-deleted (belum publish, kena restriction): {title[:50]}")

        removed_ids.add(vid)

        # Notify watermark channel
        if wm_channel:
            reason_text = {
                "rejected:copyright": "ditolak karena **copyright**",
                "rejected:claim": "kena **copyright claim**",
                "rejected:duplicate": "ditolak karena **duplikat**",
                "rejected:termsOfUse": "ditolak karena **pelanggaran ToS**",
                "rejected:inappropriate": "ditolak karena **konten tidak sesuai**",
                "not_found": "hilang/dihapus dari YouTube",
                "copyright_claim:content_id": "kena **Content ID claim** (Partially blocked / Copyright)",
                "copyright_claim:forced_license": "kena **copyright claim** (forced license)",
                "still_private_after_publish": "tetap **private** setelah jadwal tayang **(Shorts policy / copyright)**",
            }.get(reason) or (
                f"kena **Content ID / Shorts Policy block** ({reason.split(':')[1]})" if reason.startswith("region_blocked:") else f"blocked: {reason}"
            )

            if reason == "not_found":
                action_text = "🗑️ Sudah dihapus dari YouTube oleh platform"
            elif should_delete and deleted:
                action_text = "✅ **Auto-dihapus** — video belum tayang, sudah dihapus otomatis"
            elif should_delete and not deleted:
                action_text = "⚠️ Gagal dihapus otomatis, cek manual di YouTube Studio"
            else:
                action_text = f"ℹ️ Video sudah tayang (public) — perlu tindakan manual di YouTube Studio"

            embed = discord.Embed(
                title="⚠️ Video Kena Restrictions",
                color=0xFF4444,
            )
            embed.add_field(name="Title", value=title, inline=False)
            embed.add_field(name="Channel", value=channel_type.title(), inline=True)
            embed.add_field(name="Status Video", value=privacy_status or "unknown", inline=True)
            embed.add_field(name="Video ID", value=f"[{vid}](https://youtu.be/{vid})", inline=False)
            embed.add_field(name="Restriction", value=reason_text, inline=False)
            embed.add_field(name="Tindakan", value=action_text, inline=False)
            embed.timestamp = datetime.now(WITA)
            try:
                await wm_channel.send(embed=embed)
            except Exception as e:
                print(f"[Copyright] Gagal kirim notif: {e}")

    # Remove blocked videos from history and reclaim schedule slots
    if removed_ids:
        # Collect which channel_types had future slots freed
        now = datetime.now(WITA)
        freed_by_channel: dict[str, datetime | None] = {}
        for entry in blocked:
            if entry["video_id"] not in removed_ids:
                continue
            try:
                sched = datetime.fromisoformat(entry.get("scheduled_at", ""))
                if sched > now:
                    ct = entry.get("channel_type", "wanderingwithme")
                    freed_by_channel.setdefault(ct, None)
            except Exception:
                pass

        new_history = [h for h in history if h["video_id"] not in removed_ids]
        save_upload_history(new_history)
        print(f"[Copyright] {len(removed_ids)} video dihapus dari history")

        # Reclaim: set last_scheduled to the latest remaining scheduled video per channel
        SCHEDULE_MAP = {
            "wanderingwithme": SCHEDULE_FILE,
            "film": SCHEDULE_FILM_FILE,
            "motivational": SCHEDULE_MOTIVATIONAL_FILE,
            "bola_geming": SCHEDULE_BOLA_GEMING_FILE,
        }
        for ct in freed_by_channel:
            sched_file = SCHEDULE_MAP.get(ct)
            if not sched_file:
                continue

            remaining = [h for h in new_history if h.get("channel_type", "wanderingwithme") == ct]
            if remaining:
                # Set last_scheduled to the latest scheduled_at among remaining videos
                latest = max(
                    datetime.fromisoformat(h["scheduled_at"])
                    for h in remaining if h.get("scheduled_at")
                )
                with open(sched_file, 'w') as f:
                    json.dump({"last_scheduled": latest.isoformat()}, f)
                print(f"[Copyright] Slot reclaimed untuk {ct}: last_scheduled -> {latest.strftime('%d %b %H:%M WITA')}")
            else:
                # No remaining videos — reset schedule so next upload starts fresh
                if os.path.exists(sched_file):
                    os.remove(sched_file)
                print(f"[Copyright] Schedule {ct} di-reset (tidak ada video tersisa)")


# ── Duplicate Schedule Detection ──────────────────────────────────────────────

# Channel mapping for duplicate schedule alerts
SCHEDULE_ALERT_CHANNELS = {
    "wanderingwithme": 1483820632717394132,
    "film":            1485607366778159104,
    "motivational":    1485609389904629860,
    "bola_geming":     1491056906348007635,
}

# Track already-alerted duplicates to avoid spam
_alerted_duplicates: set[str] = set()


async def check_duplicate_schedules():
    """Check if any videos are scheduled at the same date+hour, auto-fix by rescheduling."""
    history = load_upload_history()
    if not history:
        return

    now = datetime.now(WITA)
    loop = asyncio.get_event_loop()
    history_modified = False

    # Credential mapping per channel type
    from config import YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE, YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE
    CREDS = {
        "wanderingwithme": (None, None, SCHEDULE_FILE),
        "film": (YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE, SCHEDULE_FILM_FILE),
        "motivational": (YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE, SCHEDULE_MOTIVATIONAL_FILE),
        "bola_geming": (YT_BOLA_GEMING_CLIENT_SECRET, YT_BOLA_GEMING_TOKEN_FILE, SCHEDULE_BOLA_GEMING_FILE),
    }

    for channel_type, alert_ch_id in SCHEDULE_ALERT_CHANNELS.items():
        entries = [h for h in history if h.get("channel_type", "wanderingwithme") == channel_type]

        # Group by schedule slot (date + hour)
        slots: dict[str, list[dict]] = {}
        for h in entries:
            sched_str = h.get("scheduled_at")
            if not sched_str:
                continue
            try:
                sched = datetime.fromisoformat(sched_str)
                if sched > now:
                    slot_key = sched.strftime("%Y-%m-%d %H:00")
                    slots.setdefault(slot_key, []).append(h)
            except Exception:
                pass

        # Find and fix duplicates
        for slot_key, vids in slots.items():
            if len(vids) <= 1:
                continue

            dup_key = f"{channel_type}:{slot_key}:" + ",".join(sorted(v["video_id"] for v in vids))
            if dup_key in _alerted_duplicates:
                continue

            _alerted_duplicates.add(dup_key)

            cs, tf, sched_file = CREDS.get(channel_type, (None, None, SCHEDULE_FILE))

            # Keep the first video, reschedule the rest
            keep = vids[0]
            to_reschedule = vids[1:]

            channel = bot.get_channel(alert_ch_id)

            for vid_entry in to_reschedule:
                vid_id = vid_entry["video_id"]
                # Get next available slot
                new_time = get_next_schedule_time(
                    schedule_file=sched_file,
                    channel_type=channel_type,
                )

                # Reschedule on YouTube
                success = await loop.run_in_executor(
                    None, reschedule_video, vid_id, new_time, cs, tf
                )

                if success:
                    # Update history entry
                    for h in history:
                        if h["video_id"] == vid_id:
                            old_time = h.get("scheduled_at", "?")
                            h["scheduled_at"] = new_time.isoformat()
                            break
                    history_modified = True

                    # Send notification
                    if channel:
                        embed = discord.Embed(
                            title="🔄 Jadwal Duplikat — Auto-diperbaiki",
                            color=0x00CC66,
                        )
                        embed.add_field(name="Video", value=vid_entry.get("title", vid_id)[:50], inline=False)
                        embed.add_field(name="Channel", value=channel_type.title(), inline=True)
                        embed.add_field(name="Jadwal Lama", value=slot_key + " WITA", inline=True)
                        embed.add_field(name="Jadwal Baru", value=new_time.strftime("%Y-%m-%d %H:%M") + " WITA", inline=True)
                        embed.timestamp = datetime.now(WITA)
                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"[Schedule] Gagal kirim notif: {e}")

                    print(f"[Schedule] Auto-fix: {vid_entry.get('title','?')[:40]} dijadwal ulang ke {new_time.strftime('%d %b %H:%M WITA')}")
                else:
                    # Failed to reschedule — alert manually
                    if channel:
                        embed = discord.Embed(
                            title="⚠️ Jadwal Duplikat — Gagal Auto-fix",
                            description="Perlu dijadwal ulang manual di YouTube Studio",
                            color=0xFF4444,
                        )
                        embed.add_field(name="Video", value=vid_entry.get("title", vid_id)[:50], inline=False)
                        embed.add_field(name="Jadwal Bentrok", value=slot_key + " WITA", inline=True)
                        embed.timestamp = datetime.now(WITA)
                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"[Schedule] Gagal kirim alert: {e}")

    if history_modified:
        save_upload_history(history)


async def dashboard_loop():
    global dashboard_msg_ids

    await bot.wait_until_ready()
    await asyncio.sleep(5)

    dashboard_msg_ids = load_dashboard_msg_ids()

    if not DASHBOARD_CHANNEL_ID:
        print("[Dashboard] DASHBOARD_CHANNEL_ID tidak diset, dashboard nonaktif.")
        return

    print(f"[Dashboard] Real-time dashboard aktif (update setiap {DASHBOARD_INTERVAL}s)")

    while not bot.is_closed():
        try:
            await refresh_dashboard_stats()
            await check_and_handle_copyright()
            await check_duplicate_schedules()
            await update_dashboard()

        except Exception as e:
            print(f"[Dashboard] Error: {e}")

        await asyncio.sleep(DASHBOARD_INTERVAL)
