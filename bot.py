import os
import asyncio
import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
TEST_URL = "http://httpbin.org/ip"
TIMEOUT = 10

async def check_proxy(proxy: str) -> dict:
    proxy = proxy.strip()
    if not proxy:
        return None
    if not proxy.startswith(("http://", "https://", "socks5://")):
        proxy = f"http://{proxy}"
    try:
        start = asyncio.get_event_loop().time()
        async with aiohttp.ClientSession() as session:
            async with session.get(TEST_URL, proxy=proxy, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                elapsed = asyncio.get_event_loop().time() - start
                if r.status == 200:
                    return {"proxy": proxy, "status": "✅ LIVE", "speed": f"{elapsed:.2f}s"}
    except Exception:
        pass
    return {"proxy": proxy, "status": "❌ DEAD", "speed": "N/A"}

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Proxy Checker Bot*\n\n"
        "Send me proxies to check — one per line.\n"
        "Supported formats:\n"
        "`ip:port`\n`http://ip:port`\n`socks5://ip:port`\n\n"
        "Or use /check followed by proxies.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = update.message.text.strip().splitlines()
    proxies = [l.strip() for l in lines if l.strip()]
    if not proxies:
        return

    if len(proxies) > 50:
        await update.message.reply_text("⚠️ Max 50 proxies per request.")
        return

    msg = await update.message.reply_text(f"🔍 Checking {len(proxies)} proxies...")

    tasks = [check_proxy(p) for p in proxies]
    results = await asyncio.gather(*tasks)

    live = [r for r in results if r and "LIVE" in r["status"]]
    dead = [r for r in results if r and "DEAD" in r["status"]]

    lines_out = [f"📊 *Results: {len(live)} live / {len(dead)} dead*\n"]
    for r in live:
        lines_out.append(f"✅ `{r['proxy']}` — {r['speed']}")
    for r in dead:
        lines_out.append(f"❌ `{r['proxy']}`")

    await msg.edit_text("\n".join(lines_out), parse_mode="Markdown")

async def check_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        update.message.text = "\n".join(ctx.args)
        await handle_message(update, ctx)
    else:
        await update.message.reply_text("Usage: /check ip:port ip:port ...")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
