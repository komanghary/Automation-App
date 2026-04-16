"""
Centralized configuration — loads .env and exposes all settings/constants.
"""

import os
from datetime import timedelta, timezone
from dotenv import load_dotenv

# ── Base paths ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── FFmpeg ────────────────────────────────────────────────────────────────────
_FFMPEG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft", "WinGet", "Links",
)
FFMPEG = (
    os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
    if os.path.isfile(os.path.join(_FFMPEG_DIR, "ffmpeg.exe"))
    else "ffmpeg"
)

# ── Instagram ─────────────────────────────────────────────────────────────────
IG_USERNAME   = os.getenv("IG_USERNAME", "")
IG_PASSWORD   = os.getenv("IG_PASSWORD", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

_watcher_ch = os.getenv("WATCHER_CHANNEL_ID", "")
WATCHER_CHANNEL_ID: int | None = int(_watcher_ch) if _watcher_ch.strip().isdigit() else None

# Watchlist: mapping nama grup IG → channel Discord
WATCHLIST: dict[str, int] = {}
_wl_raw = os.getenv("WATCHLIST", "")
if _wl_raw.strip():
    for _entry in _wl_raw.split(","):
        _entry = _entry.strip()
        if "=" in _entry:
            _name_part, _ch_part = _entry.rsplit("=", 1)
            _name_part = _name_part.strip()
            _ch_part = _ch_part.strip()
            if _name_part and _ch_part.isdigit():
                WATCHLIST[_name_part.lower()] = int(_ch_part)

_watermark_ch = os.getenv("WATERMARK_CHANNEL_ID", "")
WATERMARK_CHANNEL_ID: int | None = int(_watermark_ch) if _watermark_ch.strip().isdigit() else None

_dashboard_ch = os.getenv("DASHBOARD_CHANNEL_ID", "")
DASHBOARD_CHANNEL_ID: int | None = int(_dashboard_ch) if _dashboard_ch.strip().isdigit() else None
DASHBOARD_INTERVAL = int(os.getenv("DASHBOARD_INTERVAL", "300"))

_copyright_ch = os.getenv("COPYRIGHT_CHANNEL_ID", "")
COPYRIGHT_CHANNEL_ID: int | None = int(_copyright_ch) if _copyright_ch.strip().isdigit() else None

_book_ch = os.getenv("BOOK_CHANNEL_ID", "")
BOOK_CHANNEL_ID: int | None = int(_book_ch) if _book_ch.strip().isdigit() else None

# ── Watermark assets (all in watermarks/ folder) ─────────────────────────────
_WM_DIR = os.path.join(BASE_DIR, "watermarks")

WATERMARK_PATH = os.getenv("WATERMARK_PATH", os.path.join(_WM_DIR, "watermark.png"))
WM_USERNAME    = os.getenv("WATERMARK_USERNAME", "@wanderingwithmevideos")

# Film — Coroco.png
FILM_WATERMARK_PATH = os.path.join(_WM_DIR, "Coroco.png")
FILM_CHANNEL_ID: int | None = None
_film_ch = os.getenv("FILM_CHANNEL_ID", "1485607366778159104")
if _film_ch.strip().isdigit():
    FILM_CHANNEL_ID = int(_film_ch)

# Motivational — Absolutegrowt.mp4
MOTIVATIONAL_WATERMARK_PATH = os.path.join(_WM_DIR, "Absolutegrowt.mp4")
MOTIVATIONAL_CHANNEL_ID: int | None = None
_motiv_ch = os.getenv("MOTIVATIONAL_CHANNEL_ID", "1485609389904629860")
if _motiv_ch.strip().isdigit():
    MOTIVATIONAL_CHANNEL_ID = int(_motiv_ch)

# Bola Geming — ONGOAL_WM_VIDEO.mp4 (greenscreen, freeze-at-5s)
BOLA_GEMING_WATERMARK_PATH = os.path.join(_WM_DIR, "ONGOAL_WM_VIDEO.mp4")
BOLA_GEMING_CHANNEL_ID: int | None = None
_bg_ch = os.getenv("BOLA_GEMING_CHANNEL_ID", "1491056906348007635")
if _bg_ch.strip().isdigit():
    BOLA_GEMING_CHANNEL_ID = int(_bg_ch)

YT_BOLA_GEMING_CLIENT_SECRET = os.getenv(
    "YT_BOLA_GEMING_CLIENT_SECRET",
    os.path.join(BASE_DIR, "client_secret_919480837979-39j6mblko720r02ndc3th21rfrs9f2lf.apps.googleusercontent.com.json"),
)
YT_BOLA_GEMING_TOKEN_FILE = os.path.join(BASE_DIR, "data", "youtube_token_bola_geming.json")
YT_BOLA_GEMING_CATEGORY_ID = os.getenv("YT_BOLA_GEMING_CATEGORY_ID", "17")  # Sports

# ── YouTube ───────────────────────────────────────────────────────────────────
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET", os.path.join(BASE_DIR, "client_secret.json"))
YT_TOKEN_FILE    = os.path.join(BASE_DIR, "data", "youtube_token.json")
YT_SCOPES        = [
    "https://www.googleapis.com/auth/youtube",
]
YT_CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "22")

