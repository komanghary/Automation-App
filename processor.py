import subprocess
import json
import os
import re
import tempfile
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from pilmoji.source import AppleEmojiSource

# Regex to detect emoji characters (broad Unicode ranges)
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002702-\U000027B0"  # dingbats
    "\U000024C2-\U0001F251"
    "\U0000200D"             # ZWJ
    "\U0000FE0F"             # variation selector
    "]+", re.UNICODE
)

# ── Target output dimensions ──────────────────────────────────────────────────
TARGET_W = 1080
TARGET_H = 1920

# ── Layout config (card-style like reference) ────────────────────────────────
PAD_TOP      = 20        # padding from top edge to avatar row
PAD_SIDE     = 50        # side padding for text
AVATAR_SZ    = 140       # small profile picture (like reference)
GAP_AV_TXT   = 18        # gap between avatar and username text
TITLE_GAP    = 2         # gap between title and video

# Blue tick image path
BLUE_TICK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watermarks", "blue_tick.png")

# ── Font search paths ─────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── FFmpeg path ──────────────────────────────────────────────────────────────
_FFMPEG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft", "WinGet", "Links",
)
_FFMPEG  = os.path.join(_FFMPEG_DIR, "ffmpeg.exe") if os.path.isfile(
    os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
) else "ffmpeg"
_FFPROBE = os.path.join(_FFMPEG_DIR, "ffprobe.exe") if os.path.isfile(
    os.path.join(_FFMPEG_DIR, "ffprobe.exe")
) else "ffprobe"

