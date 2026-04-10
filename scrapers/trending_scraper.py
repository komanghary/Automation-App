"""
GitHub Trending Scraper — Discord Bot
=======================================
Collects daily and weekly GitHub trending repos.
Output: Excel with two sheets (Daily + Weekly), sorted by most stars.

Schedule: 08:00 WITA daily
"""

import os
import sys
import asyncio
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import discord
from discord.ext import tasks
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_BOT_TOKEN  = os.getenv(
    "DISCORD_BOT_TOKEN",
    "MTQ4ODkyMjc1MTA2MTE5NzA0MA.GD88E_.FhddnIp8Asw1Vmbf3Ztxe9FUpeACaQyJqlKGo8",
)
DISCORD_CHANNEL_ID = int(os.getenv("TRENDING_CHANNEL_ID", os.getenv("DISCORD_CHANNEL_ID", "1489102521946607616")))
GEMINI_API_KEYS    = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]

WITA          = timezone(timedelta(hours=8))
SCRAPE_HOUR   = 8
SCRAPE_MINUTE = 0

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

DESC_MAX_LEN = 300   # fallback truncate length if Gemini is not configured

# ── Gemini key rotation state ─────────────────────────────────────────────────
_gemini_key_index = 0


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI — BATCH DESCRIPTION SUMMARIZER
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_next_key() -> str:
    global _gemini_key_index
    if not GEMINI_API_KEYS:
        return ""
    key = GEMINI_API_KEYS[_gemini_key_index % len(GEMINI_API_KEYS)]
    _gemini_key_index += 1
    return key


def summarize_descriptions(repos: list[dict]) -> list[dict]:
    """
    Use Gemini to:
    - Summarize descriptions in plain Indonesian (orang awam friendly)
    - Rate importance 1-10
    - Give reason why this repo is worth knowing

    Falls back to truncated original + empty rating/reason if Gemini unavailable.
    """
    # Initialize fields so Excel always has columns even on fallback
    for r in repos:
        r.setdefault("rating", "")
        r.setdefault("alasan", "")

    if not GEMINI_API_KEYS:
        for r in repos:
            if len(r["description"]) > DESC_MAX_LEN:
                r["description"] = r["description"][:DESC_MAX_LEN - 1].rstrip() + "…"
        return repos

    try:
        import google.generativeai as genai
    except ImportError:
        print("  [Gemini] google-generativeai not installed — pip install google-generativeai")
        return repos

    # Build numbered list for the prompt
    lines = []
    for i, r in enumerate(repos, 1):
        name = f"{r['author']}/{r['repo']}"
        desc = r["description"] or "(no description)"
        lines.append(f"{i}. [{name}] {desc}")

    prompt = (
        "Berikut daftar repository GitHub trending. Untuk setiap repository, berikan 3 hal:\n\n"
        "1. DESKRIPSI: Jelaskan dalam Bahasa Indonesia yang mudah dipahami orang awam "
        "(yang tidak tahu programming sama sekali). Gunakan analogi sehari-hari jika perlu. "
        "Jelaskan apa fungsinya, untuk siapa, dan kenapa berguna. Maksimal 1 paragraf (3-5 kalimat).\n\n"
        "2. RATING: Nilai kepentingannya untuk diketahui publik umum, skala 1-10. "
        "(10 = sangat penting/revolusioner, 1 = hanya untuk programmer niche). "
        "Tulis HANYA angkanya.\n\n"
        "3. ALASAN: Jelaskan dalam 1-2 kalimat kenapa kita harus tahu repository ini. "
        "Fokus pada dampak nyata di kehidupan sehari-hari atau industri.\n\n"
        "Format jawaban PERSIS seperti ini (jangan tambah teks lain di luar format):\n\n"
        "---\n"
        "NOMOR: 1\n"
        "DESKRIPSI: <teks>\n"
        "RATING: <angka>\n"
        "ALASAN: <teks>\n"
        "---\n"
        "NOMOR: 2\n"
        "...\n\n"
        "Data:\n"
        + "\n".join(lines)
    )

    last_err = None
    for _ in range(len(GEMINI_API_KEYS)):
        api_key = _gemini_next_key()
        key_num = (_gemini_key_index - 1) % len(GEMINI_API_KEYS) + 1
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(prompt)
            result = response.text.strip()

            # Parse blocks separated by ---
            parsed = {}
            current = {}
            for line in result.splitlines():
                line = line.strip()
                if line == "---":
                    if "nomor" in current:
                        parsed[current["nomor"]] = current
                    current = {}
                elif line.upper().startswith("NOMOR:"):
                    try:
                        current["nomor"] = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.upper().startswith("DESKRIPSI:"):
                    current["deskripsi"] = line.split(":", 1)[1].strip()
                elif line.upper().startswith("RATING:"):
                    val = re.search(r"\d+", line.split(":", 1)[1])
                    current["rating"] = int(val.group()) if val else ""
                elif line.upper().startswith("ALASAN:"):
                    current["alasan"] = line.split(":", 1)[1].strip()
            # catch last block (no trailing ---)
            if "nomor" in current:
                parsed[current["nomor"]] = current

            applied = 0
            for i, r in enumerate(repos, 1):
                block = parsed.get(i, {})
                if block.get("deskripsi"):
                    r["description"] = block["deskripsi"]
                    applied += 1
                elif len(r["description"]) > DESC_MAX_LEN:
                    r["description"] = r["description"][:DESC_MAX_LEN - 1].rstrip() + "…"
                r["rating"] = block.get("rating", "")
                r["alasan"] = block.get("alasan", "")

            print(f"  [Gemini] Key #{key_num} — Analyzed {applied}/{len(repos)} repos")
            return repos

        except Exception as e:
            last_err = e
            print(f"  [Gemini] Key #{key_num} gagal: {e} — coba key berikutnya...")

    # All keys failed — fallback to truncate
    print(f"  [Gemini] Semua keys gagal ({last_err}), pakai truncate")
    for r in repos:
        if len(r["description"]) > DESC_MAX_LEN:
            r["description"] = r["description"][:DESC_MAX_LEN - 1].rstrip() + "…"
    return repos


