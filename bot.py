import os
import json
import base64
import re
import time
import unicodedata
from urllib.request import Request, urlopen
from urllib.error import HTTPError

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


# =========================
# ENVIRONMENT VARIABLES
# =========================

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

WEBHOOK_SECRET = os.environ.get(
    "WEBHOOK_SECRET",
    "telegram-bot-secret"
)

PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]

ADMIN_ID = int(os.environ["ADMIN_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]


# =========================
# GITHUB SETTINGS
# =========================

GITHUB_OWNER = "Akhror4ek"
GITHUB_REPO = "telegram-group-reply-bot"
GITHUB_BRANCH = "main"

PRODUCTS_FILE = "products.json"
PRODUCTS_FOLDER = "products"


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
# ADMIN PRODUCT STATE
# =========================

admin_states = {}


# =========================
# PRODUCTS CACHE
# =========================

products_cache = []
products_cache_time = 0


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================

def format_money(value):
    return f"{int(value):,}".replace(",", " ") + " so'm"


def calculate_monthly(price):
    month_3 = round(price / 3)
    month_6 = round((price * 1.18) / 6)
    month_12 = round((price * 1.36) / 12)

    return month_3, month_6, month_12


def normalize_text(text):
    text = text.lower()

    replacements = {
        "ў": "o",
        "қ": "q",
        "ғ": "g",
        "ҳ": "h",
        "ё": "yo",
        "ъ": "",
        "’": "'",
        "`": "'",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = unicodedata.normalize(
        "NFKD",
        text
    ).encode(
        "ascii",
        "ignore"
    ).decode("ascii")

    text = re.sub(r"[^a-z0-9\s]", " ", text)

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text


def slugify(text):
    text = normalize_text(text)

    text = text.replace(
        " ",
        "_"
    )

    if not text:
        text = "product"

    return text[:60]


def parse_price(text):
    digits = re.sub(
        r"\D",
        "",
        text
    )

    if not digits:
        return None

    try:
        value = int(digits)

        if value <= 0:
            return None

        return value

    except Exception:
        return None


# =========================
# GITHUB API
# =========================

def github_api(
    method,
    path,
    data=None
):
    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/contents/"
        f"{path}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AKSO-AI-Bot",
        "Content-Type": "application/json",
    }

    request = Request(
        url,
        method=method,
        headers=headers,
    )

    if data is not None:
        request.data = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

    try:
        with urlopen(
            request,
            timeout=30
        ) as response:

            raw = response.read()

            if not raw:
                return {}

            return json.loads(
                raw.decode("utf-8")
            )

    except HTTPError as e:
        body = e.read().decode(
            "utf-8",
            errors="ignore"
        )

        raise Exception(
            f"GitHub API {e.code}: {body}"
        )


def github_upload_file(
    path,
    file_bytes,
    commit_message
):
    content = base64.b64encode(
        file_bytes
    ).decode("ascii")

    data = {
        "message": commit_message,
        "content": content,
        "branch": GITHUB_BRANCH,
    }

    return github_api(
        "PUT",
        path,
        data
    )


def github_get_products():
    global products_cache
    global products_cache_time

    now = time.time()

    # 60 soniya cache
    if (
        products_cache
        and now - products_cache_time < 60
    ):
        return products_cache

    try:
        result = github_api(
            "GET",
            f"{PRODUCTS_FILE}?ref={GITHUB_BRANCH}"
        )

        content = result.get(
            "content",
            ""
        )

        if not content:
            products_cache = []
            products_cache_time = now
            return []

        content = content.replace(
            "\n",
            ""
        )

        decoded = base64.b64decode(
            content
        ).decode("utf-8")

        products = json.loads(
            decoded
        )

        if not isinstance(
            products,
            list
        ):
            products = []

        products_cache = products
        products_cache_time = now

        return products

    except Exception as e:

        # products.json hali mavjud bo'lmasa
        if "404" in str(e):
            products_cache = []
            products_cache_time = now
            return []

        raise


def github_save_products(products):
    global products_cache
    global products_cache_time

    content = json.dumps(
        products,
        ensure_ascii=False,
        indent=2
    ).encode("utf-8")

    encoded = base64.b64encode(
        content
    ).decode("ascii")

    sha = None

    try:
        current = github_api(
            "GET",
            f"{PRODUCTS_FILE}?ref={GITHUB_BRANCH}"
        )

        sha = current.get(
            "sha"
        )

    except Exception as e:

        if "404" not in str(e):
            raise

    data = {
        "message": "Update product catalog",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }

    if sha:
        data["sha"] = sha

    github_api(
        "PUT",
        PRODUCTS_FILE,
        data
    )

    products_cache = products
    products_cache_time = time.time()


# =========================
# MAHSULOTNI TELEGRAMDA KO'RSATISH
# =========================

async def send_product(
    update,
    product
):
    price = int(
        product["price"]
    )

    month_3, month_6, month_12 = calculate_monthly(
        price
    )

    caption = (
        f"🛍 <b>{product['name']}</b>\n\n"
        f"💵 Naqd: <b>{format_money(price)}</b>\n\n"
        f"📅 Bo'lib to'lash:\n"
        f"• 3 oy — <b>{format_money(month_3)}/oy</b>\n"
        f"• 6 oy — <b>{format_money(month_6)}/oy</b>\n"
        f"• 12 oy — <b>{format_money(month_12)}/oy</b>"
    )

    try:
        await update.message.reply_photo(
            photo=product["telegram_file_id"],
            caption=caption,
            parse_mode="HTML"
        )

    except Exception as e:
        print(
            "TELEGRAM FILE_ID XATOSI:",
            repr(e)
        )

        # Agar file_id bilan yuborishning iloji bo'lmasa,
        # GitHub raw URL orqali yuborishga urinadi.

        raw_url = product.get(
            "raw_url"
        )

        if raw_url:
            await update.message.reply_photo(
                photo=raw_url,
                caption=caption,
                parse_mode="HTML"
            )


# =========================
# MAHSULOT QIDIRISH
# =========================

def find_local_products(
    user_text,
    products
):
    query = normalize_text(
        user_text
    )

    if not query:
        return []

    scored = []

    for product in products:

        name = normalize_text(
            product.get(
                "name",
                ""
            )
        )

        keywords = product.get(
            "keywords",
            []
        )

        search_words = []

        if name:
            search_words.extend(
                name.split()
            )

        for keyword in keywords:
            search_words.append(
                normalize_text(
                    keyword
                )
            )

        score = 0

        if name and name in query:
            score += 20

        for word in search_words:

            if not word:
                continue

            if len(word) < 2:
                continue

            if word in query:
                score += 5

        if score > 0:
            scored.append(
                (
                    score,
                    product
                )
            )

    scored.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return [
        product
        for score, product in scored[:5]
    ]


async def find_ai_products(
    user_text,
    products
):
    if not products:
        return []

    catalog_lines = []

    for index, product in enumerate(
        products
    ):
        catalog_lines.append(
            f"{index}: {product['name']}"
        )

    catalog_text = "\n".join(
        catalog_lines
    )

    prompt = f"""
Sen mahsulot katalogidan mos mahsulotni topuvchi yordamchisan.

Foydalanuvchi so'rovi:
{user_text}

Katalog:
{catalog_text}

Vazifa:
Foydalanuvchi so'roviga mos keladigan katalog mahsulotlarining indekslarini top.

Faqat mos indekslarni vergul bilan yoz.
Masalan:
0,2,5

Agar mos mahsulot bo'lmasa:
NONE

Hech qanday izoh yozma.
"""

    try:

        response = await ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )

        result = (
            response.text or ""
        ).strip()

        if not result:
            return []

        if result.upper() == "NONE":
            return []

        indexes = re.findall(
            r"\d+",
            result
        )

        selected = []

        for value in indexes:

            index = int(value)

            if (
                0 <= index < len(products)
            ):
                product = products[index]

                if product not in selected:
                    selected.append(
                        product
                    )

            if len(selected) >= 5:
                break

        return selected

    except Exception as e:
        print(
            "AI KATALOG XATOSI:",
            repr(e)
        )

        return []


# =========================
# /start
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
# /id
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
# /addproduct
# =========================

async def add_product_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user:
        return

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    admin_states[user.id] = {
        "step": "photo"
    }

    await update.message.reply_text(
        "➕ <b>Yangi mahsulot qo'shish</b>\n\n"
        "1/3\n"
        "📸 Mahsulot rasmini yuboring.",
        parse_mode="HTML"
    )


# =========================
# /cancel
# =========================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user:
        return

    if user.id == ADMIN_ID:
        admin_states.pop(
            user.id,
            None
        )

        await update.message.reply_text(
            "❌ Mahsulot qo'shish bekor qilindi."
        )


# =========================
# ADMIN MAHSULOT QO'SHISH
# =========================

async def handle_admin_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return False

    user = update.message.from_user

    if not user:
        return False

    if user.id != ADMIN_ID:
        return False

    state = admin_states.get(
        user.id
    )

    if not state:
        return False

    step = state.get(
        "step"
    )

    # -------------------------
    # 1. RASM
    # -------------------------

    if step == "photo":

        if not update.message.photo:

            await update.message.reply_text(
                "📸 Iltimos, mahsulotning rasmini yuboring."
            )

            return True

        try:

            photo = update.message.photo[-1]

            telegram_file = await context.bot.get_file(
                photo.file_id
            )

            image_bytes = await telegram_file.download_as_bytearray()

            state["telegram_file_id"] = (
                photo.file_id
            )

            state["image_bytes"] = bytes(
                image_bytes
            )

            state["step"] = "name"

            await update.message.reply_text(
                "✅ Rasm qabul qilindi.\n\n"
                "2/3\n"
                "📝 Endi mahsulot nomini yozing.\n\n"
                "Masalan:\n"
                "<b>Sandiq №1</b>",
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "ADMIN RASM XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Rasmni qabul qilishda xatolik yuz berdi. "
                "Qaytadan yuboring."
            )

        return True

    # -------------------------
    # 2. NOMI
    # -------------------------

    if step == "name":

        if not update.message.text:

            await update.message.reply_text(
                "📝 Iltimos, mahsulot nomini matn ko'rinishida yozing."
            )

            return True

        name = update.message.text.strip()

        if not name:

            await update.message.reply_text(
                "❌ Mahsulot nomi bo'sh bo'lishi mumkin emas."
            )

            return True

        state["name"] = name

        state["step"] = "price"

        await update.message.reply_text(
            "✅ Mahsulot nomi saqlandi.\n\n"
            "3/3\n"
            "💰 Endi mahsulotning naqd narxini yozing.\n\n"
            "Masalan:\n"
            "<b>1500000</b>\n"
            "yoki\n"
            "<b>1 500 000</b>",
            parse_mode="HTML"
        )

        return True

    # -------------------------
    # 3. NARX
    # -------------------------

    if step == "price":

        if not update.message.text:

            await update.message.reply_text(
                "💰 Iltimos, narxni raqam bilan yozing."
            )

            return True

        price = parse_price(
            update.message.text
        )

        if price is None:

            await update.message.reply_text(
                "❌ Narxni tushuna olmadim.\n\n"
                "Masalan: 1500000"
            )

            return True

        state["price"] = price

        await update.message.reply_text(
            "⏳ Mahsulot GitHub'ga saqlanmoqda..."
        )

        try:

            name = state["name"]
            image_bytes = state["image_bytes"]
            telegram_file_id = state[
                "telegram_file_id"
            ]

            filename = (
                f"{slugify(name)}_"
                f"{int(time.time())}.jpg"
            )

            github_path = (
                f"{PRODUCTS_FOLDER}/"
                f"{filename}"
            )

            # 1. Rasmni GitHub'ga yuklash
            github_upload_file(
                github_path,
                image_bytes,
                f"Add product image: {name}"
            )

            raw_url = (
                f"https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/"
                f"{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/"
                f"{github_path}"
            )

            # 2. Mahsulot katalogini olish
            products = github_get_products()

            # 3. Mahsulot uchun qidiruv so'zlari
            normalized_name = normalize_text(
                name
            )

            keywords = [
                word
                for word in normalized_name.split()
                if len(word) >= 2
            ]

            product = {
                "id": str(
                    int(time.time() * 1000)
                ),
                "name": name,
                "price": price,
                "telegram_file_id": telegram_file_id,
                "github_path": github_path,
                "raw_url": raw_url,
                "keywords": keywords,
                "created_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }

            products.append(
                product
            )

            # 4. products.json'ga saqlash
            github_save_products(
                products
            )

            # 5. Admin holatini tozalash
            admin_states.pop(
                user.id,
                None
            )

            month_3, month_6, month_12 = calculate_monthly(
                price
            )

            await update.message.reply_text(
                "✅ <b>Mahsulot muvaffaqiyatli saqlandi!</b>\n\n"
                f"🛍 <b>{name}</b>\n"
                f"💵 Naqd: <b>{format_money(price)}</b>\n\n"
                f"📅 3 oy — <b>{format_money(month_3)}/oy</b>\n"
                f"📅 6 oy — <b>{format_money(month_6)}/oy</b>\n"
                f"📅 12 oy — <b>{format_money(month_12)}/oy</b>\n\n"
                "📦 Rasm va ma'lumot GitHub'ga saqlandi.",
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "GITHUB MAHSULOT XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Mahsulotni GitHub'ga saqlashda xatolik yuz berdi.\n\n"
                "Render Logs bo'limidagi xatoni tekshiramiz."
            )

            admin_states.pop(
                user.id,
                None
            )

        return True

    return False


# =========================
# ASOSIY AI + MAHSULOT
# =========================

async def reply_to_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    # Botlarning xabariga javob bermaslik
    if (
        update.message.from_user
        and update.message.from_user.is_bot
    ):
        return

    # -------------------------
    # ADMIN MAHSULOT QO'SHISH
    # -------------------------

    handled = await handle_admin_product(
        update,
        context
    )

    if handled:
        return

    # -------------------------
    # RASM TAHLILI
    # -------------------------

    if update.message.photo:

        try:

            photo = update.message.photo[-1]

            file = await context.bot.get_file(
                photo.file_id
            )

            image_bytes = await file.download_as_bytearray()

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
                answer = (
                    "Kechirasiz, rasmni "
                    "tahlil qila olmadim."
                )

            if len(answer) > 4000:
                answer = answer[:4000] + "..."

            if update.message.chat.type in [
                "group",
                "supergroup"
            ]:

                await update.message.reply_text(
                    answer,
                    reply_to_message_id=update.message.message_id
                )

            else:

                await update.message.reply_text(
                    answer
                )

        except Exception as e:

            print(
                "RASM GEMINI XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "Kechirasiz, rasmni tahlil qilishda "
                "texnik xatolik yuz berdi."
            )

        return

    # -------------------------
    # ODDIY MATN
    # -------------------------

    if not update.message.text:
        return

    user_text = update.message.text.strip()

    if not user_text:
        return

    # -------------------------
    # MAHSULOT KATALOGINI QIDIRISH
    # -------------------------

    try:

        products = github_get_products()

        local_products = find_local_products(
            user_text,
            products
        )

        if local_products:

            for product in local_products:

                await send_product(
                    update,
                    product
                )

            return

        # Mahalliy qidiruv topmasa,
        # Gemini orqali katalogdan qidiramiz.

        ai_products = await find_ai_products(
            user_text,
            products
        )

        if ai_products:

            for product in ai_products:

                await send_product(
                    update,
                    product
                )

            return

    except Exception as e:

        print(
            "KATALOG QIDIRUV XATOSI:",
            repr(e)
        )

    # -------------------------
    # ODDIY GEMINI JAVOBI
    # -------------------------

    try:

        prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchiga uning xabariga qarab
tabiiy, foydali va aniq javob ber.

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
            answer = (
                "Kechirasiz, hozir javob "
                "bera olmadim."
            )

        if len(answer) > 4000:
            answer = answer[:4000] + "..."

        if update.message.chat.type in [
            "group",
            "supergroup"
        ]:

            await update.message.reply_text(
                answer,
                reply_to_message_id=update.message.message_id
            )

        else:

            await update.message.reply_text(
                answer
            )

    except Exception as e:

        print(
            "GEMINI XATOSI:",
            repr(e)
        )

        try:

            if update.message.chat.type in [
                "group",
                "supergroup"
            ]:

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
# COMMAND HANDLERS
# =========================

telegram_app.add_handler(
    CommandHandler(
        "start",
        start_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "id",
        my_id_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "addproduct",
        add_product_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "cancel",
        cancel_command
    )
)


# =========================
# MESSAGE HANDLER
# =========================

telegram_app.add_handler(
    MessageHandler(
        (
            filters.TEXT
            | filters.PHOTO
        ) & ~filters.COMMAND,
        reply_to_message
    )
)


# =========================
# START WEBHOOK
# =========================

if __name__ == "__main__":

    print(
        "Bot ishga tushmoqda..."
    )

    print(
        "Render URL:",
        BASE_URL
    )

    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message"
        ],
        drop_pending_updates=True,
    )