# Separate YouTube accounts for Film and Motivational
# Each has its own client_secret and token file
YT_FILM_CLIENT_SECRET = os.getenv(
    "YT_FILM_CLIENT_SECRET",
    os.path.join(BASE_DIR, "client_secret_932999381863-vs560avbbchcejjlmgm0oe7242j61rpn.apps.googleusercontent.com.json"),
)
YT_FILM_TOKEN_FILE = os.path.join(BASE_DIR, "data", "youtube_token_film.json")
YT_FILM_CATEGORY_ID = os.getenv("YT_FILM_CATEGORY_ID", "24")  # Entertainment

YT_MOTIVATIONAL_CLIENT_SECRET = os.getenv(
    "YT_MOTIVATIONAL_CLIENT_SECRET",
    os.path.join(BASE_DIR, "client_secret_932999381863-vs560avbbchcejjlmgm0oe7242j61rpn.apps.googleusercontent.com.json"),
)
YT_MOTIVATIONAL_TOKEN_FILE = os.path.join(BASE_DIR, "data", "youtube_token_motivational.json")
YT_MOTIVATIONAL_CATEGORY_ID = os.getenv("YT_MOTIVATIONAL_CATEGORY_ID", "22")  # People & Blogs

# ── Gemini ────────────────────────────────────────────────────────────────────
_gemini_keys_raw = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", ""))
GEMINI_API_KEYS  = [k.strip() for k in _gemini_keys_raw.split(",") if k.strip()]

# ── Paths ─────────────────────────────────────────────────────────────────────
TEMP_DIR       = os.path.join(BASE_DIR, "temp_downloads")
OUTPUT_DIR     = os.path.join(BASE_DIR, "output")
DATA_DIR       = os.path.join(BASE_DIR, "data")
SEEN_FILE      = os.path.join(DATA_DIR, "seen_messages.json")
SCHEDULE_FILE  = os.path.join(DATA_DIR, "schedule.json")
SCHEDULE_FILM_FILE = os.path.join(DATA_DIR, "schedule_film.json")
SCHEDULE_BOLA_GEMING_FILE = os.path.join(DATA_DIR, "schedule_bola_geming.json")
SCHEDULE_MOTIVATIONAL_FILE = os.path.join(DATA_DIR, "schedule_motivational.json")
HISTORY_FILE   = os.path.join(DATA_DIR, "upload_history.json")
DASHBOARD_FILE = os.path.join(DATA_DIR, "dashboard_msg.json")
PENDING_JOBS_FILE  = os.path.join(DATA_DIR, "pending_jobs.json")
UPLOAD_QUEUE_FILE  = os.path.join(DATA_DIR, "upload_queue.json")

for _d in (TEMP_DIR, OUTPUT_DIR, DATA_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────
WITA = timezone(timedelta(hours=8))
DISCORD_UPLOAD_LIMIT = 25 * 1024 * 1024  # 25 MB
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}
MAX_FILE_MB = 500
