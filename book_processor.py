"""
Book Processor — 1% Perday
===========================
PDF → Gemini AI analysis → Edge TTS audio podcasts → Dashboard

Flow:
1. Extract text + cover from PDF (PyMuPDF)
2. Gemini: analyze book, split into 3 parts, write podcast scripts
3. Edge TTS: convert scripts to MP3 (2 hosts: male + female)
4. Gemini: write 3 deep chapter analyses with sub-chapters & narratives
5. Save to data/books/{book_id}/
"""

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
BOOKS_DIR = BASE_DIR / "data" / "books"
BOOKS_DIR.mkdir(parents=True, exist_ok=True)

WITA = timezone(timedelta(hours=8))

# Gemini key rotation
_GEMINI_KEYS: list[str] = []
_gemini_idx = 0

def _init_gemini_keys():
    global _GEMINI_KEYS
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    raw = os.getenv("GEMINI_API_KEYS", "")
    _GEMINI_KEYS = [k.strip() for k in raw.split(",") if k.strip()]

def _next_gemini_key() -> str:
    global _gemini_idx
    if not _GEMINI_KEYS:
        _init_gemini_keys()
    key = _GEMINI_KEYS[_gemini_idx % len(_GEMINI_KEYS)]
    _gemini_idx += 1
    return key


# ── PDF Extraction ─────────────────────────────────────────────────────────────

def extract_pdf(pdf_path: str) -> tuple[str, str | None]:
    """
    Returns (full_text, cover_image_path).
    cover_image_path is None if extraction fails.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    full_text = ""
    for page in doc:
        full_text += page.get_text()

    # Extract cover: render first page as image
    cover_path = None
    try:
        page = doc[0]
        mat = fitz.Matrix(2, 2)  # 2x zoom for quality
        pix = page.get_pixmap(matrix=mat)
        book_id = _make_id(pdf_path)
        cover_dir = BOOKS_DIR / book_id
        cover_dir.mkdir(parents=True, exist_ok=True)
        cover_path = str(cover_dir / "cover.jpg")
        pix.save(cover_path)
    except Exception as e:
        print(f"  [BookProcessor] Cover extraction failed: {e}")

    doc.close()
    return full_text.strip(), cover_path


def _make_id(pdf_path: str) -> str:
    """Stable ID from file content hash."""
    h = hashlib.md5(open(pdf_path, "rb").read(65536)).hexdigest()[:10]
    ts = datetime.now(WITA).strftime("%Y%m%d")
    return f"{ts}_{h}"


# ── Gemini Helper ──────────────────────────────────────────────────────────────

def _gemini_call(prompt: str, max_retries: int = 3) -> str:
    import google.generativeai as genai

    if not _GEMINI_KEYS:
        _init_gemini_keys()

    last_err = None
    for _ in range(max_retries * len(_GEMINI_KEYS)):
        key = _next_gemini_key()
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel("gemini-2.5-flash")
            resp = model.generate_content(prompt)
            return resp.text.strip()
        except Exception as e:
            last_err = e
            print(f"  [Gemini] Error: {e} — retrying...")
            time.sleep(2)

    raise RuntimeError(f"Gemini semua key gagal: {last_err}")


# ── Step 1: Book Metadata ──────────────────────────────────────────────────────

def analyze_book_metadata(text: str) -> dict:
    """Extract title, author, description from book text."""
    sample = text[:3000]
    prompt = f"""Dari teks buku berikut, ekstrak informasi ini dalam format JSON:

{{
  "title": "judul buku",
  "author": "nama penulis",
  "genre": "genre buku (self-help / novel / bisnis / dll)",
  "description": "deskripsi singkat buku dalam Bahasa Indonesia, 2-3 kalimat, mudah dipahami orang awam",
  "language": "bahasa buku (Indonesia/English/dll)"
}}

Teks buku:
{sample}

Jawab HANYA dengan JSON, tanpa teks lain."""

    raw = _gemini_call(prompt)
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {
            "title": "Buku Tanpa Judul",
            "author": "Tidak diketahui",
            "genre": "Umum",
            "description": "Buku ini sedang diproses.",
            "language": "Indonesia",
        }


# ── Step 2: Split Text into 3 Parts ───────────────────────────────────────────

def split_book_text(text: str) -> tuple[str, str, str]:
    """Split text into beginning (33%), middle (33%), end (34%)."""
    words = text.split()
    total = len(words)
    third = total // 3

    part1 = " ".join(words[:third])
    part2 = " ".join(words[third: third * 2])
    part3 = " ".join(words[third * 2:])
    return part1, part2, part3


# ── Step 3: Podcast Scripts ────────────────────────────────────────────────────

PODCAST_LABELS = {
    1: "Awal Buku",
    2: "Pertengahan Buku",
    3: "Akhir Buku & Kesimpulan",
}

def generate_podcast_script(part_text: str, part_num: int, book_title: str) -> str:
    """Generate 2-host podcast dialogue for one part."""
    label = PODCAST_LABELS[part_num]
    sample = part_text[:4000]

    prompt = f"""Kamu adalah penulis skrip podcast populer. Buat skrip podcast bagian "{label}" dari buku "{book_title}".

