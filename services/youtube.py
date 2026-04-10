"""
YouTube service — authentication, upload, scheduling, history, stats.
"""

import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import (
    YT_CLIENT_SECRET, YT_TOKEN_FILE, YT_SCOPES, YT_CATEGORY_ID,
    YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE, YT_FILM_CATEGORY_ID,
    YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE, YT_MOTIVATIONAL_CATEGORY_ID,
    SCHEDULE_FILE, SCHEDULE_FILM_FILE, SCHEDULE_MOTIVATIONAL_FILE,
    HISTORY_FILE, UPLOAD_QUEUE_FILE, WITA,
)


# ── Authentication ────────────────────────────────────────────────────────────

def yt_authenticate(client_secret: str = None, token_file: str = None):
    """Authenticate with YouTube API (OAuth2). Opens browser on first run.
    Supports multiple accounts via different client_secret/token_file pairs.
    """
    client_secret = client_secret or YT_CLIENT_SECRET
    token_file = token_file or YT_TOKEN_FILE

    creds = None
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, YT_SCOPES)
        except Exception:
            pass

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(client_secret):
            raise FileNotFoundError(
                f"client_secret.json tidak ditemukan di {client_secret}\n"
                "Download dari Google Cloud Console → APIs & Services → Credentials"
            )
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, YT_SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        with open(token_file, 'w') as f:
            f.write(creds.to_json())
        print(f"[YouTube] Token tersimpan: {token_file}")

    return build("youtube", "v3", credentials=creds)


# ── Scheduling ────────────────────────────────────────────────────────────────

def get_next_schedule_time(schedule_file: str = None, start_hour: int = 0,
                           channel_type: str = None) -> datetime:
    """
    Find the first EMPTY 12-hour slot for this channel.

    All channels use fixed hours: 09:00 and 21:00 WITA.
    A slot is "taken" if there's already a video scheduled on the same date+hour.

    - schedule_file: path to the schedule state file (default: SCHEDULE_FILE)
    - start_hour: ignored (kept for API compat), all channels use 9/21
    - channel_type: used to filter history for gap detection (wanderingwithme/film/motivational)
    """
    schedule_file = schedule_file or SCHEDULE_FILE
    now = datetime.now(WITA)

    # Collect all future scheduled times for this channel from history
    taken_slots: set[str] = set()  # "YYYY-MM-DD HH" format for exact match
    try:
        history = load_upload_history()
        for h in history:
            ct = h.get("channel_type", "wanderingwithme")
            if channel_type and ct != channel_type:
                continue
            sched_str = h.get("scheduled_at")
            if sched_str:
                try:
                    sched = datetime.fromisoformat(sched_str)
                    if sched > now:
                        taken_slots.add(sched.strftime("%Y-%m-%d %H"))
                except Exception:
                    pass
    except Exception:
        pass

    # Also read schedule_file for last scheduled time (prevents race condition
    # when multiple uploads happen before history is updated)
    try:
        if os.path.exists(schedule_file):
            with open(schedule_file, 'r') as f:
                last = json.load(f).get("last_scheduled", "")
            if last:
                last_dt = datetime.fromisoformat(last)
                if last_dt > now:
                    taken_slots.add(last_dt.strftime("%Y-%m-%d %H"))
    except Exception:
        pass

    # Fixed slots: 09:00 and 21:00 WITA only
    SLOT_HOURS = [9, 21]

    # Start from the next available slot after now
    candidate = now.replace(minute=0, second=0, microsecond=0)
    # Find the next 9 or 21 hour
    if candidate.hour < 9:
        candidate = candidate.replace(hour=9)
    elif candidate.hour < 21:
        candidate = candidate.replace(hour=21)
    else:
        candidate = (candidate + timedelta(days=1)).replace(hour=9)

    # If candidate is in the past (same hour but already passed), move forward
    if candidate <= now:
        if candidate.hour == 9:
            candidate = candidate.replace(hour=21)
        else:
            candidate = (candidate + timedelta(days=1)).replace(hour=9)

    # Walk forward up to 60 slots (~30 days) to find first empty one
    for _ in range(60):
        slot_key = candidate.strftime("%Y-%m-%d %H")
        if slot_key not in taken_slots:
            break
        # Move to next slot
        if candidate.hour == 9:
            candidate = candidate.replace(hour=21)
        else:
            candidate = (candidate + timedelta(days=1)).replace(hour=9)

    target = candidate

    with open(schedule_file, 'w') as f:
        json.dump({"last_scheduled": target.isoformat()}, f)

    print(f"[Schedule] Slot dipilih: {target.strftime('%d %b %Y %H:%M WITA')} (channel: {channel_type or 'default'})")
    return target


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_to_youtube(
    video_path: str,
    title: str,
    description: str,
    publish_at: datetime,
    category_id: str = "22",
    client_secret: str = None,
    token_file: str = None,
) -> str:
    """Upload video to YouTube as scheduled (private until publish_at). Returns video ID."""
    youtube = yt_authenticate(client_secret, token_file)

    publish_utc = publish_at.astimezone(timezone.utc)
    publish_str = publish_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
            "tags": [t.strip() for t in title.split() if len(t.strip()) > 2],
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_str,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    print(f"[YouTube] Uploading: {title}")
    response = None
    retry = 0
    max_retries = 10
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"   [YouTube] Upload progress: {pct}%")
            retry = 0  # reset on success
        except (socket.error, ConnectionError, OSError) as e:
            retry += 1
            if retry > max_retries:
                raise RuntimeError(f"Upload gagal setelah {max_retries} retry: {e}") from e
            wait = min(2 ** retry, 60)  # exponential backoff, max 60s
            print(f"   [YouTube] Koneksi terputus ({e}), retry {retry}/{max_retries} dalam {wait}s...")
            time.sleep(wait)
        except Exception as e:
            # Let non-connection errors (quota, auth, etc.) bubble up immediately
            raise

    video_id = response["id"]
    print(f"[YouTube] Upload selesai! Video ID: {video_id}")
    print(f"[YouTube] Scheduled: {publish_at.strftime('%Y-%m-%d %H:%M WITA')}")
    return video_id


