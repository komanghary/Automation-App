# Coroco House Bot

An automated Discord bot for managing Instagram content → watermark → YouTube upload, with a GitHub Trending scraper.

> **Bahasa Indonesia:** Bot Discord otomatis untuk mengelola konten Instagram → watermark → YouTube upload, dilengkapi scraper GitHub Trending.

---

## Table of Contents / Daftar Isi

- [Project Structure](#project-structure)
- [Components](#components)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment Configuration](#environment-configuration)
- [Running the Bots](#running-the-bots)
- [How It Works](#how-it-works)
- [Notes](#notes)

---

## Project Structure

```
Coroco-House/
├── bot.py                  # Main bot entry point
├── processor.py            # Text watermark renderer (PIL)
├── processor_overlay.py    # Video overlay watermark (FFmpeg)
├── requirements.txt
├── .env.example            # Environment template
│
├── config/
│   └── settings.py         # All env vars & constants
│
├── core/
│   ├── watcher.py          # Instagram polling loop
│   └── dashboard.py        # Discord dashboard embed updater
│
├── services/
│   ├── instagram.py        # IG login & video download
│   ├── youtube.py          # YouTube upload & scheduling
│   ├── gemini.py           # AI title/description generation
│   └── discord_views.py    # Discord UI buttons & views
│
└── scrapers/
    └── trending_scraper.py  # GitHub Trending scraper bot
```

---

## Components

### 1. Main Bot (`bot.py`)
- Monitors Instagram group chats for new videos (polls every 30 seconds)
- Adds watermarks to videos across 4 channels: Wutering, Film, Motivational, Bola Geming
- Uploads to YouTube with automatic scheduling (09:00 & 21:00 WITA)
- Generates titles & descriptions using Gemini AI
- Real-time dashboard in Discord

> **ID:** Bot utama untuk monitoring Instagram, watermark, dan upload YouTube otomatis dengan jadwal.

### 2. GitHub Trending Scraper (`scrapers/trending_scraper.py`)
- Scrapes GitHub Trending every day at 08:00 WITA
- Covers Daily & Weekly trending repositories
- AI-powered description summaries in plain Indonesian (Gemini)
- Importance rating 1–10 with reasoning
- Output: Color-coded Excel file sent to Discord

> **ID:** Scraper trending GitHub harian dengan ringkasan AI dan rating kepentingan.

---

## Requirements

- Python 3.10+
- FFmpeg — install via `winget install ffmpeg`
- Fonts in `fonts/` folder:
  - `ProximaNova-Regular.ttf`
  - `ProximaNova-Bold.ttf`
- Watermark assets in `watermarks/` folder:
  - `watermark.png` — Wutering channel
  - `Coroco.png` — Film channel
  - `Absolutegrowt.mp4` — Motivational channel
  - `ONGOAL_WM_VIDEO.mp4` — Bola Geming channel

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/komanghary/Coroco-House.git
cd Coroco-House

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy environment template
copy .env.example .env
# Fill in your credentials in .env

# 4. Add YouTube OAuth files
# Place client_secret*.json files from Google Cloud Console in the project root
```

---

## Environment Configuration

Copy `.env.example` to `.env` and fill in your values:

```env
# ── Discord ──────────────────────────────────────────────
DISCORD_TOKEN=           # Discord bot token

# ── Instagram ────────────────────────────────────────────
IG_USERNAME=             # Instagram email/username
IG_PASSWORD=             # Instagram password
POLL_INTERVAL=30         # Polling interval in seconds

# ── Discord Channels ─────────────────────────────────────
WATCHER_CHANNEL_ID=      # Fallback watcher channel
WATERMARK_CHANNEL_ID=    # Manual watermark upload channel
DASHBOARD_CHANNEL_ID=    # Dashboard channel
COPYRIGHT_CHANNEL_ID=    # Copyright alert channel
BOLA_GEMING_CHANNEL_ID=  # Bola Geming channel

# ── Watchlist: ig_group_name=discord_channel_id ──────────
WATCHLIST=Wutering with me=...,Film=...,Motivational=...,Bola Geming=...

# ── Watermark ────────────────────────────────────────────
WATERMARK_USERNAME=@yourhandle

# ── YouTube (one client_secret per channel) ──────────────
YT_CLIENT_SECRET=client_secret.json
YT_FILM_CLIENT_SECRET=client_secret_film.json
YT_MOTIVATIONAL_CLIENT_SECRET=client_secret_motivational.json
YT_BOLA_GEMING_CLIENT_SECRET=client_secret_bola_geming.json

# ── Gemini AI (comma-separated keys for rotation) ────────
GEMINI_API_KEYS=key1,key2,key3,...
```

---

## Running the Bots

### Main Bot
```bash
cd Coroco-House
python bot.py
```

### GitHub Trending Scraper
```bash
cd Coroco-House/scrapers
python trending_scraper.py
```

> Both can run simultaneously as separate processes.
>
> **ID:** Keduanya bisa dijalankan bersamaan sebagai proses terpisah.

---

## How It Works

```
Instagram Group Chat
        ↓  (new video detected)
Download HD + Preview
        ↓
Send to Discord  [Accept] [Cancel]
        ↓  (Accept clicked)
FFmpeg watermark processing
        ↓
Gemini AI generates title & description
        ↓
Preview sent to Discord  [Upload to YouTube] [Cancel]
        ↓  (Upload clicked)
YouTube upload with automatic scheduling
        ↓
Discord dashboard updated
```

### YouTube Upload Schedule
Videos are scheduled twice daily: **09:00** and **21:00 WITA**, alternating per video uploaded.

---

## Notes

- **First YouTube upload per channel** — a browser window will open for OAuth login. This only happens once; the token is saved to `data/youtube_token_*.json`.
- **YouTube quota exceeded** — videos are automatically queued and uploaded when quota resets (~16:00 WITA daily).
- **Cloudflare WARP** — recommended if the server has connectivity issues with Google APIs. Install via `winget install Cloudflare.Warp`.
- **Secrets** — never commit `.env` or `client_secret*.json` files. They are already listed in `.gitignore`.

> **ID:**
> - Login OAuth YouTube hanya sekali per channel, token disimpan otomatis.
> - Jika quota habis, video masuk antrian dan upload otomatis saat reset.
> - Pasang Cloudflare WARP jika server tidak bisa koneksi ke Google API.
> - Jangan commit file `.env` dan `client_secret*.json`.
