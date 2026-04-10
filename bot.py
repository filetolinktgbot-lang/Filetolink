import os
import logging
import asyncio
import hashlib
import time
from datetime import datetime

import telebot
from telebot import types
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
import httpx
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
BASE_URL      = os.environ["BASE_URL"].rstrip("/")
PORT          = int(os.environ.get("PORT", 8000))
WEBHOOK_URL   = f"{BASE_URL}/webhook/{BOT_TOKEN}"

# Local Bot API Server URL (set this in Railway variables)
# Example: https://your-local-api.up.railway.app
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "").rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ── Point to Local Bot API if configured ─────────────────────────────────────
if LOCAL_API_URL:
    log.info("Using Local Bot API Server: %s", LOCAL_API_URL)
    telebot.apihelper.API_URL  = LOCAL_API_URL + "/bot{0}/{1}"
    telebot.apihelper.FILE_URL = LOCAL_API_URL + "/file/bot{0}/{1}"
else:
    log.warning("LOCAL_API_URL not set — files over 20 MB will fail!")
    telebot.apihelper.API_URL  = "https://api.telegram.org/bot{0}/{1}"
    telebot.apihelper.FILE_URL = "https://api.telegram.org/file/bot{0}/{1}"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = FastAPI(title="TG FileLink Bot")

# In-memory store: token → file metadata
FILE_STORE: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_token(file_id: str) -> str:
    return hashlib.sha256(file_id.encode()).hexdigest()[:16]


def human_size(n) -> str:
    if not n:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def register_file(file_id, file_name, mime, size) -> str:
    token = make_token(file_id)
    FILE_STORE[token] = {
        "file_id":   file_id,
        "file_name": file_name or "file",
        "mime_type": mime or "application/octet-stream",
        "file_size": size or 0,
        "added_at":  datetime.utcnow().isoformat(),
    }
    log.info("Stored token=%s  name=%s  size=%s", token, file_name, human_size(size))
    return token


def get_tg_url(file_id: str) -> str:
    """Get streamable URL from Telegram (or local server)."""
    tg_file = bot.get_file(file_id)
    if LOCAL_API_URL:
        return f"{LOCAL_API_URL}/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"


def extract_file_info(message):
    m = message
    if m.document:
        d = m.document
        return d.file_id, d.file_name or "file", d.mime_type, d.file_size
    if m.video:
        v = m.video
        return v.file_id, f"video_{v.file_id[:6]}.mp4", v.mime_type or "video/mp4", v.file_size
    if m.audio:
        a = m.audio
        return a.file_id, a.file_name or "audio.mp3", a.mime_type or "audio/mpeg", a.file_size
    if m.voice:
        v = m.voice
        return v.file_id, f"voice_{int(time.time())}.ogg", "audio/ogg", v.file_size
    if m.video_note:
        vn = m.video_note
        return vn.file_id, f"vnote_{int(time.time())}.mp4", "video/mp4", vn.file_size
    if m.photo:
        p = m.photo[-1]
        return p.file_id, f"photo_{int(time.time())}.jpg", "image/jpeg", p.file_size
    if m.sticker:
        s = m.sticker
        return s.file_id, "sticker.webp", "image/webp", s.file_size
    return None


# ── Bot handlers ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    mode = "✅ Local Bot API (supports up to 4 GB)" if LOCAL_API_URL else "⚠️ Public API (max 20 MB)"
    bot.send_message(
        msg.chat.id,
        f"📁 <b>File → Link Bot</b>\n\n"
        f"Send me any file and get a <b>direct download link</b>.\n\n"
        f"🖥 <b>Mode:</b> {mode}\n\n"
        f"Supported: Documents, Videos, Audio, Photos, Voice, Stickers",
    )


@bot.message_handler(commands=["status"])
def cmd_status(msg):
    mode = f"Local API: {LOCAL_API_URL}" if LOCAL_API_URL else "Public API (20 MB limit)"
    bot.send_message(
        msg.chat.id,
        f"🤖 <b>Bot Status</b>\n\n"
        f"📡 Mode: {mode}\n"
        f"📦 Files cached: {len(FILE_STORE)}\n"
        f"🌐 Base URL: {BASE_URL}",
    )


@bot.message_handler(
    content_types=["document", "video", "audio", "voice", "video_note", "photo", "sticker"]
)
def handle_file(message):
    info = extract_file_info(message)
    if not info:
        bot.reply_to(message, "❌ Could not read that file.")
        return

    file_id, file_name, mime, size = info

    # Warn if no local server and file is large
    if not LOCAL_API_URL and size and size > 20 * 1024 * 1024:
        bot.reply_to(
            message,
            f"⚠️ <b>File is {human_size(size)}</b> — exceeds the 20 MB public API limit.\n\n"
            "Set <code>LOCAL_API_URL</code> in Railway variables to support large files.\n"
            "Link generated but download will fail.",
        )

    token   = register_file(file_id, file_name, mime, size)
    dl_link = f"{BASE_URL}/dl/{token}"
    pg_link = f"{BASE_URL}/file/{token}"

    bot.reply_to(
        message,
        f"✅ <b>File received!</b>\n\n"
        f"📄 <b>Name:</b> <code>{file_name}</code>\n"
        f"💾 <b>Size:</b> {human_size(size)}\n"
        f"🔠 <b>Type:</b> {mime}\n\n"
        f"🔗 <b>Download link:</b>\n<code>{dl_link}</code>\n\n"
        f"🌐 <b>Info page:</b>\n<code>{pg_link}</code>",
        disable_web_page_preview=True,
    )