# ─────────────────────────────────────────────────────────────────────────────
#  GITHUB SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

_GH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

GH_TIMEOUT    = 30   # seconds per GitHub request
GH_MAX_RETRY  = 3    # retries on timeout


def _short_desc(text: str) -> str:
    """Truncate description to DESC_MAX_LEN characters."""
    text = text.strip()
    if len(text) <= DESC_MAX_LEN:
        return text
    return text[:DESC_MAX_LEN - 1].rstrip() + "…"


def scrape_github_trending(since: str = "daily") -> list[dict]:
    """
    Scrape github.com/trending for trending repos.
    since: 'daily' or 'weekly'
    Returns list sorted by stars_period descending.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  [GitHub] BeautifulSoup not installed — run: pip install beautifulsoup4")
        return []

    url = f"https://github.com/trending?since={since}"

    resp = None
    for attempt in range(1, GH_MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=_GH_HEADERS, timeout=GH_TIMEOUT)
            resp.raise_for_status()
            break
        except Exception as e:
            print(f"  [GitHub/{since}] Attempt {attempt}/{GH_MAX_RETRY} failed: {e}")
            if attempt == GH_MAX_RETRY:
                return []
            time.sleep(3)

    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    repos = []

    for article in soup.select("article.Box-row"):
        try:
            h2 = article.select_one("h2 a")
            if not h2:
                continue
            full_name = h2.get_text(strip=True).replace(" ", "").replace("\n", "")
            parts = [p.strip() for p in full_name.split("/") if p.strip()]
            author = parts[0] if parts else ""
            repo   = parts[1] if len(parts) > 1 else full_name

            desc_el = article.select_one("p")
            desc = _short_desc(desc_el.get_text(strip=True)) if desc_el else ""

            lang_el = article.select_one("[itemprop='programmingLanguage']")
            lang = lang_el.get_text(strip=True) if lang_el else ""

            stars_total = 0
            forks = 0
            for el in article.select("a.Link--muted"):
                href = el.get("href", "")
                text = el.get_text(strip=True).replace(",", "")
                if "stargazers" in href:
                    try: stars_total = int(text)
                    except ValueError: pass
                elif "forks" in href:
                    try: forks = int(text)
                    except ValueError: pass

            stars_period_el = article.select_one("span.d-inline-block.float-sm-right")
            stars_period_text = stars_period_el.get_text(strip=True) if stars_period_el else "0"
            m = re.search(r"([\d,]+)", stars_period_text)
            stars_period = int(m.group(1).replace(",", "")) if m else 0

            repos.append({
                "repo":         repo,
                "author":       author,
                "description":  desc,
                "language":     lang,
                "stars":        stars_total,
                "stars_period": stars_period,
                "forks":        forks,
                "url":          f"https://github.com/{author}/{repo}",
                "rating":       "",
                "alasan":       "",
            })

        except Exception as e:
            print(f"  [GitHub/{since}] Parse error: {e}")
            continue

    # Sort by stars in this period (highest first), then assign rank
    repos.sort(key=lambda r: r["stars_period"], reverse=True)
    for i, r in enumerate(repos, 1):
        r["rank"] = i

    print(f"  [GitHub/{since}] {len(repos)} repos")
    return repos


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def _sheet_style(ws, headers: list[str], col_widths: list[int], title: str):
    header_font  = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill(start_color="24292E", end_color="24292E", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_font    = Font(name="Calibri", size=10)
    center_align = Alignment(horizontal="center", vertical="center")
    text_align   = Alignment(horizontal="left", vertical="center", wrap_text=True)
    number_align = Alignment(horizontal="right", vertical="center")
    thin_border  = Border(
        left=Side(style="thin", color="D9E2F3"),
        right=Side(style="thin", color="D9E2F3"),
        top=Side(style="thin", color="D9E2F3"),
        bottom=Side(style="thin", color="D9E2F3"),
    )
    even_fill = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")

    ws.merge_cells(f"A1:{get_column_letter(len(headers))}1")
    tc = ws["A1"]
    tc.value     = title
    tc.font      = Font(name="Calibri", bold=True, size=13, color="1F4E79")
    tc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=2, column=ci, value=h)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = thin_border
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[2].height = 24

    return data_font, center_align, text_align, number_align, thin_border, even_fill


def _rating_fill(rating) -> PatternFill:
    """Return cell background color based on rating value."""
    try:
        val = int(rating)
    except (TypeError, ValueError):
        return PatternFill()  # no fill
    if val >= 8:
        return PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")  # green
    if val >= 5:
        return PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")  # yellow
    return PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")      # red


def _add_github_sheet(wb: Workbook, since: str, rows: list[dict], date_str: str):
    label = "Daily" if since == "daily" else "Weekly"
    ws = wb.create_sheet(title=f"🐙 GitHub {label}")

    period_label = "⭐ Stars Today" if since == "daily" else "⭐ Stars Week"
    headers    = ["#", "Repo", "Author", "Deskripsi (AI)", "Bahasa", "⭐ Total", period_label, "🍴 Forks", "Rating\n(1-10)", "Kenapa Harus Tahu?", "URL"]
    col_widths = [5, 28, 18, 70, 13, 11, 12, 9, 10, 65, 45]
    title = f"🐙 GitHub Trending {label} — {date_str}"

    data_font, center, text, number, border, even = _sheet_style(ws, headers, col_widths, title)

    if not rows:
        ws.cell(row=3, column=1, value="❌ No data — GitHub scraping failed")
        return

    for i, row in enumerate(rows):
        r = i + 3
        rating = row.get("rating", "")
        values = [row["rank"], row["repo"], row["author"], row["description"],
                  row["language"], row["stars"], row["stars_period"],
                  row["forks"], rating, row.get("alasan", ""), row["url"]]
        aligns = [center, text, text, text, center, number, number, number, center, text, text]
        for ci, (val, align) in enumerate(zip(values, aligns), 1):
            cell = ws.cell(row=r, column=ci, value=val)
            cell.font      = data_font
            cell.alignment = align
            cell.border    = border
            # Rating column gets color-coded fill, others get alternating fill
            if ci == 9:
                cell.fill = _rating_fill(rating)
                cell.font = Font(name="Calibri", size=10, bold=True)
            elif i % 2 == 0:
                cell.fill = even
            if ci in (6, 7, 8):
                cell.number_format = "#,##0"

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(headers))}{len(rows) + 2}"
    ws.row_dimensions[2].height = 30


def build_excel(daily: list[dict], weekly: list[dict], date_str: str) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    _add_github_sheet(wb, "daily",  daily,  date_str)
    _add_github_sheet(wb, "weekly", weekly, date_str)
    path = OUTPUT_DIR / f"github_trending_{date_str}.xlsx"
    wb.save(path)
    return path



# ─────────────────────────────────────────────────────────────────────────────
#  DISCORD BOT
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

_scrape_lock = asyncio.Lock()
_last_scrape_date = ""


async def send_file_with_retry(channel, path: Path, caption: str, retries: int = 3):
    """Send a single file to Discord with retry on timeout."""
    for attempt in range(1, retries + 1):
        try:
            await channel.send(
                caption,
                file=discord.File(str(path), filename=path.name),
            )
            return True
        except Exception as e:
            print(f"  [Discord] Upload attempt {attempt}/{retries} gagal: {e}")
            if attempt < retries:
                await asyncio.sleep(5)
    await channel.send(f"⚠️ Gagal upload `{path.name}` setelah {retries}x percobaan.")
    return False


async def run_scrape():
    global _last_scrape_date

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print(f"[GitHub Trending] Channel {DISCORD_CHANNEL_ID} not found!")
        return

    date_str = datetime.now(WITA).strftime("%Y-%m-%d")
    now_str  = datetime.now(WITA).strftime("%Y-%m-%d %H:%M WITA")

    print(f"\n{'='*60}")
    print(f"  GitHub Trending Scraper — {now_str}")
    print(f"{'='*60}")

    start_embed = discord.Embed(
        title="🔄 GitHub Trending Scraper Dimulai",
        description=f"Mengambil data daily & weekly trending...\n{now_str}",
        color=0x24292E,
        timestamp=datetime.now(WITA),
    )
    await channel.send(embed=start_embed)

    loop = asyncio.get_event_loop()

    # Daily
    await channel.send("▶️ **Scraping GitHub Daily Trending...**")
    try:
        daily = await loop.run_in_executor(None, scrape_github_trending, "daily")
        await channel.send(f"📅 **Daily selesai** — {len(daily)} repos (sorted by stars today)")
    except Exception as e:
        daily = []
        await channel.send(f"❌ Daily gagal: {e}")

    # Weekly
    await channel.send("▶️ **Scraping GitHub Weekly Trending...**")
    try:
        weekly = await loop.run_in_executor(None, scrape_github_trending, "weekly")
        await channel.send(f"📆 **Weekly selesai** — {len(weekly)} repos (sorted by stars this week)")
    except Exception as e:
        weekly = []
        await channel.send(f"❌ Weekly gagal: {e}")

    # Summarize descriptions with Gemini
    if GEMINI_API_KEYS:
        await channel.send("🤖 **Meringkas deskripsi dengan Gemini AI...**")
    try:
        if daily:
            daily  = await loop.run_in_executor(None, summarize_descriptions, daily)
        if weekly:
            weekly = await loop.run_in_executor(None, summarize_descriptions, weekly)
        if GEMINI_API_KEYS:
            await channel.send("✅ **Deskripsi selesai diringkas**")
    except Exception as e:
        await channel.send(f"⚠️ Ringkas deskripsi gagal, pakai teks asli: {e}")

    # Build Excel
    excel_path = None
    try:
        excel_path = build_excel(daily, weekly, date_str)
        print(f"\n[GitHub Trending] Excel: {excel_path.name}")
    except Exception as e:
        print(f"[GitHub Trending] Excel error: {e}")
        await channel.send(f"⚠️ Gagal buat Excel: {e}")

    summary_embed = discord.Embed(
        title="✅ GitHub Trending Selesai",
        color=0x2ECC71,
        timestamp=datetime.now(WITA),
    )
    summary_embed.add_field(name="📅 Daily",  value=f"{len(daily)} repos",  inline=True)
    summary_embed.add_field(name="📆 Weekly", value=f"{len(weekly)} repos", inline=True)
    await channel.send(embed=summary_embed)

    if excel_path and excel_path.exists():
        await send_file_with_retry(channel, excel_path, "📊 **Excel:**")

    _last_scrape_date = date_str


@tasks.loop(minutes=1)
async def scrape_scheduler():
    global _last_scrape_date
    now = datetime.now(WITA)
    if _last_scrape_date == now.strftime("%Y-%m-%d"):
        return
    if now.hour != SCRAPE_HOUR or now.minute != SCRAPE_MINUTE:
        return
    async with _scrape_lock:
        await run_scrape()


@scrape_scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()
    print(f"[Scheduler] Active — scrape jam {SCRAPE_HOUR:02d}:{SCRAPE_MINUTE:02d} WITA setiap hari")


@bot.event
async def on_ready():
    print(f"\n[Bot] Online sebagai {bot.user} (ID: {bot.user.id})")
    print(f"[Bot] Channel: {DISCORD_CHANNEL_ID}")
    print(f"[Bot] Platform: GitHub (Daily + Weekly)")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name="GitHub Trending 🐙"),
    )
    if not scrape_scheduler.is_running():
        scrape_scheduler.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if message.channel.id != DISCORD_CHANNEL_ID:
        return

    content = message.content.strip().lower()

    if content == "!trending":
        async with _scrape_lock:
            await message.reply("🔄 Memulai scraping GitHub trending...")
            await run_scrape()

    elif content == "!status":
        now = datetime.now(WITA)
        scraped = "✅ Ya" if _last_scrape_date == now.strftime("%Y-%m-%d") else "❌ Belum"
        embed = discord.Embed(title="🐙 GitHub Trending Scraper Status", color=0x24292E)
        embed.add_field(name="Waktu",            value=now.strftime("%Y-%m-%d %H:%M WITA"), inline=True)
        embed.add_field(name="Scraped Hari Ini", value=scraped,                             inline=True)
        embed.add_field(name="Jadwal",           value=f"Setiap hari jam {SCRAPE_HOUR:02d}:00 WITA", inline=True)
        embed.add_field(name="Data",   value="📅 Daily + 📆 Weekly trending repos", inline=False)
        ai_status = f"✅ {len(GEMINI_API_KEYS)} key(s)" if GEMINI_API_KEYS else "❌ Tidak dikonfigurasi"
        embed.add_field(name="Gemini AI", value=ai_status, inline=False)
        await message.reply(embed=embed)

    elif content == "!help":
        embed = discord.Embed(
            title="📖 GitHub Trending Scraper — Help",
            description="Ambil data GitHub trending setiap hari jam 08:00 WITA",
            color=0x24292E,
        )
        embed.add_field(name="!trending", value="Jalankan scraping sekarang", inline=False)
        embed.add_field(name="!status",   value="Lihat status bot",           inline=False)
        await message.reply(embed=embed)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  GitHub Trending Scraper Bot")
    print(f"  Data     : Daily + Weekly")
    print(f"  Sort     : Stars terbanyak (period)")
    print(f"  Schedule : {SCRAPE_HOUR:02d}:00 WITA setiap hari")
    print(f"  Gemini   : {'✅ ' + str(len(GEMINI_API_KEYS)) + ' key(s)' if GEMINI_API_KEYS else '❌ Tidak dikonfigurasi (set GEMINI_API_KEYS di .env)'}")
    print(f"  Command  : !trending (manual trigger)")
    print("=" * 60)
    bot.run(DISCORD_BOT_TOKEN)
