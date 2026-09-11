import os
import json
import base64
import re
import time
import unicodedata
from difflib import SequenceMatcher
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
    """
    O'zbek, rus va ingliz tilidagi yozuvlarni
    qidiruv uchun yagona ko'rinishga keltiradi.
    """

    text = (text or "").lower().strip()

    replacements = {
        # O'zbek kirill
        "ў": "o",
        "қ": "q",
        "ғ": "g",
        "ҳ": "h",

        # Rus / kirill
        "ё": "yo",
        "й": "y",
        "ю": "yu",
        "я": "ya",
        "ш": "sh",
        "ч": "ch",
        "ц": "ts",
        "щ": "shch",
        "ж": "j",
        "х": "x",
        "э": "e",
        "ъ": "",
        "ь": "",
        "ы": "y",

        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "з": "z",
        "и": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",

        # Apostroflar
        "‘": "'",
        "’": "'",
        "`": "'",
        "ʻ": "'",
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

    text = text.replace(
        "'",
        ""
    )

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text


def stem_token(token):
    """
    Sandiqlarni -> sandiq
    shkaflar -> shkaf
    kullerlar -> kuller
    kabi oddiy qo'shimchalarni ajratadi.
    """

    token = normalize_text(token)

    if len(token) <= 3:
        return token

    suffixes = [
        "larning",
        "larni",
        "lar",
        "ning",
        "dan",
        "dagi",
        "ga",
        "da",
        "ni",
        "mi",
    ]

    changed = True

    while changed and len(token) > 3:

        changed = False

        for suffix in suffixes:

            if (
                token.endswith(suffix)
                and len(token) - len(suffix) >= 3
            ):
                token = token[:-len(suffix)]
                changed = True
                break

    return token


def token_list(text):

    result = []

    for word in normalize_text(
        text
    ).split():

        stemmed = stem_token(
            word
        )

        if len(stemmed) >= 2:
            result.append(
                stemmed
            )

    return result


def slugify(text):

    text = normalize_text(
        text
    )

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
        text or ""
    )

    if not digits:
        return None

    try:

        value = int(
            digits
        )

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

        if "404" in str(e):

            products_cache = []
            products_cache_time = now

            return []

        raise


def github_save_products(
    products
):

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
# FUZZY QIDIRUV
# =========================

def fuzzy_product_score(
    user_text,
    product
):
    """
    Imlo xatolari va kichik farqlarni ushlaydi.

    Masalan:
    kuler -> kuller
    shkaflar -> shkaf
    sandiqlarni -> sandiq
    """

    query = normalize_text(
        user_text
    )

    if not query:
        return 0

    query_tokens = set(
        token_list(user_text)
    )

    name = product.get(
        "name",
        ""
    )

    name_normalized = normalize_text(
        name
    )

    name_tokens = set(
        token_list(name)
    )

    for keyword in product.get(
        "keywords",
        []
    ):

        name_tokens.update(
            token_list(keyword)
        )

    if not name_tokens:
        return 0

    score = 0

    # To'liq nom mosligi
    if (
        name_normalized
        and name_normalized in query
    ):
        score += 100

    # Har bir so'zning eng yaqin mosligini topamiz.
    for qt in query_tokens:

        best = 0.0

        for nt in name_tokens:

            if (
                len(qt) < 3
                or len(nt) < 3
            ):
                continue

            similarity = SequenceMatcher(
                None,
                qt,
                nt
            ).ratio()

            if similarity > best:
                best = similarity

        if best >= 0.90:
            score += 40

        elif best >= 0.82:
            score += 25

        elif best >= 0.74:
            score += 12

        if qt in name_tokens:
            score += 35

    overlap = len(
        query_tokens.intersection(
            name_tokens
        )
    )

    score += overlap * 20

    return score


def find_local_products(
    user_text,
    products
):

    scored = []

    for product in products:

        score = fuzzy_product_score(
            user_text,
            product
        )

        if score >= 20:

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
        for score, product
        in scored[:5]
    ]


