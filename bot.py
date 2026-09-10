import os
import asyncio

from flask import Flask, request
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from google import genai


# =========================
# SOZLAMALAR
# =========================

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

WEBHOOK_SECRET = os.environ.get(
    "WEBHOOK_SECRET",
    "telegram-bot-secret"
)

PORT = int(os.environ.get("PORT", "10000"))


# =========================
# GEMINI AI
# =========================

ai_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================
# FLASK
# =========================

app = Flask(__name__)


# =========================
# TELEGRAM BOT
# =========================

telegram_app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .build()
)


# =========================
# AI JAVOB
# =========================

async def reply_to_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # Xabar mavjud bo'lmasa
    if not update.message:
        return

    # Boshqa botlarning xabarlariga javob bermaymiz
    if (
        update.message.from_user
        and update.message.from_user.is_bot
    ):
        return

    # Faqat matnli xabarlar
    if not update.message.text:
        return

    user_text = update.message.text.strip()

    # Telegram komandalariga javob bermaymiz
    if user_text.startswith("/"):
        return

    try:

        prompt = f"""
Sen Telegram guruhidagi aqlli yordamchi botsan.

Foydalanuvchi yozgan xabarga mazmuniga qarab javob ber.

Asosiy qoidalar:
- Foydalanuvchi o'zbek tilida yozsa, o'zbek tilida javob ber.
- Rus tilida yozsa, rus tilida javob ber.
- Ingliz tilida yozsa, ingliz tilida javob ber.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob yozma.
- Oddiy savollarga oddiy javob ber.
- Hurmat bilan gapir.
- Agar foydalanuvchi shunchaki salomlashsa, salomlashib javob ber.
- Agar savol bersa, imkon qadar aniq javob ber.
- Foydalanuvchi xabarini qayta takrorlama.

Foydalanuvchi xabari:
{user_text}
"""

        # Gemini so'rovini alohida thread'da bajaramiz
        # Bu Telegram botni bloklab qo'ymasligi uchun kerak.
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-3.7-flash",
            contents=prompt
        )

        answer = response.text

        if not answer:
            answer = "Kechirasiz, hozir javob bera olmadim."

        # Telegram bitta xabarda 4096 belgigacha qabul qiladi
        if len(answer) > 4000:
            answer = answer[:4000] + "..."

        # JAVOB AYNAN GURUHNING O'ZIDA
        await update.message.reply_text(
            answer,
            reply_to_message_id=update.message.message_id
        )

    except Exception as e:

        print("GEMINI XATOSI:", repr(e))

        await update.message.reply_text(
            "Kechirasiz, AI bilan bog‘lanishda xatolik yuz berdi.",
            reply_to_message_id=update.message.message_id
        )


# =========================
# MESSAGE HANDLER
# =========================

telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        reply_to_group_message
    )
)


# =========================
# HEALTH CHECK
# =========================

@app.get("/")
def health():
    return "Bot is running", 200


# =========================
# TELEGRAM WEBHOOK
# =========================

@app.post("/webhook")
async def webhook():

    # Telegram xavfsizlik tekshiruvi
    if (
        request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
        != WEBHOOK_SECRET
    ):
        return "Unauthorized", 401

    data = request.get_json(force=True)

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(update)

    return "OK", 200


# =========================
# ISHGA TUSHIRISH
# =========================

if __name__ == "__main__":

    async def main():

        await telegram_app.initialize()
        await telegram_app.start()

        base_url = os.environ["RENDER_EXTERNAL_URL"]

        await telegram_app.bot.set_webhook(
            url=f"{base_url}/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"]
        )

        app.run(
            host="0.0.0.0",
            port=PORT
        )

    asyncio.run(main())
