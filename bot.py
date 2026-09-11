import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types


BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

WEBHOOK_SECRET = os.environ.get(
    "WEBHOOK_SECRET",
    "telegram-bot-secret"
)

PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]


# Gemini AI
ai_client = genai.Client(
    api_key=GEMINI_API_KEY
).aio


# Telegram bot
telegram_app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .build()
)


# =========================
# /id komandasi
# =========================

async def my_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user:
        return

    await update.message.reply_text(
        f"🆔 Sizning Telegram ID'ingiz:\n\n"
        f"`{user.id}`",
        parse_mode="Markdown"
    )


# =========================
# /start komandasi
# =========================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    await update.message.reply_text(
        "👋 Assalomu alaykum! Men AKSO AI botman.\n\n"
        "Menga istalgan savolingizni yozishingiz mumkin. 🤖"
    )


# =========================
# AI javob
# =========================

async def reply_to_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    # Botlarning xabariga javob bermaslik
    if update.message.from_user and update.message.from_user.is_bot:
        return

    # =========================
    # Rasm yuborilgan bo'lsa
    # =========================

    if update.message.photo:

        try:
            # Telegramdagi eng katta sifatli rasmni olamiz
            photo = update.message.photo[-1]

            # Rasm faylini Telegram serveridan yuklab olamiz
            file = await context.bot.get_file(photo.file_id)

            image_bytes = await file.download_as_bytearray()

            # Caption bo'lsa, savol sifatida ishlatamiz
            user_text = (
                update.message.caption.strip()
                if update.message.caption
                else "Bu rasmda nima borligini batafsil tushuntir."
            )

            prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchi senga rasm yubordi.

Rasmni diqqat bilan tahlil qil va foydalanuvchining savoliga javob ber.

Qoidalar:
- O'zbek tilida yozilsa, o'zbek tilida javob ber.
- Rus tilida yozilsa, rus tilida javob ber.
- Ingliz tilida yozilsa, ingliz tilida javob ber.
- Rasmda ko'rinadigan narsalarni aniq tasvirla.
- Bilmagan narsangni taxmin qilib fakt sifatida aytma.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob bermagin.

Foydalanuvchi savoli:
{user_text}
"""

            image_part = types.Part.from_bytes(
                data=bytes(image_bytes),
                mime_type="image/jpeg"
            )

            response = await ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    image_part,
                    prompt
                ]
            )

            answer = response.text

            if not answer:
                answer = "Kechirasiz, rasmni tahlil qila olmadim."

            if len(answer) > 4000:
                answer = answer[:4000] + "..."

            if update.message.chat.type in ["group", "supergroup"]:
                await update.message.reply_text(
                    answer,
                    reply_to_message_id=update.message.message_id
                )
            else:
                await update.message.reply_text(answer)

        except Exception as e:
            print("RASM GEMINI XATOSI:", repr(e))

            await update.message.reply_text(
                "Kechirasiz, rasmni tahlil qilishda texnik xatolik yuz berdi."
            )

        return

    # =========================
    # Oddiy matnli xabar
    # =========================

    if not update.message.text:
        return

    user_text = update.message.text.strip()

    if not user_text:
        return

    try:
        prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchiga uning xabariga qarab tabiiy, foydali va aniq javob ber.

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

        response = await ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )

        answer = response.text

        if not answer:
            answer = "Kechirasiz, hozir javob bera olmadim."

        if len(answer) > 4000:
            answer = answer[:4000] + "..."

        if update.message.chat.type in ["group", "supergroup"]:
            await update.message.reply_text(
                answer,
                reply_to_message_id=update.message.message_id
            )
        else:
            await update.message.reply_text(answer)

    except Exception as e:
        print("GEMINI XATOSI:", repr(e))

        try:
            if update.message.chat.type in ["group", "supergroup"]:
                await update.message.reply_text(
                    "Kechirasiz, hozir javob berishda "
                    "texnik xatolik yuz berdi.",
                    reply_to_message_id=update.message.message_id
                )
            else:
                await update.message.reply_text(
                    "Kechirasiz, hozir javob berishda "
                    "texnik xatolik yuz berdi."
                )
        except Exception as telegram_error:
            print(
                "TELEGRAM JAVOB XATOSI:",
                repr(telegram_error)
            )


# =========================
# /start va /id
# =========================

telegram_app.add_handler(
    CommandHandler("start", start_command)
)

telegram_app.add_handler(
    CommandHandler("id", my_id_command)
)


# =========================
# Matnli va rasmli xabarlar
# =========================

telegram_app.add_handler(
    MessageHandler(
        (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
        reply_to_message
    )
)


# =========================
# Botni ishga tushirish
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
