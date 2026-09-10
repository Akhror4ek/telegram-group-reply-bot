import os

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

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

BASE_URL = os.environ["RENDER_EXTERNAL_URL"]


# =========================
# GEMINI AI
# =========================

ai_client = genai.Client(
    api_key=GEMINI_API_KEY
).aio


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

    if not update.message:
        return

    # Botlarning xabarlariga javob bermaymiz
    if (
        update.message.from_user
        and update.message.from_user.is_bot
    ):
        return

    # Faqat matnli xabarlar
    if not update.message.text:
        return

    user_text = update.message.text.strip()

    # /start, /help va boshqa komandalarni o'tkazib yuboramiz
    if user_text.startswith("/"):
        return

    try:

        prompt = f"""
Sen Telegram guruhidagi aqlli yordamchi botsan.

Foydalanuvchining xabariga mazmuniga qarab javob ber.

Qoidalar:
- O'zbek tilida yozilsa, o'zbek tilida javob ber.
- Rus tilida yozilsa, rus tilida javob ber.
- Ingliz tilida yozilsa, ingliz tilida javob ber.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob bermagin.
- Oddiy savolga oddiy va aniq javob ber.
- Salomlashishga odob bilan javob ber.
- Foydalanuvchi xabarini qayta takrorlama.

Foydalanuvchi xabari:

{user_text}
"""

        # Gemini so'rovini alohida thread'da bajarish
        
response = await ai_client.models.generate_content(
    model="gemini-3.7-flash",
    contents=prompt
)
        answer = response.text

        if not answer:
            answer = "Kechirasiz, hozir javob bera olmadim."

        # Telegram xabar uzunligi chegarasi
        if len(answer) > 4000:
            answer = answer[:4000] + "..."

        # Javobni AYNAN GURUHDA yuborish
        await update.message.reply_text(
            answer,
            reply_to_message_id=update.message.message_id
        )

    except Exception as e:

        print("GEMINI XATOSI:", repr(e))

        try:
            await update.message.reply_text(
                "Kechirasiz, hozir javob berishda texnik xatolik yuz berdi.",
                reply_to_message_id=update.message.message_id
            )
        except Exception as telegram_error:
            print(
                "TELEGRAM JAVOB XATOSI:",
                repr(telegram_error)
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
# ISHGA TUSHIRISH
# =========================

if __name__ == "__main__":

    print("Bot ishga tushmoqda...")
    print("Render URL:", BASE_URL)

    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
        drop_pending_updates=True,
    )