Format: percakapan antara 2 host:
- HOST_A: [nama: Ardi — pria, antusias, suka analogi]
- HOST_B: [nama: Sari — wanita, kritis, suka pertanyaan mendalam]

Aturan:
- Minimal 15 giliran bicara bergantian (bukan terlalu pendek)
- Bahasa Indonesia santai tapi informatif
- Ceritakan insight penting dari bagian buku ini
- Gunakan analogi sehari-hari
- Tutup dengan teaser ke bagian berikutnya (kecuali bagian 3: tutup dengan motivasi)
- Format PERSIS seperti ini:

HOST_A: [teks]
HOST_B: [teks]
HOST_A: [teks]
...

Isi buku bagian {part_num}:
{sample}

Tulis skrip sekarang:"""

    return _gemini_call(prompt)


# ── Step 4: Edge TTS Audio ─────────────────────────────────────────────────────

async def script_to_audio(script: str, output_path: str) -> bool:
    """Convert 2-host script to MP3 using Edge TTS."""
    import edge_tts

    VOICE_A = "id-ID-ArdiNeural"    # Male
    VOICE_B = "id-ID-GadisNeural"   # Female

    # Parse lines
    segments: list[tuple[str, str]] = []
    for line in script.splitlines():
        line = line.strip()
        if line.startswith("HOST_A:"):
            text = line[7:].strip()
            if text:
                segments.append((VOICE_A, text))
        elif line.startswith("HOST_B:"):
            text = line[7:].strip()
            if text:
                segments.append((VOICE_B, text))

    if not segments:
        print("  [TTS] No segments found in script")
        return False

    # Generate audio for each segment, combine
    import tempfile
    tmp_files: list[str] = []

    try:
        for i, (voice, text) in enumerate(segments):
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(tmp.name)
            tmp_files.append(tmp.name)

        # Concatenate with ffmpeg
        list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        for f in tmp_files:
            list_file.write(f"file '{f}'\n")
        list_file.close()

        ffmpeg = _find_ffmpeg()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", list_file.name, "-c", "copy", output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    finally:
        for f in tmp_files:
            try: os.unlink(f)
            except: pass
        try: os.unlink(list_file.name)
        except: pass


def _find_ffmpeg() -> str:
    import shutil
    winget = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"
    )
    if os.path.exists(winget):
        return winget
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"


# ── Step 5: Deep Chapter Analysis ─────────────────────────────────────────────

def generate_chapter_analysis(part_text: str, part_num: int, book_title: str) -> dict:
    """Generate deep chapter analysis with sub-chapters and key narratives."""
    label = PODCAST_LABELS[part_num]
    sample = part_text[:5000]

    prompt = f"""Kamu adalah analis buku profesional. Buat pembahasan mendalam bagian "{label}" dari buku "{book_title}".

Tulis dalam format JSON berikut:

{{
  "title": "judul untuk bagian ini",
  "overview": "ringkasan 2-3 paragraf bagian ini dalam Bahasa Indonesia",
  "sub_chapters": [
    {{
      "title": "judul sub-bab",
      "content": "penjelasan mendalam 1-2 paragraf",
      "key_narratives": ["kutipan/narasi penting 1", "kutipan/narasi penting 2"]
    }}
  ],
  "key_lessons": ["pelajaran utama 1", "pelajaran utama 2", "pelajaran utama 3"],
  "memorable_quote": "kutipan paling berkesan dari bagian ini"
}}

Buat 3-4 sub-bab. Sertakan narasi-narasi asli yang penting dari teks.

Teks buku bagian {part_num}:
{sample}

