"""
Discord UI components — Views, Modals, Buttons for watermark workflow.
Persistent views survive bot restarts as long as local files still exist.
"""

import asyncio
import json
import os
import zlib

import discord

from config import (
    TEMP_DIR, OUTPUT_DIR, WATERMARK_CHANNEL_ID,
    WATERMARK_PATH, WM_USERNAME, YT_CATEGORY_ID,
    DISCORD_UPLOAD_LIMIT,
    FILM_WATERMARK_PATH, FILM_CHANNEL_ID,
    MOTIVATIONAL_WATERMARK_PATH, MOTIVATIONAL_CHANNEL_ID,
    BOLA_GEMING_WATERMARK_PATH, BOLA_GEMING_CHANNEL_ID,
    YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE, YT_FILM_CATEGORY_ID,
    YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE, YT_MOTIVATIONAL_CATEGORY_ID,
    YT_BOLA_GEMING_CLIENT_SECRET, YT_BOLA_GEMING_TOKEN_FILE, YT_BOLA_GEMING_CATEGORY_ID,
    SCHEDULE_FILM_FILE, SCHEDULE_MOTIVATIONAL_FILE, SCHEDULE_BOLA_GEMING_FILE,
    PENDING_JOBS_FILE,
)
from services.youtube import (
    upload_to_youtube, get_next_schedule_time, add_to_history,
    is_quota_exceeded, add_to_upload_queue,
)
from services.gemini import generate_description, generate_title_and_description
from processor import process_video
from processor_overlay import process_film, process_motivational, process_bola_geming


# Shared state (set by bot.py at startup)
bot: discord.Client = None  # type: ignore
pending_jobs: dict[int, dict] = {}

# ── Processing Queue (Semaphore) ──────────────────────────────────────────────
# Limits concurrent FFmpeg processes to 1. Shows queue position to users.
_ffmpeg_semaphore: asyncio.Semaphore = None  # initialized lazily (needs event loop)
_ffmpeg_waiting: int = 0


def _get_semaphore() -> asyncio.Semaphore:
    global _ffmpeg_semaphore
    if _ffmpeg_semaphore is None:
        _ffmpeg_semaphore = asyncio.Semaphore(1)
    return _ffmpeg_semaphore


async def run_ffmpeg_queued(loop, fn, *args, status_msg=None, label: str = ""):
    """
    Run an FFmpeg function through the processing queue.
    Shows queue position in status_msg if other jobs are waiting.
    """
    global _ffmpeg_waiting
    _ffmpeg_waiting += 1
    pos = _ffmpeg_waiting

    if pos > 1 and status_msg:
        try:
            await status_msg.edit(content=f"⏳ Dalam antrian posisi **#{pos}** — menunggu proses sebelumnya selesai...")
        except Exception:
            pass

    try:
        async with _get_semaphore():
            _ffmpeg_waiting -= 1
            if status_msg:
                try:
                    await status_msg.edit(content=f"⚙️ Processing{(' ' + label) if label else ''}...")
                except Exception:
                    pass
            return await loop.run_in_executor(None, fn, *args)
    except Exception:
        _ffmpeg_waiting = max(0, _ffmpeg_waiting - 1)
        raise


def init(bot_instance: discord.Client):
    """Called by bot.py to inject the bot instance."""
    global bot
    bot = bot_instance


# ══════════════════════════════════════════════════════════════════════════════
#  PENDING JOBS PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def save_pending_jobs():
    """Save pending_jobs to disk so they survive restarts."""
    serializable = {}
    for k, v in pending_jobs.items():
        serializable[str(k)] = v
    try:
        os.makedirs(os.path.dirname(PENDING_JOBS_FILE), exist_ok=True)
        with open(PENDING_JOBS_FILE, 'w') as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        print(f"[PendingJobs] Failed to save: {e}")


def load_pending_jobs():
    """Load pending_jobs from disk. Remove entries whose files no longer exist."""
    global pending_jobs
    if not os.path.exists(PENDING_JOBS_FILE):
        return

    try:
        with open(PENDING_JOBS_FILE, 'r') as f:
            data = json.load(f)
    except Exception:
        return

    for k, v in data.items():
        # Check if at least one key file still exists
        has_file = False
        for fkey in ("input_path", "compressed_path", "hq_path"):
            if v.get(fkey) and os.path.exists(v[fkey]):
                has_file = True
                break
        if has_file:
            pending_jobs[int(k)] = v
        else:
            print(f"[PendingJobs] Skipping {k} — local files gone")

    if pending_jobs:
        print(f"[PendingJobs] Restored {len(pending_jobs)} job(s)")

    # Clean the file
    save_pending_jobs()


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOAD VIEW (after HQ save)
# ══════════════════════════════════════════════════════════════════════════════

