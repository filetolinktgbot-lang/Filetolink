import os
import logging
import asyncio
import hashlib
import time
from datetime import datetime

import telebot
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
import httpx
import uvicorn

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", 8000))
WEBHOOK_URL = f"{BASE_URL}/webhook/{BOT_TOKEN}"

# Local API (VERY IMPORTANT for 4GB)
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "").rstrip("/")

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ================= TELEGRAM API =================
if LOCAL_API_URL:
    log.info(f"Using Local API: {LOCAL_API_URL}")
    telebot.apihelper.API_URL = LOCAL_API_URL + "/bot{0}/{1}"
    telebot.apihelper.FILE_URL = LOCAL_API_URL + "/file/bot{0}/{1}"
else:
    log.warning("LOCAL_API_URL not set → limit = 20MB")
    telebot.apihelper.API_URL = "https://api.telegram.org/bot{0}/{1}"
    telebot.apihelper.FILE_URL = "https://api.telegram.org/file/bot{0}/{1}"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = FastAPI()

# ================= MEMORY STORE =================
FILE_STORE = {}

# ================= HELPERS =================
def make_token(file_id):
    return hashlib.sha256(file_id.encode()).hexdigest()[:16]


def human_size(size):
    if not size:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def register_file(file_id, name, mime, size):
    token = make_token(file_id)
    FILE_STORE[token] = {
        "file_id": file_id,
        "file_name": name or "file",
        "mime_type": mime or "application/octet-stream",
        "file_size": size or 0,
        "time": datetime.utcnow().isoformat()
    }
    return token


def get_file_url(file_id):
    tg_file = bot.get_file(file_id)
    if LOCAL_API_URL:
        return f"{LOCAL_API_URL}/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"


def extract(message):
    if message.document:
        d = message.document
        return d.file_id, d.file_name, d.mime_type, d.file_size
    if message.video:
        v = message.video
        return v.file_id, "video.mp4", "video/mp4", v.file_size
    if message.audio:
        a = message.audio
        return a.file_id, "audio.mp3", "audio/mpeg", a.file_size
    if message.photo:
        p = message.photo[-1]
        return p.file_id, "photo.jpg", "image/jpeg", p.file_size
    return None

# ================= BOT =================
@bot.message_handler(commands=["start"])
def start(msg):
    mode = "4GB Mode ✅" if LOCAL_API_URL else "20MB Mode ⚠️"
    bot.send_message(msg.chat.id, f"📁 Send file\nMode: {mode}")


@bot.message_handler(content_types=["document", "video", "audio", "photo"])
def handle(msg):
    data = extract(msg)
    if not data:
        return bot.reply_to(msg, "❌ Unsupported file")

    file_id, name, mime, size = data

    token = register_file(file_id, name, mime, size)

    dl = f"{BASE_URL}/dl/{token}"
    pg = f"{BASE_URL}/file/{token}"

    bot.reply_to(msg,
        f"✅ File received\n\n"
        f"📄 {name}\n"
        f"💾 {human_size(size)}\n\n"
        f"🔗 {dl}\n"
        f"🌐 {pg}"
    )

# ================= WEB =================
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(update: dict):
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return {"ok": True}


@app.get("/dl/{token}")
async def download(token: str):
    file = FILE_STORE.get(token)
    if not file:
        raise HTTPException(404, "File not found")

    url = get_file_url(file["file_id"])

    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream(),
        headers={"Content-Disposition": f'attachment; filename="{file["file_name"]}"'}
    )


@app.get("/file/{token}")
async def page(token: str):
    file = FILE_STORE.get(token)
    if not file:
        return HTMLResponse("Not found")

    return HTMLResponse(f"""
    <h2>{file['file_name']}</h2>
    <p>Size: {human_size(file['file_size'])}</p>
    <a href="/dl/{token}">Download</a>
    """)


@app.get("/")
def home():
    return {"status": "running"}

# ================= START =================
@app.on_event("startup")
async def start_webhook():
    await asyncio.sleep(1)
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