# ── Upload History ────────────────────────────────────────────────────────────

def load_upload_history() -> list[dict]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_upload_history(history: list[dict]):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2, default=str)


def add_to_history(video_id: str, title: str, scheduled_at: datetime, channel_type: str = "wanderingwithme"):
    history = load_upload_history()
    history.append({
        "video_id": video_id,
        "title": title,
        "scheduled_at": scheduled_at.isoformat(),
        "uploaded_at": datetime.now(WITA).isoformat(),
        "channel_type": channel_type,
    })
    save_upload_history(history)


def delete_video(video_id: str, client_secret: str = None, token_file: str = None) -> bool:
    """Delete a video from YouTube. Returns True if successful."""
    try:
        youtube = yt_authenticate(client_secret, token_file)
        youtube.videos().delete(id=video_id).execute()
        print(f"[YouTube] Video dihapus: {video_id}")
        return True
    except Exception as e:
        print(f"[YouTube] Gagal hapus video {video_id}: {e}")
        return False


def reschedule_video(video_id: str, new_publish_at: datetime,
                     client_secret: str = None, token_file: str = None) -> bool:
    """Reschedule a video on YouTube to a new publish time. Returns True if successful."""
    try:
        youtube = yt_authenticate(client_secret, token_file)
        publish_utc = new_publish_at.astimezone(timezone.utc)
        publish_str = publish_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        youtube.videos().update(
            part="status",
            body={
                "id": video_id,
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_str,
                },
            },
        ).execute()
        print(f"[YouTube] Video {video_id} dijadwal ulang ke {new_publish_at.strftime('%d %b %Y %H:%M WITA')}")
        return True
    except Exception as e:
        print(f"[YouTube] Gagal reschedule {video_id}: {e}")
        return False