class YouTubeUploadView(discord.ui.View):
    """After HQ saved: Upload ke YouTube or Skip (delete all)."""

    def __init__(self, job_key: int, hq_path: str, title: str,
                 author_id: int, wm_msg_ids: list[int], timeout: float = None):
        super().__init__(timeout=timeout)
        self.job_key = job_key
        self.hq_path = hq_path
        self.title = title
        self.author_id = author_id
        self.wm_msg_ids = wm_msg_ids

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Hanya pengirim yang bisa klik ini.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Upload ke YouTube", style=discord.ButtonStyle.green, emoji="\U0001F4F9", custom_id="yt_upload_confirm")
    async def upload_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=f"Uploading **{self.title}** ke YouTube...\nGenerating deskripsi dengan Gemini...",
            view=None,
        )

        loop = asyncio.get_event_loop()

        try:
            description = await loop.run_in_executor(
                None, generate_description, self.title
            )
            schedule_time = get_next_schedule_time(channel_type="wanderingwithme")
            schedule_str = schedule_time.strftime("%d %b %Y, %H:%M WITA")

            await interaction.edit_original_response(
                content=(
                    f"Uploading **{self.title}** ke YouTube...\n"
                    f"Scheduled: **{schedule_str}**\n"
                    f"Please wait..."
                ),
            )

            video_id = await loop.run_in_executor(
                None, upload_to_youtube,
                self.hq_path, self.title, description,
                schedule_time, YT_CATEGORY_ID,
            )

            yt_url = f"https://youtu.be/{video_id}"
            add_to_history(video_id, self.title, schedule_time, "wanderingwithme")

            # Send success notification before cleanup
            try:
                await interaction.channel.send(
                    f"✅ **{self.title}** berhasil di-upload ke YouTube!\n"
                    f"🔗 {yt_url}\n"
                    f"📅 Scheduled: **{schedule_str}**"
                )
            except Exception:
                pass

            self._delete_local()
            await self._delete_wm_messages(interaction)

        except Exception as e:
            if is_quota_exceeded(e):
                # Queue it for auto-upload when quota resets
                description_for_queue = locals().get("description", self.title)
                add_to_upload_queue(
                    hq_path=self.hq_path,
                    title=self.title,
                    description=description_for_queue,
                    channel_type="wanderingwithme",
                    yt_client_secret=YT_CLIENT_SECRET,
                    yt_token_file=str(YT_TOKEN_FILE) if YT_TOKEN_FILE else "",
                    yt_category_id=str(YT_CATEGORY_ID),
                    schedule_file=None,
                    start_hour=9,
                )
                await interaction.edit_original_response(
                    content=(
                        f"⚠️ Quota YouTube habis untuk hari ini.\n"
                        f"**{self.title}** sudah masuk **antrian** dan akan otomatis diupload saat quota reset (~15:00–16:00 WITA)."
                    ),
                )
                self.stop()
            else:
                # Non-quota error — restore buttons so user can retry
                try:
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Gagal upload ke YouTube:\n```\n{str(e)[:800]}\n```\n"
                            f"Tekan **Upload ke YouTube** untuk coba lagi."
                        ),
                        view=self,
                    )
                except Exception:
                    pass
                return  # don't stop — keep buttons alive

        self.stop()

    @discord.ui.button(label="Tidak, Skip", style=discord.ButtonStyle.red, custom_id="yt_upload_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="Skipped YouTube upload. Menghapus file & chat watermark...",
            view=None,
        )
        self._delete_local()
        await self._delete_wm_messages(interaction)

        await interaction.edit_original_response(
            content="File lokal & chat watermark sudah dihapus.",
        )
        self.stop()

    async def on_timeout(self):
        self._delete_local()

    def _delete_local(self):
        for path in (self.hq_path,):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    print(f"   [Cleanup] Deleted: {path}")
                except OSError:
                    pass
        job = pending_jobs.pop(self.job_key, None)
        if job:
            for key in ("input_path", "compressed_path", "discord_preview_path"):
                p = job.get(key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        save_pending_jobs()

    async def _delete_wm_messages(self, interaction: discord.Interaction):
        if not WATERMARK_CHANNEL_ID:
            return
        wm_channel = bot.get_channel(WATERMARK_CHANNEL_ID)
        if not wm_channel:
            return
        for msg_id in self.wm_msg_ids:
            try:
                msg = await wm_channel.fetch_message(msg_id)
                await msg.delete()
            except Exception:
                pass
        try:
            original = await interaction.original_response()
            if original:
                await original.delete()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIRM VIEW (preview -> HQ save -> YouTube choice) — PERSISTENT
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    def __init__(self, job_key: int, author_id: int, timeout: float = None):
        super().__init__(timeout=timeout)
        self.job_key = job_key
        self.author_id = author_id
        # Store custom_id with job_key for persistence
        self.confirm_btn.custom_id = f"confirm_hq:{job_key}"
        self.change_title_btn.custom_id = f"confirm_title:{job_key}"
        self.cancel_btn.custom_id = f"confirm_cancel:{job_key}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # On restart, author_id might be 0 — allow anyone
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Hanya pengirim yang bisa klik ini.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm & Save HQ", style=discord.ButtonStyle.green)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        job = pending_jobs.get(self.job_key)
        if not job:
            await interaction.response.send_message(
                "Session expired — file lokal sudah tidak ada.", ephemeral=True
            )
            return

        # Check if input file still exists
        if not job.get("input_path") or not os.path.exists(job["input_path"]):
            await interaction.response.send_message(
                "File lokal sudah tidak ada.", ephemeral=True
            )
            pending_jobs.pop(self.job_key, None)
            save_pending_jobs()
            return

        wm_msg_ids = job.get("wm_msg_ids", [])
        wm_msg_ids.append(interaction.message.id)
        job["wm_msg_ids"] = wm_msg_ids

        await interaction.response.edit_message(
            content="⏳ Menunggu antrian...",
            view=None,
            attachments=[],
        )

        try:
            safe_title = "".join(
                c if c.isalnum() or c in " _-" else "_" for c in job["title"]
            )[:50].strip()
            hq_filename = f"{safe_title}_{self.job_key}_HQ.mp4"
            hq_path = os.path.join(OUTPUT_DIR, hq_filename)

            # Use a proxy message object to update via edit_original_response
            class _InteractionProxy:
                async def edit(self_, content):
                    try:
                        await interaction.edit_original_response(content=content)
                    except Exception:
                        pass

            loop = asyncio.get_event_loop()
            await run_ffmpeg_queued(
                loop, process_video,
                job["input_path"], hq_path, job["title"],
                WATERMARK_PATH, WM_USERNAME, 18,
                status_msg=_InteractionProxy(), label="HQ watermark",
            )

            hq_size_mb = os.path.getsize(hq_path) / (1024 * 1024)
            job["hq_path"] = hq_path
            save_pending_jobs()

            for key in ("input_path", "compressed_path", "discord_preview_path"):
                p = job.get(key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            yt_view = YouTubeUploadView(
                job_key=self.job_key,
                hq_path=hq_path,
                title=job["title"],
                author_id=self.author_id or interaction.user.id,
                wm_msg_ids=wm_msg_ids,
            )
            # Persist so view survives bot restart
            job["view_type"] = "youtube_upload"
            job["author_id"] = self.author_id or interaction.user.id
            job["wm_msg_ids"] = wm_msg_ids
            save_pending_jobs()
            bot.add_view(yt_view)

            await interaction.edit_original_response(
                content=(
                    f"HQ Saved! — **{job['title']}** ({hq_size_mb:.1f} MB)\n"
                    f"```\n{hq_path}\n```\n"
                    f"Upload ke YouTube?"
                ),
                view=yt_view,
            )

        except Exception as exc:
            await interaction.edit_original_response(
                content=f"Error: {str(exc)[:500]}"
            )
            self._cleanup(job)
            pending_jobs.pop(self.job_key, None)
            save_pending_jobs()

        self.stop()

    @discord.ui.button(label="Ubah Title", style=discord.ButtonStyle.blurple)
    async def change_title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        job = pending_jobs.get(self.job_key)
        if not job:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return

        if not job.get("input_path") or not os.path.exists(job["input_path"]):
            await interaction.response.send_message(
                "File lokal sudah tidak ada.", ephemeral=True
            )
            pending_jobs.pop(self.job_key, None)
            save_pending_jobs()
            return

        old_preview_msg = interaction.message

        await interaction.response.send_message("Ketik title baru di chat ini (60 detik):")
        prompt_msg = await interaction.original_response()

        def check(m: discord.Message):
            return (
                m.author.id == (self.author_id or interaction.user.id)
                and m.channel.id == interaction.channel.id
                and not m.attachments
            )

        try:
            reply = await bot.wait_for("message", check=check, timeout=60)
            new_title = reply.content.strip()
            if not new_title:
                await interaction.followup.send("Title kosong. Dibatalkan.")
                return

            processing_msg = await interaction.followup.send(
                "⏳ Menunggu antrian...", wait=True
            )

            compressed_path = os.path.join(TEMP_DIR, f"wm_{self.job_key}_preview.mp4")
            loop = asyncio.get_event_loop()
            await run_ffmpeg_queued(
                loop, process_video,
                job["input_path"], compressed_path, new_title,
                WATERMARK_PATH, WM_USERNAME, 32,
                status_msg=processing_msg, label="reprocess preview",
            )

            job["title"] = new_title
            save_pending_jobs()

            msgs_to_delete = [prompt_msg, reply, processing_msg, old_preview_msg]
            for msg in msgs_to_delete:
                try:
                    await msg.delete()
                except Exception:
                    pass

            wm_ids = job.setdefault("wm_msg_ids", [])
            if old_preview_msg and old_preview_msg.id in wm_ids:
                wm_ids.remove(old_preview_msg.id)

            file_size = os.path.getsize(compressed_path)
            if file_size <= DISCORD_UPLOAD_LIMIT:
                new_view = ConfirmView(self.job_key, self.author_id or interaction.user.id)
                preview_msg = await interaction.channel.send(
                    f"**Preview** — Title: **{new_title}**\nSudah sesuai?",
                    file=discord.File(compressed_path, filename="preview.mp4"),
                    view=new_view,
                )
                wm_ids.append(preview_msg.id)
                os.remove(compressed_path)
            else:
                await interaction.channel.send("Preview terlalu besar untuk Discord.")

            save_pending_jobs()

        except asyncio.TimeoutError:
            try:
                await prompt_msg.delete()
            except Exception:
                pass
            await interaction.followup.send("Timeout — tidak ada title baru.")

        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        job = pending_jobs.pop(self.job_key, None)
        if job:
            self._cleanup(job)
            # Delete all related Discord messages
            wm_msg_ids = job.get("wm_msg_ids", [])
            channel = interaction.channel
            for msg_id in wm_msg_ids:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
                except Exception:
                    pass
        save_pending_jobs()
        # Delete the current message (the one with buttons)
        try:
            await interaction.response.defer()
            await interaction.message.delete()
        except Exception:
            pass
        self.stop()

    async def on_timeout(self):
        job = pending_jobs.pop(self.job_key, None)
        if job:
            self._cleanup(job)
        save_pending_jobs()

    def _cleanup(self, job: dict):
        for key in ("input_path", "compressed_path", "discord_preview_path", "hq_path"):
            path = job.get(key)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
#  TITLE MODAL + WATERMARK VIEW (IG watcher buttons) — PERSISTENT
# ══════════════════════════════════════════════════════════════════════════════

class TitleModal(discord.ui.Modal, title="Kirim ke Watermark"):
    title_input = discord.ui.TextInput(
        label="Title Video",
        placeholder="Masukkan title untuk watermark...",
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(self, hd_path: str, preview_path: str,
                 video_message: discord.Message, user_id: int):
        super().__init__()
        self.hd_path = hd_path
        self.preview_path = preview_path
        self.video_message = video_message
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        title_text = self.title_input.value.strip()
        if not title_text:
            await interaction.followup.send("Title kosong!", ephemeral=True)
            return

        if not WATERMARK_CHANNEL_ID:
            await interaction.followup.send(
                "WATERMARK_CHANNEL_ID belum diset!", ephemeral=True
            )
            return

        wm_channel = bot.get_channel(WATERMARK_CHANNEL_ID)
        if not wm_channel:
            await interaction.followup.send(
                "Watermark channel tidak ditemukan!", ephemeral=True
            )
            return

        if not os.path.exists(self.hd_path):
            return

        job_key = self.video_message.id
        compressed_path = os.path.join(TEMP_DIR, f"wm_{job_key}_wm_preview.mp4")

        try:
            status_msg = await wm_channel.send("⏳ Menunggu antrian watermark...")

            loop = asyncio.get_event_loop()
            await run_ffmpeg_queued(
                loop, process_video,
                self.hd_path, compressed_path, title_text,
                WATERMARK_PATH, WM_USERNAME, 32,
                status_msg=status_msg, label="watermark preview",
            )

            wm_msg_ids = [status_msg.id]

            pending_jobs[job_key] = {
                "input_path": self.hd_path,
                "title": title_text,
                "compressed_path": compressed_path,
                "discord_preview_path": self.preview_path,
                "wm_msg_ids": wm_msg_ids,
                "author_id": self.user_id,
                "view_type": "confirm",
            }
            save_pending_jobs()

            file_size = os.path.getsize(compressed_path)
            view = ConfirmView(job_key, self.user_id)

            await status_msg.delete()
            wm_msg_ids.remove(status_msg.id)

            if file_size <= DISCORD_UPLOAD_LIMIT:
                preview_msg = await wm_channel.send(
                    f"**{title_text}** — Sudah sesuai?",
                    file=discord.File(compressed_path, filename="preview.mp4"),
                    view=view,
                )
                wm_msg_ids.append(preview_msg.id)
            else:
                size_mb = file_size / (1024 * 1024)
                preview_msg = await wm_channel.send(
                    f"**{title_text}** ({size_mb:.1f} MB) — Sudah sesuai?\n"
                    "Preview terlalu besar untuk ditampilkan.",
                    view=view,
                )
                wm_msg_ids.append(preview_msg.id)

            save_pending_jobs()
            print(f"   [Watermark] Preview sent (HD source): {title_text}")

            # Delete the original preview message in the watcher channel
            try:
                await self.video_message.delete()
                print(f"   [Watermark] Deleted watcher preview for: {title_text}")
            except Exception:
                pass

        except Exception as e:
            print(f"   [Watermark] Error processing: {e}")
            if os.path.exists(compressed_path):
                os.remove(compressed_path)


class WatermarkView(discord.ui.View):
    """Buttons on watcher channel: Kirim ke Watermark / Cancel — PERSISTENT"""

    def __init__(self, hd_path: str, preview_path: str):
        super().__init__(timeout=None)
        self.hd_path = hd_path
        self.preview_path = preview_path
        # Use file-based custom_id for persistence
        basename = os.path.basename(hd_path) if hd_path else "unknown"
        self.watermark_btn.custom_id = f"wm_send:{basename}"
        self.cancel_btn.custom_id = f"wm_cancel:{basename}"

    @discord.ui.button(label="Kirim ke Watermark", style=discord.ButtonStyle.blurple)
    async def watermark_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if file still exists
        if not self.hd_path or not os.path.exists(self.hd_path):
            await interaction.response.send_message(
                "File lokal sudah tidak ada.", ephemeral=True
            )
            return

        modal = TitleModal(self.hd_path, self.preview_path,
                           interaction.message, interaction.user.id)
        await interaction.response.send_modal(modal)
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for p in (self.hd_path, self.preview_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        await interaction.response.edit_message(
            content="**Dibatalkan** — Video sudah dihapus dari local.",
            attachments=[],
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        for p in (self.hd_path, self.preview_path):
            try:
                os.remove(p)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  OVERLAY VIEWS — Film & Motivational (Accept -> watermark -> replace message)
# ══════════════════════════════════════════════════════════════════════════════

class _OverlayViewBase(discord.ui.View):
    """
    Base view for Film / Motivational channels.
    Accept -> watermark -> Gemini AI title/desc -> YouTube upload.
    Cancel -> delete local files -> edit message.
    """
    # Subclasses set these:
    _processor = None          # callable(input, output, watermark, crf)
    _watermark_asset = ""      # path to watermark file
    _label = "Overlay"         # display name
    _channel_type = "film"     # "film" or "motivational" (for Gemini prompt)
    _yt_client_secret = ""     # YouTube OAuth client secret path
    _yt_token_file = ""        # YouTube OAuth token path
    _yt_category_id = "22"     # YouTube category
    _schedule_file = None      # schedule state file
    _start_hour = 9            # all channels: 09:00 & 21:00 WITA

    def __init__(self, hd_path: str, preview_path: str):
        super().__init__(timeout=None)
        self.hd_path = hd_path
        self.preview_path = preview_path
        # Persistent custom_id
        basename = os.path.basename(hd_path) if hd_path else "unknown"
        self.accept_btn.custom_id = f"overlay_accept:{self._channel_type}:{basename}"
        self.cancel_btn.custom_id = f"overlay_cancel:{self._channel_type}:{basename}"
        # Deterministic job key from filename (stable across restarts)
        self._job_key = zlib.adler32(basename.encode("utf-8", errors="replace")) & 0x7FFFFFFF
        # Save to pending_jobs only when first created (not when restoring)
        if self._job_key not in pending_jobs:
            pending_jobs[self._job_key] = {
                "hd_path": hd_path,
                "preview_path": preview_path,
                "view_type": f"overlay_{self._channel_type}",
            }
            save_pending_jobs()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if file still exists
        if not self.hd_path or not os.path.exists(self.hd_path):
            await interaction.response.send_message(
                "File lokal sudah tidak ada.", ephemeral=True
            )
            return

        # Show queue position immediately
        global _ffmpeg_waiting
        _ffmpeg_waiting += 1
        pos = _ffmpeg_waiting

        if pos > 1:
            await interaction.response.edit_message(
                content=f"⏳ Dalam antrian posisi **#{pos}** — menunggu proses sebelumnya selesai...",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"⏳ Menunggu antrian...",
                view=None,
            )

        loop = asyncio.get_event_loop()
        hd_out = os.path.join(OUTPUT_DIR, f"{self._label}_{interaction.message.id}_HD.mp4")
        preview_out = os.path.join(TEMP_DIR, f"{self._label}_{interaction.message.id}_wm_preview.mp4")

        async def do_process():
            # Runs inside the semaphore — full pipeline for one video at a time
            try:
                await interaction.edit_original_response(content=f"⚙️ Processing {self._label} HD...")
            except Exception:
                pass

            # 1) Process HD watermark
            await loop.run_in_executor(
                None, self._processor,
                self.hd_path, hd_out, self._watermark_asset, 18,
            )
            hd_size_mb = os.path.getsize(hd_out) / (1024 * 1024)

            try:
                await interaction.edit_original_response(content=f"⚙️ Processing {self._label} preview...")
            except Exception:
                pass

            # 2) Process preview (lower quality for Discord)
            await loop.run_in_executor(
                None, self._processor,
                self.hd_path, preview_out, self._watermark_asset, 35,
            )

            try:
                await interaction.edit_original_response(content=f"🤖 Generating title & description dengan Gemini AI...")
            except Exception:
                pass

            # 3) Generate title & description with Gemini
            title, description = await loop.run_in_executor(
                None, generate_title_and_description,
                hd_out, self._channel_type,
            )

            # 4) Delete the original interaction message
            try:
                await interaction.message.delete()
            except Exception:
                pass

            # 5) Send result to channel
            channel = interaction.channel
            preview_size = os.path.getsize(preview_out)

            yt_view = OverlayYouTubeView(
                hd_path=hd_out,
                preview_path=preview_out,
                title=title,
                description=description,
                channel_type=self._channel_type,
                yt_client_secret=self._yt_client_secret,
                yt_token_file=self._yt_token_file,
                yt_category_id=self._yt_category_id,
                schedule_file=self._schedule_file,
                start_hour=self._start_hour,
            )

            # Transition pending job: overlay -> overlay_yt (for restart recovery)
            pending_jobs.pop(self._job_key, None)
            pending_jobs[yt_view._job_key] = {
                "hd_path": hd_out,
                "preview_path": preview_out,
                "title": title,
                "description": description,
                "channel_type": self._channel_type,
                "yt_client_secret": self._yt_client_secret,
                "yt_token_file": self._yt_token_file,
                "yt_category_id": self._yt_category_id,
                "schedule_file": self._schedule_file,
                "start_hour": self._start_hour,
                "view_type": f"overlay_yt_{self._channel_type}",
            }
            save_pending_jobs()

            content = (
                f"**{self._label}** watermark selesai! (HD: {hd_size_mb:.1f} MB)\n"
                f"**Title:** {title}\n"
                f"**Desc:** {description[:200]}...\n\n"
                f"Upload ke YouTube?"
            )

            if preview_size <= DISCORD_UPLOAD_LIMIT:
                await channel.send(
                    content,
                    file=discord.File(preview_out, filename="watermarked_preview.mp4"),
                    view=yt_view,
                )
            else:
                await channel.send(content, view=yt_view)

            # Cleanup source files
            for p in (self.hd_path, self.preview_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            print(f"[{self._label}] Watermark selesai, menunggu YouTube upload: {hd_out}")

        try:
            async with _get_semaphore():
                _ffmpeg_waiting -= 1
                await do_process()
        except Exception as e:
            _ffmpeg_waiting = max(0, _ffmpeg_waiting - 1)
            try:
                await interaction.edit_original_response(
                    content=(
                        f"❌ Error {self._label} watermark:\n```\n{str(e)[:800]}\n```\n"
                        f"Tekan **Accept** untuk coba lagi."
                    ),
                    view=self,
                )
            except Exception:
                pass
            return  # don't stop — keep buttons alive

        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for p in (self.hd_path, self.preview_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        pending_jobs.pop(self._job_key, None)
        save_pending_jobs()
        await interaction.response.edit_message(
            content=f"**Dibatalkan** — Video {self._label} sudah dihapus.",
            attachments=[],
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        for p in (self.hd_path, self.preview_path):
            try:
                os.remove(p)
            except Exception:
                pass
        pending_jobs.pop(self._job_key, None)
        save_pending_jobs()


# ══════════════════════════════════════════════════════════════════════════════
#  OVERLAY YOUTUBE UPLOAD VIEW (after watermark + Gemini title) — PERSISTENT
# ══════════════════════════════════════════════════════════════════════════════

class OverlayYouTubeView(discord.ui.View):
    """Accept -> upload to YouTube with Gemini-generated title/desc."""

    def __init__(self, hd_path: str, preview_path: str,
                 title: str, description: str, channel_type: str,
                 yt_client_secret: str, yt_token_file: str,
                 yt_category_id: str, schedule_file: str = None,
                 start_hour: int = 0, timeout: float = None):
        super().__init__(timeout=timeout)
        self.hd_path = hd_path
        self.preview_path = preview_path
        self.title = title
        self.description = description
        self.channel_type = channel_type
        self.yt_client_secret = yt_client_secret
        self.yt_token_file = yt_token_file
        self.yt_category_id = yt_category_id
        self.schedule_file = schedule_file
        self.start_hour = start_hour
        # Persistent custom_id
        basename = os.path.basename(hd_path) if hd_path else "unknown"
        self.upload_btn.custom_id = f"overlay_yt_upload:{channel_type}:{basename}"
        self.cancel_btn.custom_id = f"overlay_yt_cancel:{channel_type}:{basename}"
        # Deterministic job key for persistence
        self._job_key = zlib.adler32(basename.encode("utf-8", errors="replace")) & 0x7FFFFFFF

    @discord.ui.button(label="Upload ke YouTube", style=discord.ButtonStyle.green, emoji="\U0001F4F9")
    async def upload_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if file still exists
        if not self.hd_path or not os.path.exists(self.hd_path):
            await interaction.response.send_message(
                "File lokal sudah tidak ada.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=(
                f"Uploading **{self.title}** ke YouTube ({self.channel_type.title()})...\n"
                f"Please wait..."
            ),
            view=None,
        )

        loop = asyncio.get_event_loop()

        try:
            schedule_time = get_next_schedule_time(
                schedule_file=self.schedule_file, start_hour=self.start_hour,
                channel_type=self.channel_type,
            )
            schedule_str = schedule_time.strftime("%d %b %Y, %H:%M WITA")

            await interaction.edit_original_response(
                content=(
                    f"Uploading **{self.title}** ke YouTube...\n"
                    f"Scheduled: **{schedule_str}**\n"
                    f"Please wait..."
                ),
            )

            video_id = await loop.run_in_executor(
                None, upload_to_youtube,
                self.hd_path, self.title, self.description,
                schedule_time, self.yt_category_id,
                self.yt_client_secret, self.yt_token_file,
            )

            yt_url = f"https://youtu.be/{video_id}"
            add_to_history(video_id, self.title, schedule_time, self.channel_type)

            # Send success notification before cleanup
            try:
                await interaction.channel.send(
                    f"✅ **{self.title}** berhasil di-upload ke YouTube! [{self.channel_type.title()}]\n"
                    f"🔗 {yt_url}\n"
                    f"📅 Scheduled: **{schedule_str}**"
                )
            except Exception:
                pass

            # Cleanup all local files then delete the message
            self._cleanup()
            try:
                await interaction.message.delete()
            except Exception:
                pass

        except Exception as e:
            if is_quota_exceeded(e):
                add_to_upload_queue(
                    hq_path=self.hd_path,
                    title=self.title,
                    description=self.description,
                    channel_type=self.channel_type,
                    yt_client_secret=self.yt_client_secret,
                    yt_token_file=self.yt_token_file,
                    yt_category_id=self.yt_category_id,
                    schedule_file=self.schedule_file,
                    start_hour=self.start_hour,
                )
                # Remove preview from Discord but keep the HQ file for later upload
                pending_jobs.pop(self._job_key, None)
                save_pending_jobs()
                if self.preview_path and os.path.exists(self.preview_path):
                    try:
                        os.remove(self.preview_path)
                    except OSError:
                        pass
                await interaction.edit_original_response(
                    content=(
                        f"⚠️ Quota YouTube habis untuk hari ini.\n"
                        f"**{self.title}** sudah masuk **antrian** dan akan otomatis diupload saat quota reset (~15:00–16:00 WITA)."
                    ),
                )
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
                self.stop()
            else:
                # Non-quota error — restore buttons so user can retry
                try:
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Gagal upload ke YouTube:\n```\n{str(e)[:800]}\n```\n"
                            f"**Title:** {self.title}\nTekan **Upload ke YouTube** untuk coba lagi."
                        ),
                        view=self,
                    )
                except Exception:
                    pass
                return  # don't stop — keep buttons alive

        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._cleanup()
        await interaction.response.edit_message(
            content=f"**Dibatalkan** — File sudah dihapus.",
            attachments=[],
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        self._cleanup()

    def _cleanup(self):
        pending_jobs.pop(self._job_key, None)
        save_pending_jobs()
        for p in (self.hd_path, self.preview_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    print(f"   [Cleanup] Deleted: {p}")
                except OSError:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
#  CONCRETE OVERLAY VIEWS
# ══════════════════════════════════════════════════════════════════════════════

class FilmWatermarkView(_OverlayViewBase):
    """Film channel: overlay Coroco.png -> Gemini title -> YouTube (Film account)"""
    _processor = staticmethod(process_film)
    _watermark_asset = FILM_WATERMARK_PATH
    _label = "Film"
    _channel_type = "film"
    _yt_client_secret = YT_FILM_CLIENT_SECRET
    _yt_token_file = YT_FILM_TOKEN_FILE
    _yt_category_id = YT_FILM_CATEGORY_ID
    _schedule_file = SCHEDULE_FILM_FILE
    _start_hour = 9  # 09:00 & 21:00 WITA


class MotivationalWatermarkView(_OverlayViewBase):
    """Motivational channel: overlay Absolutegrowt.mp4 -> Gemini title -> YouTube (Motivational account)"""
    _processor = staticmethod(process_motivational)
    _watermark_asset = MOTIVATIONAL_WATERMARK_PATH
    _label = "Motivational"
    _channel_type = "motivational"
    _yt_client_secret = YT_MOTIVATIONAL_CLIENT_SECRET
    _yt_token_file = YT_MOTIVATIONAL_TOKEN_FILE
    _yt_category_id = YT_MOTIVATIONAL_CATEGORY_ID
    _schedule_file = SCHEDULE_MOTIVATIONAL_FILE
    _start_hour = 9  # 09:00 & 21:00 WITA


class BolaGemingWatermarkView(_OverlayViewBase):
    """Bola Geming channel: greenscreen watermark, freeze at 5s, YouTube Sports upload."""
    _processor = staticmethod(process_bola_geming)
    _watermark_asset = BOLA_GEMING_WATERMARK_PATH
    _label = "BolaGeming"
    _channel_type = "bola_geming"
    _yt_client_secret = YT_BOLA_GEMING_CLIENT_SECRET
    _yt_token_file = YT_BOLA_GEMING_TOKEN_FILE
    _yt_category_id = YT_BOLA_GEMING_CATEGORY_ID
    _schedule_file = SCHEDULE_BOLA_GEMING_FILE
    _start_hour = 9


# ══════════════════════════════════════════════════════════════════════════════
#  RESTORE PERSISTENT VIEWS ON STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def restore_persistent_views():
    """
    Called on bot startup to re-register persistent views for pending jobs.
    This allows buttons to work even after bot restart.
    """
    load_pending_jobs()

    restored = 0
    tracked_hd_paths: set[str] = set()

    for job_key, job in list(pending_jobs.items()):
        view_type = job.get("view_type", "confirm")
        author_id = job.get("author_id", 0)

        if view_type == "confirm":
            view = ConfirmView(job_key, author_id)
            bot.add_view(view)
            restored += 1

        elif view_type == "overlay_film":
            hd_path = job.get("hd_path", "")
            preview_path = job.get("preview_path", "")
            if hd_path and os.path.exists(hd_path):
                view = FilmWatermarkView(hd_path, preview_path)
                bot.add_view(view)
                tracked_hd_paths.add(hd_path)
                restored += 1
            else:
                pending_jobs.pop(job_key, None)

        elif view_type == "overlay_motivational":
            hd_path = job.get("hd_path", "")
            preview_path = job.get("preview_path", "")
            if hd_path and os.path.exists(hd_path):
                view = MotivationalWatermarkView(hd_path, preview_path)
                bot.add_view(view)
                tracked_hd_paths.add(hd_path)
                restored += 1
            else:
                pending_jobs.pop(job_key, None)

        elif view_type == "youtube_upload":
            hq_path = job.get("hq_path", "")
            if hq_path and os.path.exists(hq_path):
                view = YouTubeUploadView(
                    job_key=int(job_key),
                    hq_path=hq_path,
                    title=job.get("title", ""),
                    author_id=job.get("author_id", 0),
                    wm_msg_ids=job.get("wm_msg_ids", []),
                )
                bot.add_view(view)
                restored += 1
            else:
                pending_jobs.pop(job_key, None)

        elif view_type in ("overlay_yt_film", "overlay_yt_motivational"):
            hd_path = job.get("hd_path", "")
            if hd_path and os.path.exists(hd_path):
                view = OverlayYouTubeView(
                    hd_path=hd_path,
                    preview_path=job.get("preview_path", ""),
                    title=job.get("title", ""),
                    description=job.get("description", ""),
                    channel_type=job.get("channel_type", ""),
                    yt_client_secret=job.get("yt_client_secret", ""),
                    yt_token_file=job.get("yt_token_file", ""),
                    yt_category_id=job.get("yt_category_id", "22"),
                    schedule_file=job.get("schedule_file"),
                    start_hour=job.get("start_hour", 0),
                )
                bot.add_view(view)
                tracked_hd_paths.add(hd_path)
                restored += 1
            else:
                pending_jobs.pop(job_key, None)

    if restored:
        print(f"[PersistentViews] Restored {restored} view(s)")

    # Restore WatermarkViews by scanning temp_downloads for HD files
    # Skip files already tracked as Film/Motivational overlay views
    try:
        for fname in os.listdir(TEMP_DIR):
            if fname.endswith("_hd.mp4") or fname.endswith("_HD.mp4"):
                hd_path = os.path.join(TEMP_DIR, fname)
                if hd_path in tracked_hd_paths:
                    continue
                preview_path = hd_path.replace("_hd.mp4", "_preview.mp4").replace("_HD.mp4", "_preview.mp4")
                view = WatermarkView(hd_path, preview_path if os.path.exists(preview_path) else "")
                bot.add_view(view)
    except Exception:
        pass

    save_pending_jobs()