_FONT_REGULAR = [
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Regular.ttf"),
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Regular.otf"),
    "C:/Windows/Fonts/ProximaNova-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_FONT_BOLD = [
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Bold.ttf"),
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Bold.otf"),
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Semibold.ttf"),
    os.path.join(_BASE_DIR, "fonts", "ProximaNova-Semibold.otf"),
    "C:/Windows/Fonts/ProximaNova-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = _FONT_BOLD if bold else _FONT_REGULAR
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_video_info(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    cmd = [
        _FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    for stream in data["streams"]:
        if stream["codec_type"] == "video":
            return int(stream["width"]), int(stream["height"])
    raise ValueError("No video stream found in file.")


def _make_circle_avatar(img_path: str, size: int) -> Image.Image:
    """Load image, CENTER-CROP to square, resize, clip to circle (RGBA)."""
    img = Image.open(img_path).convert("RGBA")
    w, h = img.size

    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img  = img.crop((left, top, left + side, top + side))

    img = img.resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)

    result = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    result.paste(img, mask=mask)
    return result


def _measure_text_w(text: str, font: ImageFont.FreeTypeFont) -> int:
    """Measure text width, accounting for emoji characters."""
    emoji_size = int(font.size * 1.25)
    parts = _EMOJI_RE.split(text)
    emojis = _EMOJI_RE.findall(text)

    total_w = 0
    for i, part in enumerate(parts):
        if part:
            bbox = font.getbbox(part)
            total_w += bbox[2] - bbox[0]
        if i < len(emojis):
            clean = emojis[i].replace("\u200D", "").replace("\uFE0F", "")
            n_emoji = max(1, len(clean))
            total_w += n_emoji * emoji_size
    return total_w


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Break text into lines that fit within max_width pixels (emoji-aware)."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if _measure_text_w(test, font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if _measure_text_w(current, font) > max_width:
                chars = list(current)
                sub_line = ""
                for ch in chars:
                    test_ch = sub_line + ch
                    if _measure_text_w(test_ch, font) <= max_width:
                        sub_line = test_ch
                    else:
                        if sub_line:
                            lines.append(sub_line)
                        sub_line = ch
                current = sub_line
    if current:
        lines.append(current)
    return lines or [text]


def _get_title_height(title: str, font_title, max_text_w: int, line_h: int) -> int:
    """Calculate total pixel height of wrapped title text."""
    lines = _wrap_text(title, font_title, max_text_w)
    return len(lines) * line_h


def _get_header_height(title: str) -> int:
    """Calculate total header height (avatar row + title text)."""
    font_title = _find_font(48, bold=True)
    max_text_w = TARGET_W - 2 * PAD_SIDE
    line_h     = 58
    title_h    = _get_title_height(title, font_title, max_text_w, line_h)
    # avatar row + gap + title + gap
    return PAD_TOP + AVATAR_SZ + 5 + title_h + TITLE_GAP


def create_background(
    watermark_path: str,
    username: str,
    title: str,
    header_top_y: int,
) -> Image.Image:
    """
    Create full 1080x1920 white background with card-style layout:
      - Small avatar + @username ✅ positioned at header_top_y
      - Title centered below username row
    header_top_y controls where the header block starts vertically.
    """
    img  = Image.new("RGB", (TARGET_W, TARGET_H), "white")
    draw = ImageDraw.Draw(img)

    # ── Avatar (small, with padding) ─────────────────────────────────────
    avatar_x = PAD_SIDE
    avatar_y = header_top_y
    if os.path.exists(watermark_path):
        avatar = _make_circle_avatar(watermark_path, AVATAR_SZ)
        bg = Image.new("RGB", (AVATAR_SZ, AVATAR_SZ), "white")
        bg.paste(avatar, mask=avatar.split()[3])
        img.paste(bg, (avatar_x, avatar_y))
    else:
        draw.ellipse(
            (avatar_x, avatar_y, avatar_x + AVATAR_SZ, avatar_y + AVATAR_SZ),
            fill="#CCCCCC",
        )

    # ── @username + blue tick image — vertically centered with avatar ────
    font_user = _find_font(38, bold=False)
    user_text = username
    user_bbox = font_user.getbbox(user_text)
    user_h    = user_bbox[3] - user_bbox[1]
    user_x    = avatar_x + AVATAR_SZ + GAP_AV_TXT
    user_y    = avatar_y + (AVATAR_SZ - user_h) // 2

    draw.text((user_x, user_y), user_text, fill="#222222", font=font_user)

    # Blue tick image after username, same height as text
    tick_size = int(font_user.size * 1.1) + 20  # match text size + 20px
    text_w = font_user.getbbox(user_text)[2] - font_user.getbbox(user_text)[0]
    tick_x = user_x + text_w + 10
    tick_y = user_y + (user_h - tick_size) // 2
    if os.path.exists(BLUE_TICK_PATH):
        tick_img = Image.open(BLUE_TICK_PATH).convert("RGBA")
        tick_img = tick_img.resize((tick_size, tick_size), Image.LANCZOS)
        img.paste(tick_img, (tick_x, tick_y), mask=tick_img.split()[3])

    # ── Title — centered horizontally below avatar row (emoji support) ───
    font_title = _find_font(48, bold=True)
    max_text_w = TARGET_W - 2 * PAD_SIDE
    lines      = _wrap_text(title, font_title, max_text_w)
    line_h     = 58
    title_y    = avatar_y + AVATAR_SZ + 5

    with Pilmoji(img, source=AppleEmojiSource) as pmoji:
        for i, line in enumerate(lines):
            line_w = pmoji.getsize(line, font=font_title)[0]
            x = (TARGET_W - line_w) // 2
            pmoji.text(
                (x, title_y + i * line_h),
                line,
                fill="#111111",
                font=font_title,
            )

    return img


# ── Main processor ────────────────────────────────────────────────────────────

def process_video(
    input_path: str,
    output_path: str,
    title: str,
    watermark_path: str,
    username: str,
    crf: int = 28,
) -> None:
    """
    Card-style layout:
      - Video centered vertically in 1080x1920 frame
      - Avatar + @username ✅ + title positioned just above video
    """
    src_w, src_h = get_video_info(input_path)

    # ── Header height (avatar + username + title) ────────────────────────
    header_h = _get_header_height(title)

    # ── Calculate video scale ─────────────────────────────────────────────
    # Video + header must fit in 1080x1920
    available_h = TARGET_H - header_h
    available_w = TARGET_W

    scale = min(available_w / src_w, available_h / src_h)
    new_w = int(src_w * scale) & ~1   # must be even for libx264
    new_h = int(src_h * scale) & ~1

    # ── Position video+header ──────────────────────────────────────────────
    aspect = src_h / src_w
    total_block_h = header_h + new_h

    if aspect >= 1.5:
        # 9:16 or taller — header at top, video fills rest
        header_top_y = PAD_TOP
        video_y = header_top_y + header_h
    else:
        # Landscape/square — center entire block vertically
        block_top = (TARGET_H - total_block_h) // 2
        header_top_y = block_top
        video_y = block_top + header_h

    video_x = (TARGET_W - new_w) // 2

    # ── Create full background PNG ────────────────────────────────────────
    bg_img = create_background(watermark_path, username, title, header_top_y)
    tmp_fd, bg_path = tempfile.mkstemp(suffix=".png")
    os.close(tmp_fd)
    bg_img.save(bg_path, "PNG")

    try:
        # ── FFmpeg: scale video → overlay on background ───────────────────
        filter_complex = (
            f"[0:v]scale={new_w}:{new_h}[vid];"
            f"[1:v][vid]overlay={video_x}:{video_y}:shortest=1[out]"
        )

        cmd = [
            _FFMPEG, "-y",
            "-i", input_path,                    # input 0 — video
            "-loop", "1", "-i", bg_path,         # input 1 — background (still)
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(crf),
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-2000:]}")

    finally:
        if os.path.exists(bg_path):
            os.unlink(bg_path)