def check_copyright_blocks(history: list[dict]) -> list[dict]:
    """
    Check all videos in history for copyright / Short-policy blocks.

    Detects:
    - uploadStatus == 'rejected' (any rejectionReason)
    - Videos missing from API response (deleted externally)
    - Videos past their scheduled publish date that are still 'private'
      (YouTube silently blocked them from going public — Short policy / copyright)
    - privacyStatus unexpectedly changed to 'private' after publish date
    """
    if not history:
        return []

    now_utc = datetime.now(timezone.utc)

    # Group by channel_type to use correct credentials
    CHANNEL_CREDS = {
        "wanderingwithme": (None, None),
        "film": (YT_FILM_CLIENT_SECRET, YT_FILM_TOKEN_FILE),
        "motivational": (YT_MOTIVATIONAL_CLIENT_SECRET, YT_MOTIVATIONAL_TOKEN_FILE),
    }

    blocked = []

    for channel_type, (cs, tf) in CHANNEL_CREDS.items():
        entries = [h for h in history if h.get("channel_type", "wanderingwithme") == channel_type]
        if not entries:
            continue

        video_ids = [h["video_id"] for h in entries]
        try:
            youtube = yt_authenticate(cs, tf)
            returned_ids = set()

            for i in range(0, len(video_ids), 50):
                chunk = video_ids[i:i+50]
                resp = youtube.videos().list(
                    part="status,snippet,contentDetails",
                    id=",".join(chunk),
                ).execute()

                for item in resp.get("items", []):
                    vid = item["id"]
                    returned_ids.add(vid)
                    status = item.get("status", {})
                    upload_status = status.get("uploadStatus", "")
                    rejection_reason = status.get("rejectionReason", "")
                    privacy_status = status.get("privacyStatus", "")
                    failure_reason = status.get("failureReason", "")

                    entry = next((h for h in entries if h["video_id"] == vid), None)
                    if not entry:
                        continue

                    # 1. Explicitly rejected for any reason
                    if upload_status == "rejected":
                        reason_label = rejection_reason or "unknown"
                        blocked.append({**entry, "block_reason": f"rejected:{reason_label}",
                                        "channel_type": channel_type, "privacy_status": privacy_status,
                                        "auto_delete": True})
                        print(f"   [Copyright] Rejected: {entry.get('title','?')[:40]} — {reason_label}")
                        continue

                    # 2. Upload failed
                    if upload_status == "failed":
                        blocked.append({**entry, "block_reason": f"failed:{failure_reason or 'unknown'}",
                                        "channel_type": channel_type, "privacy_status": privacy_status,
                                        "auto_delete": True})
                        print(f"   [Copyright] Failed: {entry.get('title','?')[:40]} — {failure_reason}")
                        continue

                    # 3. Content ID / copyright block — region restriction
                    # auto_delete=True regardless of visibility (scheduled/private/public/blocked)
                    region = item.get("contentDetails", {}).get("regionRestriction", {})
                    blocked_countries = region.get("blocked", [])
                    if len(blocked_countries) >= 10:
                        blocked.append({**entry, "block_reason": f"region_blocked:{len(blocked_countries)}_countries",
                                        "channel_type": channel_type, "privacy_status": privacy_status,
                                        "auto_delete": True})
                        print(f"   [Copyright] Region blocked ({len(blocked_countries)} countries) [WILL DELETE]: {entry.get('title','?')[:40]}")
                        continue

                    # 4. Copyright claim detected via license or status
                    # Some copyright claims show as "creativeCommon" forced license
                    license_type = status.get("license", "")
                    if license_type == "creativeCommon" and upload_status == "processed":
                        blocked.append({**entry, "block_reason": "copyright_claim:forced_license",
                                        "channel_type": channel_type, "privacy_status": privacy_status,
                                        "auto_delete": True})
                        print(f"   [Copyright] Forced license (copyright claim): {entry.get('title','?')[:40]}")
                        continue

                    # 5. Still private after scheduled publish date
                    # YouTube blocks videos from going public when copyright claim is detected
                    if privacy_status == "private" and upload_status == "processed":
                        sched_str = entry.get("scheduled_at", "")
                        if sched_str:
                            try:
                                sched_dt = datetime.fromisoformat(sched_str)
                                if sched_dt.tzinfo is None:
                                    sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                                # If past schedule + 1 hour buffer, it's blocked
                                if now_utc > sched_dt + timedelta(hours=1):
                                    blocked.append({**entry, "block_reason": "still_private_after_publish",
                                                    "channel_type": channel_type, "privacy_status": privacy_status,
                                                    "auto_delete": True})
                                    print(f"   [Copyright] Still private after publish date (likely copyright): {entry.get('title','?')[:40]}")
                                    continue
                            except (ValueError, TypeError):
                                pass

            # 4. Videos missing from API = deleted / removed externally
            for h in entries:
                if h["video_id"] not in returned_ids:
                    blocked.append({**h, "block_reason": "not_found", "channel_type": channel_type})
                    print(f"   [Copyright] Not found (deleted): {h.get('title','?')[:40]}")

            print(f"[Copyright] {channel_type}: checked {len(entries)} videos, {len(returned_ids)} found in API")

        except Exception as e:
            print(f"[Copyright] Gagal cek {channel_type}: {e}")

    return blocked


