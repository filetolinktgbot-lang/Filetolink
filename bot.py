import os
import logging
import asyncio
import hashlib
import time
import sqlite3
from datetime import datetime

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse, Response
import uvicorn

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BASE_URL  = os.environ["BASE_URL"].rstrip("/")
PORT      = int(os.environ.get("PORT", 8000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ── SQLite (persistent across restarts) ───────────────────────────────────────
DB_PATH = "/app/files.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            token       TEXT PRIMARY KEY,
            chat_id     INTEGER,
            message_id  INTEGER,
            file_name   TEXT,
            mime_type   TEXT,
            file_size   INTEGER,
            added_at    TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_file(token, chat_id, message_id, file_name, mime_type, file_size):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
        (token, chat_id, message_id, file_name, mime_type,
         file_size, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_file(token):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT token,chat_id,message_id,file_name,mime_type,file_size,added_at "
        "FROM files WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    if row:
        return {
            "token":      row[0],
            "chat_id":    row[1],
            "message_id": row[2],
            "file_name":  row[3],
            "mime_type":  row[4],
            "file_size":  row[5],
            "added_at":   row[6],
        }
    return None

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_token(chat_id: int, message_id: int) -> str:
    return hashlib.sha256(f"{chat_id}:{message_id}".encode()).hexdigest()[:16]

def human_size(n) -> str:
    if not n:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def is_streamable(mime_type: str) -> bool:
    return mime_type.startswith(("video/", "audio/", "image/"))

# ── Telethon client ────────────────────────────────────────────────────────────
client = TelegramClient(StringSession(), API_ID, API_HASH)
app    = FastAPI(title="TG FileLink Bot")

# ── Bot handlers ───────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern="/start|/help"))
async def cmd_start(event):
    await event.reply(
        "📁 <b>File → Link Bot</b>\n\n"
        "Send me any file and I'll give you a <b>direct download link</b>.\n\n"
        "✅ Supports files up to <b>2 GB</b>\n"
        "▶️ Streaming links for videos and audio!\n\n"
        "Supported: Documents, Videos, Audio, Photos, Voice, Stickers",
        parse_mode="html",
    )

@client.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    await event.reply(
        f"🤖 <b>Bot Status</b>\n\n"
        f"📦 Files stored: {count}\n"
        f"🌐 Base URL: {BASE_URL}",
        parse_mode="html",
    )

@client.on(events.NewMessage)
async def handle_file(event):
    msg = event.message
    if not msg.media or (msg.text and msg.text.startswith("/")):
        return

    file_name = None
    mime_type = "application/octet-stream"
    file_size = 0

    if msg.document:
        doc = msg.document
        mime_type = doc.mime_type or "application/octet-stream"
        file_size = doc.size or 0
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                file_name = attr.file_name
                break
        if not file_name:
            ext = mime_type.split("/")[-1].split(";")[0]
            file_name = f"file_{int(time.time())}.{ext}"
    elif msg.photo:
        file_name = f"photo_{int(time.time())}.jpg"
        mime_type = "image/jpeg"
        file_size = 0
    elif msg.voice:
        file_name = f"voice_{int(time.time())}.ogg"
        mime_type = "audio/ogg"
        file_size = msg.voice.size or 0
    elif msg.video_note:
        file_name = f"videonote_{int(time.time())}.mp4"
        mime_type = "video/mp4"
        file_size = msg.video_note.size or 0
    elif msg.sticker:
        file_name = "sticker.webp"
        mime_type = "image/webp"
        file_size = msg.sticker.size or 0
    else:
        return

    token = make_token(msg.chat_id, msg.id)
    save_file(token, msg.chat_id, msg.id, file_name, mime_type, file_size)

    dl_link     = f"{BASE_URL}/dl/{token}"
    stream_link = f"{BASE_URL}/stream/{token}"
    pg_link     = f"{BASE_URL}/file/{token}"

    log.info("Stored token=%s  name=%s  size=%s", token, file_name, human_size(file_size))

    # Build message
    streamable = is_streamable(mime_type)
    stream_line = (
        f"\n▶️ <b>Stream link:</b>\n<a href='{stream_link}'>{stream_link}</a>\n"
        if streamable else ""
    )

    await event.reply(
        f"✅ <b>File received!</b>\n\n"
        f"📄 <b>Name:</b> <code>{file_name}</code>\n"
        f"💾 <b>Size:</b> {human_size(file_size)}\n"
        f"🔠 <b>Type:</b> {mime_type}\n\n"
        f"🔗 <b>Download link:</b>\n<a href='{dl_link}'>{dl_link}</a>\n"
        f"{stream_line}"
        f"\n🌐 <b>Info page:</b>\n<a href='{pg_link}'>{pg_link}</a>",
        parse_mode="html",
        link_preview=False,
    )

# ── FastAPI routes ─────────────────────────────────────────────────────────────

async def get_media_stream(entry: dict, start: int = 0, end: int = None):
    """Stream media from Telegram with optional byte range."""
    message = await client.get_messages(entry["chat_id"], ids=entry["message_id"])
    if not message or not message.media:
        raise HTTPException(status_code=404, detail="File no longer available.")

    async for chunk in client.iter_download(message.media, offset=start, limit=end):
        yield chunk


@app.get("/dl/{token}")
async def download(token: str):
    entry = get_file(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Link not found. Please re-send the file to the bot.")

    async def stream():
        try:
            async for chunk in get_media_stream(entry):
                yield chunk
        except Exception as e:
            log.error("Stream error: %s", e)

    headers = {
        "Content-Disposition": f'attachment; filename="{entry["file_name"]}"',
        "Content-Type":        entry["mime_type"],
    }
    if entry["file_size"]:
        headers["Content-Length"] = str(entry["file_size"])

    return StreamingResponse(stream(), headers=headers, media_type=entry["mime_type"])


@app.get("/stream/{token}")
async def stream_media(token: str, request: Request):
    """Streaming endpoint with range request support for video/audio players."""
    entry = get_file(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Link not found.")

    file_size = entry["file_size"] or 0
    mime_type = entry["mime_type"]

    # Parse Range header
    range_header = request.headers.get("Range")
    start = 0
    end   = file_size - 1 if file_size else None

    if range_header and file_size:
        try:
            range_val = range_header.replace("bytes=", "")
            parts     = range_val.split("-")
            start     = int(parts[0]) if parts[0] else 0
            end       = int(parts[1]) if parts[1] else file_size - 1
        except Exception:
            start = 0
            end   = file_size - 1

    chunk_size   = (end - start + 1) if (end is not None and file_size) else None
    status_code  = 206 if range_header and file_size else 200

    async def stream():
        try:
            message = await client.get_messages(entry["chat_id"], ids=entry["message_id"])
            if not message or not message.media:
                return
            async for chunk in client.iter_download(
                message.media,
                offset=start,
                limit=chunk_size,
            ):
                yield chunk
        except Exception as e:
            log.error("Stream error: %s", e)

    headers = {
        "Content-Type":  mime_type,
        "Accept-Ranges": "bytes",
    }
    if file_size:
        headers["Content-Length"] = str(chunk_size or file_size)
    if range_header and file_size:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        stream(),
        status_code=status_code,
        headers=headers,
        media_type=mime_type,
    )


@app.get("/file/{token}", response_class=HTMLResponse)
async def file_page(token: str):
    entry = get_file(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Not found.")

    dl          = f"{BASE_URL}/dl/{token}"
    stream_url  = f"{BASE_URL}/stream/{token}"
    sz          = human_size(entry["file_size"])
    streamable  = is_streamable(entry["mime_type"])
    is_video    = entry["mime_type"].startswith("video/")
    is_audio    = entry["mime_type"].startswith("audio/")

    player_html = ""
    if is_video:
        player_html = f"""
  <video controls style="width:100%;border-radius:10px;margin-bottom:1rem;" preload="metadata">
    <source src="{stream_url}" type="{entry['mime_type']}">
  </video>"""
    elif is_audio:
        player_html = f"""
  <audio controls style="width:100%;margin-bottom:1rem;">
    <source src="{stream_url}" type="{entry['mime_type']}">
  </audio>"""

    stream_btn = (
        f'<a class="btn stream" href="{stream_url}">▶ Stream</a>'
        if streamable else ""
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
    max-width: 520px; width: 100%;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
  }}
  .icon {{ font-size: 3.2rem; text-align: center; margin-bottom: 1rem }}
  h1 {{
    font-size: 1.1rem; font-weight: 700;
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
  .buttons {{ display: flex; gap: .75rem; margin-top: .5rem }}
  a.btn {{
    display: block; text-align: center; flex: 1;
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    color: #fff; padding: 1rem; border-radius: 10px;
    font-weight: 700; font-size: 1rem; text-decoration: none;
    transition: opacity .2s, transform .2s;
  }}
  a.btn.stream {{
    background: linear-gradient(135deg, #10b981, #059669);
  }}
  a.btn:hover {{ opacity: .85; transform: translateY(-1px) }}
  .foot {{ margin-top: 1.2rem; text-align: center; font-size: .75rem; color: #475569 }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">📦</div>
  <h1>{entry['file_name']}</h1>
  {player_html}
  <div class="meta">
    <div class="row"><span class="label">Size</span><span class="value">{sz}</span></div>
    <div class="row"><span class="label">Type</span><span class="value">{entry['mime_type']}</span></div>
    <div class="row"><span class="label">Added (UTC)</span><span class="value">{entry['added_at']}</span></div>
  </div>
  <div class="buttons">
    <a class="btn" href="{dl}">⬇ Download</a>
    {stream_btn}
  </div>
  <p class="foot">TG FileLink Bot · Up to 2 GB supported</p>
</div>
</body>
</html>""")


@app.get("/")
async def root():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    return {
        "service":      "TG FileLink Bot",
        "status":       "running",
        "files_stored": count,
        "max_file_size": "2 GB",
    }


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    await client.start(bot_token=BOT_TOKEN)
    log.info("Bot started with Telethon (2 GB + streaming support)")

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
    