# =========================
# AI SEMANTIK KATALOG QIDIRUVI
# =========================

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

        keywords = ", ".join(
            product.get(
                "keywords",
                []
            )
        )

        catalog_lines.append(
            f"{index}: "
            f"{product.get('name', '')} "
            f"| kalit so'zlar: {keywords}"
        )

    catalog_text = "\n".join(
        catalog_lines
    )

    prompt = f"""
Sen AKSO SAVDO mahsulot katalogidan
mijoz so'ragan mahsulotni topuvchi yordamchisan.

Mijozning so'rovi:
{user_text}

Katalog:
{catalog_text}

MUHIM:
Mijoz mahsulot nomini aynan katalogdagi
nom bilan yozishi shart emas.

Quyidagilarni tushun:

1. Imlo xatolari.
2. Harf almashishi.
3. Birlik va ko'plik.
4. O'zbekcha va ruscha yozilish.
5. Sinonimlar.
6. Mahsulotni nomi bilan emas,
   vazifasi yoki ko'rinishi bilan so'rash.
7. Og'zaki va qisqartirilgan yozuvlar.
8. Transliteratsiya.

Misollar:

"kuler"
-> "kuller"

"kuller bormi?"
-> katalogdagi kullerlar

"shkaf bormi?"
-> katalogdagi shkaflar

"detski shkaflar"
-> bolalar shkaflari

"bolalar uchun shkaf"
-> bolalar shkaflari

"kiyim osadigan mebel"
-> kiyim saqlash/osish uchun shkaflar

"kiyim uchun mebel"
-> shkaflar

"stol kerak"
-> katalogdagi stollar

"divan kerak"
-> katalogdagi divanlar

"yotadigan mebel"
-> mos divan yoki yotoq mahsulotlari

Agar bir nechta mahsulot mos bo'lsa,
ularning barchasini tanla.

Eng ko'p 5 ta mahsulot tanla.

FAqat JSON qaytar:

[0, 2, 5]

Mos mahsulot bo'lmasa:

[]

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

        # Model ```json ... ``` yuborsa ham
        # ichidagi massivni ajratib olamiz.
        match = re.search(
            r"\[[^\]]*\]",
            result,
            flags=re.DOTALL
        )

        if not match:
            return []

        parsed = json.loads(
            match.group(0)
        )

        if not isinstance(
            parsed,
            list
        ):
            return []

        selected = []

        for value in parsed:

            try:
                index = int(value)

            except (
                TypeError,
                ValueError
            ):
                continue

            if 0 <= index < len(products):

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
# MAHSULOTNI MIJOZGA KO'RSATISH
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

    # MUHIM:
    # MIJOZGA NAQD NARX KO'RSATILMAYDI.
    # FAQAT OYLIK TO'LOVLAR KO'RSATILADI.

    caption = (
        f"🛍 <b>{product['name']}</b>\n\n"
        f"📅 <b>Bo'lib to'lash:</b>\n"
        f"• 3 oy — <b>{format_money(month_3)}/oy</b>\n"
        f"• 6 oy — <b>{format_money(month_6)}/oy</b>\n"
        f"• 12 oy — <b>{format_money(month_12)}/oy</b>"
    )

    try:

        await update.message.reply_photo(
            photo=product[
                "telegram_file_id"
            ],
            caption=caption,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            "TELEGRAM FILE_ID XATOSI:",
            repr(e)
        )

        raw_url = product.get(
            "raw_url"
        )

        if raw_url:

            await update.message.reply_photo(
                photo=raw_url,
                caption=caption,
                parse_mode="HTML"
            )

        else:

            raise


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
        "👋 Assalomu alaykum! "
        "Men AKSO AI botman.\n\n"
        "Menga istalgan savolingizni "
        "yozishingiz mumkin. 🤖"
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
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
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

    # =========================
    # 1. RASM
    # =========================

    if step == "photo":

        if not update.message.photo:

            await update.message.reply_text(
                "📸 Iltimos, mahsulotning "
                "rasmini yuboring."
            )

            return True

        try:

            photo = (
                update.message.photo[-1]
            )

            telegram_file = (
                await context.bot.get_file(
                    photo.file_id
                )
            )

            image_bytes = (
                await telegram_file.download_as_bytearray()
            )

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
                "❌ Rasmni qabul qilishda "
                "xatolik yuz berdi. "
                "Qaytadan yuboring."
            )

        return True

    # =========================
    # 2. NOMI
    # =========================

    if step == "name":

        if not update.message.text:

            await update.message.reply_text(
                "📝 Iltimos, mahsulot nomini "
                "matn ko'rinishida yozing."
            )

            return True

        name = (
            update.message.text.strip()
        )

        if not name:

            await update.message.reply_text(
                "❌ Mahsulot nomi bo'sh "
                "bo'lishi mumkin emas."
            )

            return True

        state["name"] = name

        state["step"] = "price"

        await update.message.reply_text(
            "✅ Mahsulot nomi saqlandi.\n\n"
            "3/3\n"
            "💰 Endi mahsulotning "
            "naqd narxini yozing.\n\n"
            "Masalan:\n"
            "<b>1500000</b>\n"
            "yoki\n"
            "<b>1 500 000</b>",
            parse_mode="HTML"
        )

        return True

    # =========================
    # 3. NARX
    # =========================

    if step == "price":

        if not update.message.text:

            await update.message.reply_text(
                "💰 Iltimos, narxni "
                "raqam bilan yozing."
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
            "⏳ Mahsulot GitHub'ga "
            "saqlanmoqda..."
        )

        try:

            name = state[
                "name"
            ]

            image_bytes = state[
                "image_bytes"
            ]

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

            # 1. Rasm GitHub'ga yuklanadi

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

            # 2. Katalog

            products = (
                github_get_products()
            )

            # 3. Qidiruv so'zlari

            normalized_name = (
                normalize_text(name)
            )

            keywords = [
                word
                for word
                in normalized_name.split()
                if len(
                    stem_token(word)
                ) >= 2
            ]

            product = {
                "id": str(
                    int(
                        time.time() * 1000
                    )
                ),
                "name": name,
                "price": price,
                "telegram_file_id": (
                    telegram_file_id
                ),
                "github_path": (
                    github_path
                ),
                "raw_url": raw_url,
                "keywords": keywords,
                "created_at": (
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                ),
            }

            products.append(
                product
            )

            # 4. Katalogni saqlash

            github_save_products(
                products
            )

            # 5. Holatni tozalash

            admin_states.pop(
                user.id,
                None
            )

            month_3, month_6, month_12 = (
                calculate_monthly(
                    price
                )
            )

            # Bu faqat ADMIN uchun.

            await update.message.reply_text(
                "✅ <b>Mahsulot "
                "muvaffaqiyatli saqlandi!</b>\n\n"
                f"🛍 <b>{name}</b>\n"
                f"💵 Naqd: "
                f"<b>{format_money(price)}</b>\n\n"
                f"📅 3 oy — "
                f"<b>{format_money(month_3)}/oy</b>\n"
                f"📅 6 oy — "
                f"<b>{format_money(month_6)}/oy</b>\n"
                f"📅 12 oy — "
                f"<b>{format_money(month_12)}/oy</b>\n\n"
                "📦 Rasm va ma'lumot "
                "GitHub'ga saqlandi.",
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "GITHUB MAHSULOT XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Mahsulotni GitHub'ga "
                "saqlashda xatolik yuz berdi.\n\n"
                "Render Logs bo'limidagi "
                "xatoni tekshiramiz."
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

    # Bot xabarlariga javob bermaslik

    if (
        update.message.from_user
        and update.message.from_user.is_bot
    ):
        return

    # =========================
    # ADMIN MAHSULOT QO'SHISH
    # =========================

    handled = (
        await handle_admin_product(
            update,
            context
        )
    )

    if handled:
        return

    # =========================
    # RASM TAHLILI
    # =========================

    if update.message.photo:

        try:

            photo = (
                update.message.photo[-1]
            )

            file = (
                await context.bot.get_file(
                    photo.file_id
                )
            )

            image_bytes = (
                await file.download_as_bytearray()
            )

            user_text = (
                update.message.caption.strip()
                if update.message.caption
                else
                "Bu rasmda nima borligini "
                "batafsil tushuntir."
            )

            prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchi senga rasm yubordi.

Rasmni diqqat bilan tahlil qil
va foydalanuvchining savoliga javob ber.

Qoidalar:
- O'zbek tilida yozilsa,
  o'zbek tilida javob ber.
- Rus tilida yozilsa,
  rus tilida javob ber.
- Ingliz tilida yozilsa,
  ingliz tilida javob ber.
- Rasmda ko'rinadigan narsalarni
  aniq tasvirla.
- Bilmagan narsangni taxmin qilib
  fakt sifatida aytma.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob bermagin.

Foydalanuvchi savoli:
{user_text}
"""

            image_part = (
                types.Part.from_bytes(
                    data=bytes(
                        image_bytes
                    ),
                    mime_type="image/jpeg"
                )
            )

            response = (
                await ai_client.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=[
                        image_part,
                        prompt
                    ]
                )
            )

            answer = response.text

            if not answer:

                answer = (
                    "Kechirasiz, rasmni "
                    "tahlil qila olmadim."
                )

            if len(answer) > 4000:

                answer = (
                    answer[:4000]
                    + "..."
                )

            if update.message.chat.type in [
                "group",
                "supergroup"
            ]:

                await update.message.reply_text(
                    answer,
                    reply_to_message_id=(
                        update.message.message_id
                    )
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
                "Kechirasiz, rasmni "
                "tahlil qilishda "
                "texnik xatolik yuz berdi."
            )

        return

    # =========================
    # ODDIY MATN
    # =========================

    if not update.message.text:
        return

    user_text = (
        update.message.text.strip()
    )

    if not user_text:
        return

    # =========================
    # MAHSULOT KATALOGI
    # =========================

    try:

        products = (
            github_get_products()
        )

        if products:

            # 1. Avval xato yozuv / typo qidiruvi

            local_products = (
                find_local_products(
                    user_text,
                    products
                )
            )

            if local_products:

                for product in local_products:

                    await send_product(
                        update,
                        product
                    )

                return

            # 2. Keyin ma'no bo'yicha AI qidiruvi

            ai_products = (
                await find_ai_products(
                    user_text,
                    products
                )
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

    # =========================
    # ODDIY GEMINI JAVOBI
    # =========================

    try:

        prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchiga uning xabariga qarab
tabiiy, foydali va aniq javob ber.

Qoidalar:
- O'zbek tilida yozilsa,
  o'zbek tilida javob ber.
- Rus tilida yozilsa,
  rus tilida javob ber.
- Ingliz tilida yozilsa,
  ingliz tilida javob ber.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob bermagin.
- Oddiy savolga oddiy va aniq javob ber.
- Salomlashishga odob bilan javob ber.
- Foydalanuvchi xabarini qayta takrorlama.

Foydalanuvchi xabari:
{user_text}
"""

        response = (
            await ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt
            )
        )

        answer = response.text

        if not answer:

            answer = (
                "Kechirasiz, hozir javob "
                "bera olmadim."
            )

        if len(answer) > 4000:

            answer = (
                answer[:4000]
                + "..."
            )

        if update.message.chat.type in [
            "group",
            "supergroup"
        ]:

            await update.message.reply_text(
                answer,
                reply_to_message_id=(
                    update.message.message_id
                )
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
                    "Kechirasiz, hozir "
                    "javob berishda "
                    "texnik xatolik yuz berdi.",
                    reply_to_message_id=(
                        update.message.message_id
                    )
                )

            else:

                await update.message.reply_text(
                    "Kechirasiz, hozir "
                    "javob berishda "
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