# ── Upload Queue ──────────────────────────────────────────────────────────────

def is_quota_exceeded(exc: Exception) -> bool:
    """Return True if the exception is a YouTube quota/upload-limit error."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            reason = ""
            try:
                import json as _json
                content = _json.loads(exc.content)
                errors = content.get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
            except Exception:
                reason = str(exc.content)
            # 403 quota errors
            if exc.resp.status == 403 and reason in (
                "quotaExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
            ):
                return True
            # 400 upload limit exceeded
            if exc.resp.status == 400 and reason == "uploadLimitExceeded":
                return True
    except Exception:
        pass
    return False


def load_upload_queue() -> list[dict]:
    if os.path.exists(UPLOAD_QUEUE_FILE):
        try:
            with open(UPLOAD_QUEUE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_upload_queue(queue: list[dict]):
    os.makedirs(os.path.dirname(UPLOAD_QUEUE_FILE), exist_ok=True)
    with open(UPLOAD_QUEUE_FILE, 'w') as f:
        json.dump(queue, f, indent=2, default=str)


def add_to_upload_queue(
    hq_path: str, title: str, description: str,
    channel_type: str, yt_client_secret: str, yt_token_file: str,
    yt_category_id: str, schedule_file: str = None, start_hour: int = 0,
):
    queue = load_upload_queue()
    queue.append({
        "hq_path": hq_path,
        "title": title,
        "description": description,
        "channel_type": channel_type,
        "yt_client_secret": yt_client_secret,
        "yt_token_file": yt_token_file,
        "yt_category_id": yt_category_id,
        "schedule_file": schedule_file,
        "start_hour": start_hour,
        "queued_at": datetime.now(WITA).isoformat(),
    })
    save_upload_queue(queue)
    print(f"[Queue] Ditambahkan ke antrian: {title} ({channel_type})")


def fetch_video_stats(video_ids: list[str], client_secret: str = None, token_file: str = None) -> dict:
    """Fetch YouTube stats (views, likes, comments) for a list of video IDs."""
    if not video_ids:
        return {}
    try:
        youtube = yt_authenticate(client_secret, token_file)
        stats = {}
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i+50]
            resp = youtube.videos().list(
                part="statistics,status,snippet",
                id=",".join(chunk),
            ).execute()
            for item in resp.get("items", []):
                vid = item["id"]
                s = item.get("statistics", {})
                st = item.get("status", {})
                stats[vid] = {
                    "views": int(s.get("viewCount", 0)),
                    "likes": int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                    "privacy": st.get("privacyStatus", "unknown"),
                    "publish_at": st.get("publishAt", ""),
                }
        return stats
    except Exception as e:
        print(f"[YouTube] Gagal fetch stats: {e}")
        return {}