Jawab HANYA dengan JSON, tanpa teks lain."""

    raw = _gemini_call(prompt)
    raw = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"  [BookProcessor] Chapter JSON parse error: {e}")
        return {
            "title": label,
            "overview": "Analisis sedang diproses.",
            "sub_chapters": [],
            "key_lessons": [],
            "memorable_quote": "",
        }


# ── Main Orchestrator ──────────────────────────────────────────────────────────

async def process_book(pdf_path: str, progress_cb=None) -> str:
    """
    Full pipeline: PDF → analysis → audio → save.
    Returns book_id.
    progress_cb(message: str) called at each step.
    """
    def log(msg: str):
        print(f"  [BookProcessor] {msg}")
        if progress_cb:
            asyncio.create_task(progress_cb(msg)) if asyncio.get_event_loop().is_running() else None

    # 1. Extract text + cover
    log("📄 Mengekstrak teks dan cover dari PDF...")
    text, cover_path = extract_pdf(pdf_path)
    if not text:
        raise ValueError("PDF tidak bisa diekstrak — mungkin PDF scan (gambar)")

    book_id = _make_id(pdf_path)
    book_dir = BOOKS_DIR / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    # Save raw text
    (book_dir / "full_text.txt").write_text(text, encoding="utf-8")

    # 2. Metadata
    log("🧠 Menganalisis metadata buku dengan Gemini...")
    metadata = analyze_book_metadata(text)
    log(f"📚 Judul: {metadata['title']}")

    # 3. Split into 3 parts
    log("✂️ Membagi buku menjadi 3 bagian...")
    part1, part2, part3 = split_book_text(text)
    parts = [part1, part2, part3]

    # 4. Generate podcast scripts + audio
    podcast_data = []
    for i, part_text in enumerate(parts, 1):
        label = PODCAST_LABELS[i]
        log(f"🎙️ Membuat skrip podcast bagian {i} ({label})...")
        script = generate_podcast_script(part_text, i, metadata["title"])
        script_path = book_dir / f"podcast_{i}_script.txt"
        script_path.write_text(script, encoding="utf-8")

        log(f"🔊 Mengubah skrip ke audio (Edge TTS) bagian {i}...")
        audio_path = str(book_dir / f"podcast_{i}.mp3")
        success = await script_to_audio(script, audio_path)
        if not success:
            log(f"⚠️ Audio bagian {i} gagal dibuat — script tetap tersedia")
            audio_path = None

        podcast_data.append({
            "part": i,
            "label": label,
            "script_file": f"podcast_{i}_script.txt",
            "audio_file": f"podcast_{i}.mp3" if success else None,
            "has_audio": success,
        })

    # 5. Deep chapter analyses
    chapters = []
    for i, part_text in enumerate(parts, 1):
        label = PODCAST_LABELS[i]
        log(f"📖 Membuat analisis mendalam bagian {i} ({label})...")
        ch = generate_chapter_analysis(part_text, i, metadata["title"])
        ch["part"] = i
        ch["label"] = label
        chapters.append(ch)

    # 6. Save final metadata
    log("💾 Menyimpan hasil...")
    book_meta = {
        "id": book_id,
        "title": metadata.get("title", "Unknown"),
        "author": metadata.get("author", "Unknown"),
        "genre": metadata.get("genre", ""),
        "description": metadata.get("description", ""),
        "language": metadata.get("language", ""),
        "cover_file": "cover.jpg" if cover_path else None,
        "podcasts": podcast_data,
        "chapters": chapters,
        "pdf_name": Path(pdf_path).name,
        "created_at": datetime.now(WITA).isoformat(),
        "word_count": len(text.split()),
    }

    (book_dir / "metadata.json").write_text(
        json.dumps(book_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"✅ Selesai! Book ID: {book_id}")
    return book_id


# ── API Helpers (called from web_monitor.py) ───────────────────────────────────

def list_books() -> list[dict]:
    """Return list of all processed books (summary only)."""
    books = []
    for d in sorted(BOOKS_DIR.iterdir(), reverse=True):
        meta_file = d / "metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            books.append({
                "id":          meta["id"],
                "title":       meta["title"],
                "author":      meta["author"],
                "genre":       meta.get("genre", ""),
                "description": meta["description"],
                "cover":       f"/api/books/{meta['id']}/cover" if meta.get("cover_file") else None,
                "created_at":  meta["created_at"],
                "has_audio":   any(p["has_audio"] for p in meta.get("podcasts", [])),
                "word_count":  meta.get("word_count", 0),
            })
        except Exception:
            continue
    return books


def get_book(book_id: str) -> dict | None:
    """Return full book metadata including chapters."""
    meta_file = BOOKS_DIR / book_id / "metadata.json"
    if not meta_file.exists():
        return None
    return json.loads(meta_file.read_text(encoding="utf-8"))


def get_book_cover_path(book_id: str) -> str | None:
    p = BOOKS_DIR / book_id / "cover.jpg"
    return str(p) if p.exists() else None


def get_book_audio_path(book_id: str, part: int) -> str | None:
    p = BOOKS_DIR / book_id / f"podcast_{part}.mp3"
    return str(p) if p.exists() else None


def get_book_script(book_id: str, part: int) -> str | None:
    p = BOOKS_DIR / book_id / f"podcast_{part}_script.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None
