"""
Overlay Watermark Processor
============================
Two modes:
  1. Film     — overlay a PNG (Coroco.png) with opacity, no title
  2. Motivasi — overlay a looping MP4 (Absolutegrowt.mp4) with screen blend + opacity

Both:
  - If video is not 9:16, pad with black bars (top/bottom) to 1080x1920
  - Watermark: 30% of original size (Film) / 15% (Motivasi), 50% opacity
  - Position: centered horizontally, between center and bottom (75% from top)
"""

import os
import subprocess
import json

# ── FFmpeg path ──────────────────────────────────────────────────────────────
_FFMPEG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft", "WinGet", "Links",
)
_FFMPEG = (
    os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
    if os.path.isfile(os.path.join(_FFMPEG_DIR, "ffmpeg.exe"))
    else "ffmpeg"
)
_FFPROBE = (
    os.path.join(_FFMPEG_DIR, "ffprobe.exe")
    if os.path.isfile(os.path.join(_FFMPEG_DIR, "ffprobe.exe"))
    else "ffprobe"
)

TARGET_W = 1080
TARGET_H = 1920


def _get_video_info(path: str) -> tuple[int, int]:
    """Return (width, height) of video."""
    cmd = [
        _FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError(f"No video stream found in {path}")


def process_film(
    input_path: str,
    output_path: str,
    watermark_png: str,
    crf: int = 23,
) -> None:
    """
    Film watermark: overlay PNG logo with 50% opacity, 30% scale.
    Non-9:16 videos get black bars.
    """
    src_w, src_h = _get_video_info(input_path)

    # Determine if we need black bars (pad to 9:16)
    aspect = src_h / src_w
    is_916 = aspect >= 1.5  # 9:16 = 1.78

    if is_916:
        # Scale to fit 1080 wide, keep aspect
        vid_filter = f"scale={TARGET_W}:-2:flags=lanczos"
    else:
        # Scale to fit within 1080x1920, then pad with black
        vid_filter = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black"
        )

    # Watermark: scale 30%, opacity 50%
    # Position: centered X, Y at 75% from top (between center and bottom)
    filter_complex = (
        f"[0:v]{vid_filter}[bg];"
        f"[1:v]scale=iw*0.30:-1,format=rgba,colorchannelmixer=aa=0.5[wm];"
        f"[bg][wm]overlay=(W-w)/2:3*(H)/4-h/2[out]"
    )

    cmd = [
        _FFMPEG, "-y",
        "-i", input_path,
        "-loop", "1", "-i", watermark_png,  # loop PNG so it lasts full video
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

    print(f"[Film] Watermark selesai: {output_path}")


def process_motivational(
    input_path: str,
    output_path: str,
    watermark_mp4: str,
    crf: int = 23,
) -> None:
    """
    Motivational watermark: overlay looping MP4 with screen blend (removes black),
    50% opacity, 15% scale. Non-9:16 videos get black bars.
    """
    src_w, src_h = _get_video_info(input_path)

    aspect = src_h / src_w
    is_916 = aspect >= 1.5

    if is_916:
        vid_filter = f"scale={TARGET_W}:-2:flags=lanczos"
    else:
        vid_filter = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black"
        )

    # Watermark MP4: scale 15%, no loop, remove black via colorkey, opacity 50%
    # colorkey removes black pixels making them transparent (screen-like effect)
    # Position: centered X, Y at 75% from top
    # shortest=0 so video continues after watermark ends (not cut)
    filter_complex = (
        f"[0:v]{vid_filter}[bg];"
        f"[1:v]scale=iw*0.15:-1,colorkey=black:0.3:0.15,format=rgba,"
        f"colorchannelmixer=aa=0.5[wm];"
        f"[bg][wm]overlay=(W-w)/2:3*(H)/4-h/2:shortest=0[out]"
    )

    cmd = [
        _FFMPEG, "-y",
        "-i", input_path,
        "-i", watermark_mp4,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-2000:]}")

    print(f"[Motivational] Watermark selesai: {output_path}")


def process_bola_geming(
    input_path: str,
    output_path: str,
    watermark_mp4: str,
    crf: int = 23,
) -> None:
    """
    Bola Geming watermark:
    - If video not 9:16, pad with black bars to 1080x1920
    - Remove greenscreen from watermark MP4 (chromakey)
    - Scale watermark to 30% width, position between center and bottom (75% from top)
    - Watermark plays to its midpoint, then freezes on last frame until video ends
    """
    src_w, src_h = _get_video_info(input_path)
    aspect = src_h / src_w
    is_916 = aspect >= 1.5

    if is_916:
        vid_filter = f"scale={TARGET_W}:-2:flags=lanczos"
    else:
        vid_filter = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black"
        )

    # Watermark pipeline:
    #   1. trim to first 5 seconds, freeze last frame for rest of video
    #   2. remove greenscreen — exact color #5efd00 sampled from watermark
    #   3. scale to 22% width, opacity 30%
    wm_filter = (
        f"trim=0:5,setpts=PTS-STARTPTS,"
        f"tpad=stop_mode=clone:stop_duration=10000,"
        f"chromakey=0x5efd00:0.15:0.05,"
        f"format=rgba,"
        f"colorchannelmixer=aa=0.3,"
        f"scale=iw*0.22:-1"
    )

    filter_complex = (
        f"[0:v]{vid_filter}[bg];"
        f"[1:v]{wm_filter}[wm];"
        f"[bg][wm]overlay=(W-w)/2:3*(H)/4-h/2:eof_action=pass:format=auto[out]"
    )

    cmd = [
        _FFMPEG, "-y",
        "-i", input_path,
        "-i", watermark_mp4,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-2000:]}")

    print(f"[BolaGeming] Watermark selesai: {output_path}")
