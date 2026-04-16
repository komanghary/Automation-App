"""
Instagram service — login, group threads, video extraction, download.
"""

import os
import re
import random
import subprocess
import requests

from instagrapi import Client as IGClient
from instagrapi.exceptions import ChallengeRequired

# Patch MediaXma.video_url to be optional (some IG messages have video_url=None)
try:
    from typing import Optional
    import instagrapi.types as _ig_types
    import instagrapi.extractors as _ig_ext
    class _PatchedMediaXma(_ig_types.MediaXma):
        video_url: Optional[str] = None
    _ig_types.MediaXma = _PatchedMediaXma
    _ig_ext.MediaXma = _PatchedMediaXma
except Exception:
    pass

from config import BASE_DIR, FFMPEG, TEMP_DIR


# ── Anti-detection helpers ─────────────────────────────────────────────────────

# Pool of realistic Android device fingerprints
_DEVICE_POOL = [
    {
        "manufacturer": "Samsung",
        "model": "SM-G991B",
        "android_version": "13",
        "android_release": "13",
        "app_version": "319.0.0.34.109",
    },
    {
        "manufacturer": "Samsung",
        "model": "SM-S908B",
        "android_version": "13",
        "android_release": "13",
        "app_version": "317.0.0.24.109",
    },
    {
        "manufacturer": "Xiaomi",
        "model": "2201116SG",
        "android_version": "12",
        "android_release": "12",
        "app_version": "315.0.0.24.109",
    },
    {
        "manufacturer": "OnePlus",
        "model": "CPH2423",
        "android_version": "13",
        "android_release": "13",
        "app_version": "319.0.0.34.109",
    },
]

_DEVICE_FILE = os.path.join(BASE_DIR, "data", "ig_device.json")


def _load_or_create_device() -> dict:
    """Load persisted device fingerprint, or create a new random one."""
    import json
    if os.path.exists(_DEVICE_FILE):
        try:
            with open(_DEVICE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    device = random.choice(_DEVICE_POOL).copy()
    os.makedirs(os.path.dirname(_DEVICE_FILE), exist_ok=True)
    with open(_DEVICE_FILE, "w", encoding="utf-8") as f:
        json.dump(device, f, indent=2)
    print(f"[IG] Device fingerprint baru: {device['manufacturer']} {device['model']}")
    return device


def _make_ig_client() -> IGClient:
    cl = IGClient()
    # Realistic human-like delays (3–8 detik antar request)
    cl.delay_range = [3, 8]

    # Set device fingerprint yang konsisten
    device = _load_or_create_device()
    try:
        cl.set_device({
            "app_version":      device["app_version"],
            "android_version":  int(device["android_version"]),
            "android_release":  device["android_release"],
            "dpi":              "420dpi",
            "resolution":       "1080x2340",
            "manufacturer":     device["manufacturer"],
            "device":           device["model"],
            "model":            device["model"],
            "cpu":              "qcom",
            "version_code":     "314665256",
        })
    except Exception:
        pass

    # Challenge handler (tidak bisa auto-resolve bloks challenge)
    def _challenge_code_handler(username, choice):
        print(f"[IG] Kode verifikasi diperlukan untuk {username} (choice={choice})")
        return ""  # return kosong — tidak bisa input di server
    cl.challenge_code_handler = _challenge_code_handler
    return cl


def ig_login(username: str, password: str) -> IGClient:
    session_file = os.path.join(BASE_DIR, f"{username}_session.json")

    # Coba load sesi yang ada (hindari login ulang = trigger challenge)
    if os.path.exists(session_file):
        print("[IG] Memuat sesi tersimpan...")
        cl = _make_ig_client()
        try:
            cl.load_settings(session_file)
            # Validasi sesi dengan request ringan
            cl.get_timeline_feed()
            print("[IG] Sesi masih valid.")
            cl.dump_settings(session_file)  # refresh token
            return cl
        except ChallengeRequired:
            print("[IG] Sesi trigger challenge — hapus sesi, perlu verifikasi manual.")
            try:
                os.remove(session_file)
                # Juga reset device fingerprint supaya terlihat seperti device baru
                if os.path.exists(_DEVICE_FILE):
                    os.remove(_DEVICE_FILE)
            except Exception:
                pass
            _print_manual_steps(username)
            raise
        except Exception as e:
            print(f"[IG] Sesi tidak valid ({type(e).__name__}), login ulang...")
            try:
                os.remove(session_file)
            except Exception:
                pass

    # Fresh login dengan device fingerprint baru
    print("[IG] Login fresh dengan akun dan device baru...")
    cl = _make_ig_client()
    try:
        import time as _t
        _t.sleep(random.uniform(2, 5))  # jeda sebelum login
        cl.login(username, password)
        cl.dump_settings(session_file)
        print("[IG] Login berhasil!")
        return cl
    except ChallengeRequired:
        print("[IG] Instagram tetap minta challenge setelah fresh login.")
        _print_manual_steps(username)
        raise
    except Exception as e:
        print(f"[IG] Login gagal: {e}")
        raise


def _print_manual_steps(username: str):
    print("=" * 55)
    print("[IG] VERIFIKASI MANUAL DIPERLUKAN:")
    print(f"[IG] 1. Buka Instagram di HP/browser")
    print(f"[IG] 2. Login akun: {username}")
    print("[IG] 3. Selesaikan verifikasi (email/SMS/popup)")
    print("[IG] 4. Klik 'Dismiss' kalau muncul popup bot warning")
    print("[IG] 5. Setelah berhasil, restart Watcher dari dashboard")
    print("=" * 55)


# ── Group threads ─────────────────────────────────────────────────────────────

def get_group_threads(cl: IGClient):
    print("\n[IG] Mengambil daftar Group Chat...")
    threads = cl.direct_threads(amount=20)
    groups = []
    for t in threads:
        if len(t.users) > 1:
            names = ", ".join(u.username for u in t.users[:5])
            if len(t.users) > 5:
                names += f" +{len(t.users)-5} lainnya"
            title = t.thread_title or f"Grup: {names}"
            groups.append((t.id, title))
    return groups


# ── Video extraction ──────────────────────────────────────────────────────────

def best_video_url(media) -> str | None:
    if media is None:
        return None
    best_url, best_score = None, 0
    for v in (getattr(media, 'video_versions', None) or []):
        try:
            w   = int(getattr(v, 'width', 0) or 0)
            h   = int(getattr(v, 'height', 0) or 0)
            url = getattr(v, 'url', None)
            if url and w * h > best_score:
                best_score = w * h
                best_url   = str(url)
        except Exception:
            continue
    if not best_url:
        vu = getattr(media, 'video_url', None)
        if vu:
            best_url = str(vu)
    if not best_url:
        for res in (getattr(media, 'resources', None) or []):
            url = best_video_url(res)
            if url:
                best_url = url
                break
    return best_url


def shortcode_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r'instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_\-]+)', str(url))
    return m.group(1) if m else None


