"""
1% Perday Book Worker
======================
Standalone process yang memproses antrian buku PDF.
web_monitor dan bot.py menulis job ke data/book_queue.json,
worker ini membacanya dan menjalankan book_processor.

Run: python book_worker.py
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR  = Path(__file__).parent
QUEUE_FILE = BASE_DIR / "data" / "book_queue.json"
WITA = timezone(timedelta(hours=8))


def log(msg: str):
    ts = datetime.now(WITA).strftime("%H:%M:%S")
    print(f"[{ts}] [BookWorker] {msg}", flush=True)


def _read_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_queue(q: list):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(q, indent=2), encoding="utf-8")


async def process_one(job: dict):
    pdf_path = job.get("pdf_path", "")
    if not os.path.exists(pdf_path):
        log(f"PDF tidak ditemukan, skip: {pdf_path}")
        return

    log(f"Mulai proses: {os.path.basename(pdf_path)}")
    try:
        from book_processor import process_book

        async def progress(msg: str):
            log(msg)

        book_id = await process_book(pdf_path, progress_cb=progress)
        log(f"Selesai! book_id={book_id}")
    except Exception as e:
        log(f"Error saat proses buku: {e}")


async def main():
    log("Book Worker dimulai — menunggu antrian di data/book_queue.json")
    log(f"Queue file: {QUEUE_FILE}")

    while True:
        queue = _read_queue()
        if queue:
            job = queue.pop(0)
            _write_queue(queue)
            await process_one(job)
        else:
            await asyncio.sleep(5)  # poll setiap 5 detik


if __name__ == "__main__":
    print("=" * 55)
    print("  1% Perday Book Worker")
    print(f"  Queue: {QUEUE_FILE}")
    print("  Tekan Ctrl+C untuk berhenti")
    print("=" * 55)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Book Worker dihentikan.")