# ── FastAPI routes ─────────────────────────────────────────────────────────────

@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(update: dict):
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return {"ok": True}


@app.get("/dl/{token}")
async def download(token: str):
    entry = FILE_STORE.get(token)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail="Link not found. The bot may have restarted — please re-send the file to the bot.",
        )

    try:
        tg_url = get_tg_url(entry["file_id"])
        log.info("Streaming token=%s from %s", token, tg_url[:60])
    except Exception as e:
        err = str(e).lower()
        log.error("get_file failed: %s", e)
        if "file is too big" in err or "bad request" in err:
            raise HTTPException(
                status_code=413,
                detail=(
                    "File exceeds Telegram public API limit (20 MB). "
                    "Add LOCAL_API_URL environment variable in Railway to enable 4 GB support."
                ),
            )
        raise HTTPException(status_code=502, detail=f"Telegram error: {e}")

    async def stream():
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

    return StreamingResponse(stream(), headers=headers, media_type=entry["mime_type"])


@app.get("/file/{token}", response_class=HTMLResponse)
async def file_page(token: str):
    entry = FILE_STORE.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Not found.")

    dl      = f"{BASE_URL}/dl/{token}"
    sz      = human_size(entry["file_size"])
    is_big  = (not LOCAL_API_URL) and entry["file_size"] > 20 * 1024 * 1024
    warn    = (
        '<div class="warn">⚠️ File over 20 MB — download requires Local Bot API Server</div>'
        if is_big else ""
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{entry['file_name']} – FileLink Bot</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0 }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0f1117; color: #e2e8f0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 1rem;
  }}
  .card {{
    background: #1e2230; border: 1px solid #2d3348;
    border-radius: 16px; padding: 2.5rem;
    max-width: 480px; width: 100%;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
  }}
  .icon {{ font-size: 3.2rem; text-align: center; margin-bottom: 1rem }}
  h1 {{
    font-size: 1.15rem; font-weight: 700;
    word-break: break-all; text-align: center; margin-bottom: 1.5rem;
    color: #f1f5f9;
  }}
  .meta {{ display: grid; gap: .5rem; margin-bottom: 1.5rem }}
  .row {{
    display: flex; justify-content: space-between; font-size: .87rem;
    padding: .5rem .8rem; background: #151823; border-radius: 8px;
  }}
  .label {{ color: #94a3b8 }}
  .value {{ font-weight: 600; word-break: break-all; text-align: right; max-width: 65% }}
  .warn {{
    background: #451a03; border: 1px solid #92400e; color: #fcd34d;
    padding: .75rem 1rem; border-radius: 8px;
    font-size: .83rem; margin-bottom: 1rem; text-align: center;
  }}
  a.btn {{
    display: block; text-align: center;
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    color: #fff; padding: 1rem; border-radius: 10px;
    font-weight: 700; font-size: 1rem; text-decoration: none;
    transition: opacity .2s, transform .2s;
  }}
  a.btn:hover {{ opacity: .85; transform: translateY(-1px) }}
  .foot {{ margin-top: 1.2rem; text-align: center; font-size: .75rem; color: #475569 }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">📦</div>
  <h1>{entry['file_name']}</h1>
  <div class="meta">
    <div class="row">
      <span class="label">Size</span>
      <span class="value">{sz}</span>
    </div>
    <div class="row">
      <span class="label">Type</span>
      <span class="value">{entry['mime_type']}</span>
    </div>
    <div class="row">
      <span class="label">Added (UTC)</span>
      <span class="value">{entry['added_at']}</span>
    </div>
  </div>
  {warn}
  <a class="btn" href="{dl}">⬇ Download File</a>
  <p class="foot">TG FileLink Bot · Powered by Railway</p>
</div>
</body>
</html>""")


@app.get("/")
async def root():
    return {
        "service":      "TG FileLink Bot",
        "status":       "running",
        "files_cached": len(FILE_STORE),
        "mode":         "local_api" if LOCAL_API_URL else "public_api",
    }


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await asyncio.sleep(1)
    try:
        bot.remove_webhook()
        ok = bot.set_webhook(url=WEBHOOK_URL)
        log.info("Webhook set=%s → %s", ok, WEBHOOK_URL)
    except Exception as e:
        log.error("Webhook setup failed: %s", e)


if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, log_level="info")