def extract_from_xma_clip(cl: IGClient, msg) -> tuple:
    xma = getattr(msg, 'xma_share', None)
    if xma is None:
        return None, 'xma_share tidak ada'

    def get(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    ig_url = get(xma, 'video_url')
    shortcode = shortcode_from_url(ig_url)
    if shortcode:
        try:
            pk    = cl.media_pk_from_code(shortcode)
            media = cl.media_info(pk)
            url   = best_video_url(media)
            if url:
                return url, f"shortcode({shortcode})"
        except Exception as e:
            print(f"      [IG] Gagal fetch shortcode {shortcode}: {e}")

    fbid = get(xma, 'preview_media_fbid')
    if fbid:
        try:
            media = cl.media_info(int(fbid))
            url   = best_video_url(media)
            if url:
                return url, 'preview_media_fbid'
        except Exception as e:
            print(f"      [IG] Gagal fetch fbid {fbid}: {e}")

    return None, 'xma_clip(gagal)'


def extract_video(cl: IGClient, msg) -> tuple:
    item_type = getattr(msg, 'item_type', '') or ''

    if item_type == 'xma_clip':
        return extract_from_xma_clip(cl, msg)

    if item_type == 'media_share':
        ms = getattr(msg, 'media_share', None)
        if ms:
            url = best_video_url(ms)
            if url:
                return url, 'media_share'
            pk = getattr(ms, 'pk', None) or getattr(ms, 'id', None)
            if pk:
                try:
                    url = best_video_url(cl.media_info(pk))
                    if url:
                        return url, 'media_share(api)'
                except Exception:
                    pass

    if item_type == 'clip':
        clip = getattr(msg, 'clip', None)
        if clip:
            obj = clip.get('clip', clip) if isinstance(clip, dict) else clip
            url = best_video_url(obj)
            if url:
                return url, 'clip'

    if item_type == 'media':
        url = best_video_url(getattr(msg, 'media', None))
        if url:
            return url, 'media'

    if item_type == 'raven_media':
        vm = getattr(msg, 'visual_media', None)
        if vm:
            url = best_video_url(getattr(vm, 'media', None))
            if url:
                return url, 'raven_media'

    return None, item_type


# ── Download & compress ───────────────────────────────────────────────────────

def download_video(url: str, filepath: str) -> bool:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, stream=True, timeout=120, headers=headers)
        r.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"   [DL] Tersimpan — {size_mb:.1f} MB")
        return True
    except Exception as e:
        print(f"   [DL] Gagal download: {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return False


def compress_for_preview(hd_path: str, preview_path: str) -> bool:
    """Compress HD video to a small preview for Discord upload (CRF 35, 480p max)."""
    try:
        cmd = [
            FFMPEG, "-y",
            "-i", hd_path,
            "-vf", "scale='min(480,iw)':-2",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "35",
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            preview_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"   [Compress] FFmpeg error: {result.stderr[-500:]}")
            return False
        hd_mb = os.path.getsize(hd_path) / (1024 * 1024)
        pv_mb = os.path.getsize(preview_path) / (1024 * 1024)
        print(f"   [Compress] HD: {hd_mb:.1f} MB -> Preview: {pv_mb:.1f} MB")
        return True
    except Exception as e:
        print(f"   [Compress] Gagal: {e}")
        if os.path.exists(preview_path):
            os.remove(preview_path)
        return False
