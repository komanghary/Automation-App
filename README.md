# Coroco House Bot

Bot Discord otomatis untuk mengelola konten Instagram → watermark → YouTube upload, dilengkapi dengan scraper GitHub Trending.

---

## Daftar Isi

- [Struktur Folder](#struktur-folder)
- [Komponen](#komponen)
- [Persyaratan](#persyaratan)
- [Instalasi](#instalasi)
- [Konfigurasi `.env`](#konfigurasi-env)
- [Menjalankan Bot](#menjalankan-bot)
- [Cara Kerja](#cara-kerja)
- [Perintah Discord](#perintah-discord)

---

## Struktur Folder

```
Coroco-House/
├── bot.py                  # Entry point bot utama
├── processor.py            # Watermark dengan teks (PIL)
├── processor_overlay.py    # Watermark overlay (FFmpeg)
├── requirements.txt
├── .env.example            # Template konfigurasi
│
├── config/
│   └── settings.py         # Semua env vars & konstanta
│
├── core/
│   ├── watcher.py          # Instagram polling loop
│   └── dashboard.py        # Dashboard embed Discord
│
├── services/
│   ├── instagram.py        # Login & download video IG
│   ├── youtube.py          # Upload & jadwal YouTube
│   ├── gemini.py           # Generate judul/deskripsi AI
│   └── discord_views.py    # UI tombol Discord
│
└── scrapers/
    └── trending_scraper.py  # GitHub Trending scraper
```

---

## Komponen

### 1. Bot Utama (`bot.py`)
- Menonton grup Instagram untuk video baru (polling setiap 30 detik)
- Menambahkan watermark ke video (4 channel: Wutering, Film, Motivational, Bola Geming)
- Upload ke YouTube dengan jadwal otomatis (09:00 & 21:00 WITA)
- Generate judul & deskripsi menggunakan Gemini AI
- Dashboard real-time di Discord

### 2. GitHub Trending Scraper (`scrapers/trending_scraper.py`)
- Scraping GitHub Trending setiap jam 08:00 WITA
- Daily & Weekly trending repos
- Ringkasan deskripsi AI (Gemini) dalam bahasa Indonesia
- Rating kepentingan 1–10 + alasan "kenapa harus tahu"
- Output: Excel dengan warna rating

---

## Persyaratan

- Python 3.10+
- FFmpeg (install via `winget install ffmpeg`)
- Font: `fonts/ProximaNova-Regular.ttf` dan `fonts/ProximaNova-Bold.ttf`
- Watermark assets di folder `watermarks/`:
  - `watermark.png` (Wutering)
  - `Coroco.png` (Film)
  - `Absolutegrowt.mp4` (Motivational)
  - `ONGOAL_WM_VIDEO.mp4` (Bola Geming)

---

## Instalasi

```bash
# 1. Clone repo
git clone https://github.com/komanghary/Coroco-House.git
cd Coroco-House

# 2. Install dependencies
pip install -r requirements.txt

# 3. Salin template env
copy .env.example .env
# Edit .env dengan kredensial kamu

# 4. Siapkan YouTube OAuth
# Taruh file client_secret*.json dari Google Cloud Console
```

---

## Konfigurasi `.env`

Salin `.env.example` menjadi `.env` lalu isi:

```env
# Discord
DISCORD_TOKEN=           # Token bot Discord

# Instagram
IG_USERNAME=             # Email/username Instagram
IG_PASSWORD=             # Password Instagram
POLL_INTERVAL=30         # Interval polling (detik)

# Channel Discord
WATCHER_CHANNEL_ID=      # Channel fallback watcher
WATERMARK_CHANNEL_ID=    # Channel upload manual watermark
DASHBOARD_CHANNEL_ID=    # Channel dashboard
COPYRIGHT_CHANNEL_ID=    # Channel notifikasi copyright
BOLA_GEMING_CHANNEL_ID=  # Channel Bola Geming

# Watchlist: nama_grup_ig=channel_discord_id
WATCHLIST=Wutering with me=...,Film=...,Motivational=...,Bola Geming=...

# Watermark
WATERMARK_USERNAME=@handle

# YouTube (per channel)
YT_CLIENT_SECRET=client_secret.json
YT_FILM_CLIENT_SECRET=client_secret_film.json
YT_MOTIVATIONAL_CLIENT_SECRET=client_secret_motivational.json
YT_BOLA_GEMING_CLIENT_SECRET=client_secret_bola_geming.json

# Gemini AI (pisah koma untuk rotasi)
GEMINI_API_KEYS=key1,key2,key3,...
```

---

## Menjalankan Bot

### Bot Utama
```bash
cd Coroco-House
python bot.py
```

### GitHub Trending Scraper
```bash
cd Coroco-House/scrapers
python trending_scraper.py
```

> Keduanya bisa dijalankan bersamaan secara terpisah.

---

## Cara Kerja

```
Instagram Grup
      ↓ (video baru terdeteksi)
Download HD + Preview
      ↓
Kirim ke Discord (dengan tombol Accept/Cancel)
      ↓ (klik Accept)
FFmpeg watermark processing
      ↓
Gemini AI generate judul & deskripsi
      ↓
Tampil preview + tombol Upload ke YouTube
      ↓ (klik Upload)
Upload ke YouTube dengan jadwal otomatis
      ↓
Dashboard Discord terupdate
```

### Jadwal Upload YouTube
Upload dijadwalkan dua kali sehari: **09:00** dan **21:00 WITA**, bergantian per video.

---

## Perintah Discord

Upload video manual (kirim video ke `WATERMARK_CHANNEL_ID`):
- Sertakan judul video di pesan
- Bot otomatis proses watermark dan tanya konfirmasi upload YouTube

---

## Catatan

- Pertama kali upload YouTube per channel → browser akan meminta OAuth login
- Token tersimpan di `data/youtube_token_*.json` (tidak perlu login ulang)
- Jika quota YouTube habis → video masuk antrian otomatis, upload saat quota reset (~16:00 WITA)
