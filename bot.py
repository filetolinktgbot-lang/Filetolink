import os
import logging
import asyncio
import hashlib
import time
from pathlib import Path
from datetime import datetime

import telebot
from telebot import types
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
import httpx
import uvicorn
from threading import Thread

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
BASE_URL    = os.environ["BASE_URL"].rstrip("/")   # e.g. https://yourapp.up.railway.app
PORT        = int(os.environ.get("PORT", 8000))
WEBHOOK_URL = f"{BASE_URL}/webhook/{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = FastAPI(title="TG FileLink Bot")

# In-memory store  {token: {file_id, file_name, mime_type, file_size, added_at}}
FILE_STORE: dict[str, dict] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_token(file_id: str) -> str:
    """Deterministic short token from file_id."""
    return hashlib.sha256(file_id.encode()).hexdigest()[:16]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def register_file(file_id: str, file_name: str, mime: str, size: int) -> str:
    token = make_token(file_id)
    FILE_STORE[token] = {
        "file_id":   file_id,
        "file_name": file_name,
        "mime_type": mime or "application/octet-stream",
        "file_size": size,
        "added_at":  datetime.utcnow().isoformat(),
    }
    return token


# ── Bot handlers ─────────────────────────────────────────────────────────────

def extract_file_info(message: types.Message):
    """Return (file_id, file_name, mime, size) or None."""
    m = message

    if m.document:
        d = m.document
        return d.file_id, d.file_name or "file", d.mime_type, d.file_size

    if m.video:
        v = m.video
        name = f"video_{v.file_id[:8]}.mp4"
        return v.file_id, name, v.mime_type or "video/mp4", v.file_size

    if m.audio:
        a = m.audio
        name = a.file_name or f"audio_{a.file_id[:8]}.mp3"
        return a.file_id, name, a.mime_type or "audio/mpeg", a.file_size

    if m.voice:
        v = m.voice
        return v.file_id, f"voice_{int(time.time())}.ogg", "audio/ogg", v.file_size

    if m.video_note:
        vn = m.video_note
        return vn.file_id, f"videonote_{int(time.time())}.mp4", "video/mp4", vn.file_size

    if m.photo:
        p = m.photo[-1]          # largest
        return p.file_id, f"photo_{int(time.time())}.jpg", "image/jpeg", p.file_size

    if m.sticker:
        s = m.sticker
        ext = ".webm" if s.is_video else ".webp"
        return s.file_id, f"sticker{ext}", "image/webp", s.file_size

    return None


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    bot.send_message(
        message.chat.id,
        "📁 <b>File → Link Bot</b>\n\n"
        "Send me <b>any file</b> (document, video, audio, photo…) "
        "and I'll give you a <b>direct download link</b>.\n\n"
        "⚡ Telegram supports files up to <b>4 GB</b>.\n"
        "🔗 Links are permanent as long as the bot is running.\n\n"
        "Just drop a file here to get started!",
    )


@bot.message_handler(
    content_types=[
        "document", "video", "audio", "voice",
        "video_note", "photo", "sticker",
    ]
)
def handle_file(message: types.Message):
    info = extract_file_info(message)
    if not info:
        bot.reply_to(message, "❌ Could not read that file. Try again.")
        return

    file_id, file_name, mime, size = info
    token   = register_file(file_id, file_name, mime, size)
    dl_link = f"{BASE_URL}/dl/{token}"
    pg_link = f"{BASE_URL}/file/{token}"

    text = (
        f"✅ <b>File received!</b>\n\n"
        f"📄 <b>Name:</b> <code>{file_name}</code>\n"
        f"💾 <b>Size:</b> {human_size(size)}\n"
        f"🔠 <b>Type:</b> {mime}\n\n"
        f"🔗 <b>Direct download:</b>\n{dl_link}\n\n"
        f"🌐 <b>Info page:</b>\n{pg_link}"
    )
    bot.reply_to(message, text, disable_web_page_preview=True)
    log.info("Registered  token=%s  name=%s  size=%s", token, file_name, human_size(size))


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(update: dict):
    """Receive Telegram updates via webhook."""
    update_obj = telebot.types.Update.de_json(update)
    bot.process_new_updates([update_obj])
    return {"ok": True}


@app.get("/dl/{token}")
async def download_file(token: str):
    """Stream the file directly to the browser / downloader."""
    entry = FILE_STORE.get(token)
    if not entry:
        raise HTTPException(404, "Link not found or expired.")

    # Ask Telegram for a fresh temporary URL
    try:
        tg_file = bot.get_file(entry["file_id"])
        tg_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    except Exception as exc:
        log.error("get_file failed: %s", exc)
        raise HTTPException(502, "Could not fetch file from Telegram.")

    async def streamer():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", tg_url) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes(65536):
                    yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{entry["file_name"]}"',
        "Content-Type":        entry["mime_type"],
    }
    if entry["file_size"]:
        headers["Content-Length"] = str(entry["file_size"])

    return StreamingResponse(streamer(), headers=headers, media_type=entry["mime_type"])


@app.get("/file/{token}", response_class=HTMLResponse)
async def file_page(token: str):
    """Nice HTML info page with download button."""
    entry = FILE_STORE.get(token)
    if not entry:
        raise HTTPException(404, "Link not found.")

    size_str = human_size(entry["file_size"]) if entry["file_size"] else "Unknown"
    dl_link  = f"{BASE_URL}/dl/{token}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{entry['file_name']} – FileLink Bot</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;
        display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}}
  .card{{background:#1e2230;border:1px solid #2d3348;border-radius:16px;
         padding:2.5rem;max-width:480px;width:100%;box-shadow:0 20px 60px #0008}}
  .icon{{font-size:3.5rem;text-align:center;margin-bottom:1.2rem}}
  h1{{font-size:1.35rem;font-weight:700;word-break:break-all;margin-bottom:1.5rem;
      text-align:center;color:#f1f5f9}}
  .meta{{display:grid;gap:.6rem;margin-bottom:2rem}}
  .row{{display:flex;justify-content:space-between;font-size:.9rem;
        padding:.55rem .8rem;background:#151823;border-radius:8px}}
  .label{{color:#94a3b8}}
  .value{{font-weight:600;color:#e2e8f0;word-break:break-all;text-align:right;max-width:60%}}
  a.btn{{display:block;text-align:center;background:linear-gradient(135deg,#3b82f6,#6366f1);
         color:#fff;padding:1rem;border-radius:10px;font-weight:700;font-size:1rem;
         text-decoration:none;transition:.2s}}
  a.btn:hover{{opacity:.88;transform:translateY(-1px)}}
  .foot{{margin-top:1.5rem;text-align:center;font-size:.78rem;color:#475569}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">📦</div>
  <h1>{entry['file_name']}</h1>
  <div class="meta">
    <div class="row"><span class="label">Size</span><span class="value">{size_str}</span></div>
    <div class="row"><span class="label">Type</span><span class="value">{entry['mime_type']}</span></div>
    <div class="row"><span class="label">Added</span><span class="value">{entry['added_at']} UTC</span></div>
  </div>
  <a class="btn" href="{dl_link}">⬇ Download File</a>
  <p class="foot">Powered by TG FileLink Bot · Railway</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/")
async def root():
    return {
        "service": "Telegram FileLink Bot",
        "files":   len(FILE_STORE),
        "status":  "running",
    }


# ── Startup / Webhook registration ───────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    await asyncio.sleep(1)          # give uvicorn a moment
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    log.info("Webhook set → %s", WEBHOOK_URL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, log_level="info")
