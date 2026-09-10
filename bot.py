import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "telegram-bot-secret")
PORT = int(os.environ.get("PORT", "10000"))

app = Flask(__name__)
telegram_app = Application.builder().token(TOKEN).build()

async def reply_to_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    # Ignore messages sent by bots and commands.
    if update.message.from_user and update.message.from_user.is_bot:
        return
    if update.message.text and update.message.text.startswith("/"):
        return

    await update.message.reply_text(
        "Lichkangizga maʼlumot yubordik ✅",
        reply_to_message_id=update.message.message_id,
    )

telegram_app.add_handler(
    MessageHandler(filters.ALL & ~filters.COMMAND, reply_to_group_message)
)

@app.get("/")
def health():
    return "Bot is running", 200

@app.post("/webhook")
async def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "Unauthorized", 401

    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    await telegram_app.process_update(update)
    return "OK", 200

if __name__ == "__main__":
    import asyncio

    async def main():
        await telegram_app.initialize()
        await telegram_app.start()

        # Render provides a public HTTPS URL through RENDER_EXTERNAL_URL.
        base_url = os.environ["RENDER_EXTERNAL_URL"]
        await telegram_app.bot.set_webhook(
            url=f"{base_url}/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"],
        )

        app.run(host="0.0.0.0", port=PORT)

    asyncio.run(main())
