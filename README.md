📦 Telegram File → Link Bot
Send any file (up to 4 GB) to the bot and receive an instant direct-download link hosted on Railway.
Features
Feature
Detail
Supports
Documents, Videos, Audio, Photos, Voice, Stickers
Max file size
4 GB (Telegram Premium bots limit)
Download
Streamed directly – no disk storage needed
Info page
Pretty HTML page per file at /file/<token>
Hosting
Railway (free tier works for light traffic)
Quick Deploy to Railway
1 – Create a Telegram bot
Open @BotFather → /newbot
Copy the BOT_TOKEN
2 – Push to GitHub
git init
git add .
git commit -m "init"
gh repo create tg-filebot --public --push
3 – Deploy on Railway
Go to railway.app → New Project → Deploy from GitHub
Select your repo
In Settings → Variables add:
Variable
Value
BOT_TOKEN
123456:ABC... (from BotFather)
BASE_URL
https://your-app.up.railway.app (Railway gives you this under Settings → Domains)
Railway auto-detects Procfile and starts the bot.
The bot registers its webhook automatically on first boot.
Local Development
pip install -r requirements.txt

export BOT_TOKEN="your_token"
export BASE_URL="https://your-ngrok-url"   # use ngrok for local HTTPS

python bot.py
For local testing set BASE_URL to an ngrok HTTPS tunnel:
ngrok http 8000
How It Works
User sends file (≤4 GB)
        │
        ▼
   Telegram servers store it
        │
        ▼
  Bot receives file_id + metadata
  → generates a short token
  → stores token → file_id mapping (in-memory)
  → replies with:
       • Direct download URL  /dl/<token>
       • Info page URL        /file/<token>
        │
        ▼
  Visitor opens /dl/<token>
  → bot calls getFile API (fresh temp URL, valid 1 hour)
  → streams file bytes to browser
Note: The in-memory store resets on restart. For persistent links across restarts, swap FILE_STORE for a SQLite/Redis store.
Endpoints
Route
Purpose
POST /webhook/<token>
Telegram webhook receiver
GET  /dl/<token>
Stream & download the file
GET  /file/<token>
HTML info page
GET  /
Health check / stats
Persistent Storage (optional upgrade)
Replace the in-memory FILE_STORE dict with SQLite:
import sqlite3, json

DB = sqlite3.connect("files.db", check_same_thread=False)
DB.execute("""CREATE TABLE IF NOT EXISTS files
              (token TEXT PRIMARY KEY, data TEXT)""")

def register_file(file_id, file_name, mime, size):
    token = make_token(file_id)
    DB.execute("INSERT OR REPLACE INTO files VALUES (?,?)",
               (token, json.dumps({...})))
    DB.commit()
    return token

def get_entry(token):
    row = DB.execute("SELECT data FROM files WHERE token=?", (token,)).fetchone()
    return json.loads(row[0]) if row else None
Add a Railway Volume mount so the DB survives deploys.
