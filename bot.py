import os
import json
import base64
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
# STATE / CACHE / STATS
# =========================

admin_states = {}

products_cache = []
products_cache_time = 0

stats = {
    "messages": 0,
    "catalog_queries": 0,
    "catalog_matches": 0,
    "products_added": 0,
    "products_edited": 0,
    "products_deleted": 0,
    "products_hidden": 0,
}


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================

def is_admin(user_id):
    return user_id == ADMIN_ID


def format_money(value):
    return (
        f"{int(value):,}"
        .replace(",", " ")
        + " so'm"
    )


def calculate_monthly(price):
    month_3 = round(price / 3)
    month_6 = round((price * 1.18) / 6)
    month_12 = round((price * 1.36) / 12)

    return month_3, month_6, month_12


def normalize_text(text):
    text = (text or "").lower().strip()

    replacements = {
        "ў": "o",
        "қ": "q",
        "ғ": "g",
        "ҳ": "h",
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

    text = text.replace("'", "")

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

    for word in normalize_text(text).split():
        stemmed = stem_token(word)

        if len(stemmed) >= 2:
            result.append(stemmed)

    return result


def slugify(text):
    text = normalize_text(text)
    text = text.replace(" ", "_")

    if not text:
        text = "product"

    return text[:60]


def parse_price(text):
    digits = re.sub(r"\D", "", text or "")

    if not digits:
        return None

    try:
        value = int(digits)

        if value <= 0:
            return None

        return value

    except Exception:
        return None


def product_images(product):
    images = product.get("images")

    if isinstance(images, list) and images:
        return images

    old_file_id = product.get("telegram_file_id")

    if old_file_id:
        return [old_file_id]

    return []


def visible_product(product):
    return product.get("visible", True) is not False


def refresh_product_keywords(product):
    source = " ".join([
        product.get("name", ""),
        product.get("category", ""),
        product.get("description", ""),
    ])

    keywords = []

    for word in normalize_text(source).split():
        stemmed = stem_token(word)

        if len(stemmed) >= 2 and stemmed not in keywords:
            keywords.append(stemmed)

    product["keywords"] = keywords


# =========================
# GITHUB API
# =========================

def github_api(method, path, data=None):
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
        with urlopen(request, timeout=30) as response:
            raw = response.read()

            if not raw:
                return {}

            return json.loads(raw.decode("utf-8"))

    except HTTPError as e:
        body = e.read().decode(
            "utf-8",
            errors="ignore"
        )

        raise Exception(
            f"GitHub API {e.code}: {body}"
        )


def github_upload_file(path, file_bytes, commit_message):
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

        content = result.get("content", "")

        if not content:
            products_cache = []
            products_cache_time = now
            return []

        decoded = base64.b64decode(
            content.replace("\n", "")
        ).decode("utf-8")

        products = json.loads(decoded)

        if not isinstance(products, list):
            products = []

        # Eski mahsulotlarni yangi maydonlarga moslashtiramiz.
        for product in products:
            if "images" not in product:
                old = product.get("telegram_file_id")
                product["images"] = [old] if old else []

            if "visible" not in product:
                product["visible"] = True

            if "category" not in product:
                product["category"] = ""

            if "description" not in product:
                product["description"] = ""

            if "keywords" not in product:
                refresh_product_keywords(product)

        products_cache = products
        products_cache_time = now

        return products

    except Exception as e:
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

        sha = current.get("sha")

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

def fuzzy_product_score(user_text, product):
    query = normalize_text(user_text)

    if not query:
        return 0

    query_tokens = set(
        token_list(user_text)
    )

    searchable = " ".join([
        product.get("name", ""),
        product.get("category", ""),
        product.get("description", ""),
        " ".join(
            product.get("keywords", [])
        ),
    ])

    searchable_tokens = set(
        token_list(searchable)
    )

    name_normalized = normalize_text(
        product.get("name", "")
    )

    if not searchable_tokens:
        return 0

    score = 0

    if (
        name_normalized
        and name_normalized in query
    ):
        score += 100

    for qt in query_tokens:
        best = 0.0

        for nt in searchable_tokens:
            if len(qt) < 3 or len(nt) < 3:
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

        if qt in searchable_tokens:
            score += 35

    overlap = len(
        query_tokens.intersection(
            searchable_tokens
        )
    )

    score += overlap * 20

    return score


def find_local_products(user_text, products):
    scored = []

    for product in products:
        if not visible_product(product):
            continue

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
        for score, product in scored[:5]
    ]


# =========================
# AI SEMANTIK KATALOG
# =========================

async def find_ai_products(user_text, products):
    visible = [
        product
        for product in products
        if visible_product(product)
    ]

    if not visible:
        return []

    candidates = visible[:150]

    catalog_lines = []

    for index, product in enumerate(candidates):
        keywords = ", ".join(
            product.get("keywords", [])
        )

        catalog_lines.append(
            f"{index}: "
            f"{product.get('name', '')} "
            f"| kategoriya: "
            f"{product.get('category', '')} "
            f"| tavsif: "
            f"{product.get('description', '')} "
            f"| kalit so'zlar: "
            f"{keywords}"
        )

    catalog_text = "\n".join(catalog_lines)

    prompt = f"""
Sen AKSO SAVDO mahsulot katalogidan
mijoz so'ragan mahsulotni topuvchi
aqlli katalog yordamchisisan.

Mijozning so'rovi:
{user_text}

Katalog:
{catalog_text}

Mijoz katalogdagi nomni aynan yozishi shart emas.

Tushun:
- imlo xatolari
- harf almashishi
- birlik va ko'plik
- o'zbekcha va ruscha yozilish
- sinonimlar
- og'zaki yozuv
- transliteratsiya
- mahsulotning vazifasi
- mahsulotning ishlatilish maqsadi

Misollar:

"kuler"
-> "kuller"

"kuller bormi?"
-> kullerlar

"detski shkaf"
-> bolalar shkaflari

"shkaf bormi?"
-> shkaflar

"kiyim osadigan mebel"
-> shkaflar

"kiyim qo'yadigan shkaf"
-> shkaflar

"oshxonaga mebel kerak"
-> oshxona mebellari

"stol kerak"
-> stollar

"yotadigan mebel kerak"
-> mos divan/yotoq mahsulotlari

Faqat katalogda mavjud bo'lgan
mahsulotlardan tanla.

Agar bir nechta mahsulot mos bo'lsa,
eng moslarini tanla.

Ko'pi bilan 5 ta indeks tanla.

FAQAT JSON qaytar:

[0, 2, 5]

Mos mahsulot bo'lmasa:

[]

Hech qanday izoh yozma.
"""

    try:
        response = (
            await ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt
            )
        )

        result = (
            response.text or ""
        ).strip()

        if not result:
            return []

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

        if not isinstance(parsed, list):
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

            if (
                0 <= index < len(candidates)
            ):
                product = candidates[index]

                if product not in selected:
                    selected.append(product)

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
# MAHSULOTNI MIJOZGA YUBORISH
# =========================

async def send_product_to_chat(
    bot,
    chat_id,
    product,
    reply_to_message_id=None
):
    price = int(
        product["price"]
    )

    month_3, month_6, month_12 = (
        calculate_monthly(price)
    )

    caption_parts = [
        f"🛍 <b>{product['name']}</b>"
    ]

    if product.get("category"):
        caption_parts.append(
            f"🗂 {product['category']}"
        )

    if product.get("description"):
        caption_parts.append(
            f"\n{product['description']}"
        )

    caption_parts.append(
        "\n📅 <b>Bo'lib to'lash:</b>\n"
        f"• 3 oy — "
        f"<b>{format_money(month_3)}/oy</b>\n"
        f"• 6 oy — "
        f"<b>{format_money(month_6)}/oy</b>\n"
        f"• 12 oy — "
        f"<b>{format_money(month_12)}/oy</b>"
    )

    # Naqd narx MIJOZGA yuborilmaydi.
    caption = "\n".join(
        caption_parts
    )

    images = product_images(
        product
    )

    if not images:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML"
        )
        return

    raw_urls = product.get(
        "raw_urls",
        []
    )

    if not isinstance(raw_urls, list):
        raw_urls = []

    if (
        not raw_urls
        and product.get("raw_url")
    ):
        raw_urls = [
            product["raw_url"]
        ]

    for index, image in enumerate(images):
        try:
            kwargs = {
                "chat_id": chat_id,
                "photo": image,
            }

            if index == 0:
                kwargs["caption"] = caption
                kwargs["parse_mode"] = "HTML"

            if (
                reply_to_message_id is not None
                and index == 0
            ):
                kwargs[
                    "reply_to_message_id"
                ] = reply_to_message_id

            await bot.send_photo(
                **kwargs
            )

        except Exception as e:
            print(
                "PRODUCT PHOTO XATOSI:",
                repr(e)
            )

            if index < len(raw_urls):
                kwargs = {
                    "chat_id": chat_id,
                    "photo": raw_urls[index],
                }

                if index == 0:
                    kwargs["caption"] = caption
                    kwargs["parse_mode"] = "HTML"

                if (
                    reply_to_message_id is not None
                    and index == 0
                ):
                    kwargs[
                        "reply_to_message_id"
                    ] = reply_to_message_id

                await bot.send_photo(
                    **kwargs
                )
            else:
                raise


async def send_products_to_message(
    bot,
    message,
    products
):
    if not products:
        return

    stats["catalog_matches"] += len(products)

    for product in products:
        await send_product_to_chat(
            bot,
            message.chat_id,
            product,
            reply_to_message_id=message.message_id
        )


# =========================
# ADMIN: /ADDPRODUCT
# =========================

async def add_product_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    admin_states[user.id] = {
        "mode": "add",
        "step": "photos",
        "pending_images": [],
        "image_bytes_list": [],
        "pending_raw_urls": [],
    }

    await update.message.reply_text(
        "➕ <b>Yangi mahsulot qo'shish</b>\n\n"
        "📸 Mahsulotning 1 yoki bir nechta "
        "rasmini yuboring.\n\n"
        "Rasmlar tugagach:\n"
        "<code>/done</code>\n"
        "ni bosing.\n\n"
        "Bekor qilish: <code>/cancel</code>",
        parse_mode="HTML"
    )


# =========================
# ADMIN: /PRODUCTS
# =========================

async def products_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    products = github_get_products()

    if not products:
        await update.message.reply_text(
            "📦 Katalog bo'sh."
        )
        return

    lines = [
        "📦 <b>MAHSULOTLAR KATALOGI</b>\n"
    ]

    for index, product in enumerate(
        products[:30],
        start=1
    ):
        status = (
            "✅"
            if visible_product(product)
            else "🚫"
        )

        category = product.get(
            "category",
            ""
        )

        category_text = (
            f" | {category}"
            if category
            else ""
        )

        lines.append(
            f"{index}. {status} "
            f"<b>{product.get('name', 'Nomsiz')}</b>"
            f"{category_text}"
        )

    if len(products) > 30:
        lines.append(
            f"\n… yana {len(products) - 30} ta mahsulot."
        )

    lines.append(
        f"\n<b>Jami:</b> {len(products)} ta"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================
# ADMIN: /EDITPRODUCT
# =========================

async def edit_product_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    products = github_get_products()

    if not products:
        await update.message.reply_text(
            "📦 Katalogda mahsulotlar yo'q."
        )
        return

    keyboard = []

    for product in products[:50]:
        status = (
            "✅"
            if visible_product(product)
            else "🚫"
        )

        label = (
            f"{status} "
            f"{product.get('name', 'Nomsiz')}"
        )[:60]

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=(
                    f"edit:{product['id']}"
                )
            )
        ])

    await update.message.reply_text(
        "✏️ <b>Qaysi mahsulotni "
        "tahrirlamoqchisiz?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================
# ADMIN: /DELETEPRODUCT
# =========================

async def delete_product_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    products = github_get_products()

    if not products:
        await update.message.reply_text(
            "📦 Katalogda mahsulotlar yo'q."
        )
        return

    keyboard = []

    for product in products[:50]:
        label = (
            f"🗑 {product.get('name', 'Nomsiz')}"
        )[:60]

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=(
                    f"delconfirm:{product['id']}"
                )
            )
        ])

    await update.message.reply_text(
        "🗑 <b>O'chirish uchun "
        "mahsulotni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================
# ADMIN: /CATEGORIES
# =========================

async def categories_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    products = github_get_products()

    counts = {}

    for product in products:
        category = (
            product.get(
                "category",
                ""
            ).strip()
            or "Kategoriyasiz"
        )

        counts[category] = (
            counts.get(
                category,
                0
            ) + 1
        )

    if not counts:
        await update.message.reply_text(
            "🗂 Kategoriyalar hali yo'q."
        )
        return

    lines = [
        "🗂 <b>KATEGORIYALAR</b>\n"
    ]

    for category, count in sorted(
        counts.items(),
        key=lambda x: x[0].lower()
    ):
        lines.append(
            f"• {category} — {count} ta"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


# =========================
# ADMIN: /STATS
# =========================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan "
            "foydalanish huquqi yo'q."
        )
        return

    products = github_get_products()

    visible_count = sum(
        1
        for product in products
        if visible_product(product)
    )

    hidden_count = (
        len(products)
        - visible_count
    )

    await update.message.reply_text(
        "📊 <b>AKSO BOT STATISTIKASI</b>\n\n"
        f"📦 Jami mahsulotlar: "
        f"<b>{len(products)}</b>\n"
        f"👁 Ko'rinadigan: "
        f"<b>{visible_count}</b>\n"
        f"🚫 Yashirilgan: "
        f"<b>{hidden_count}</b>\n\n"
        f"💬 Xabarlar "
        f"(bot restartidan beri): "
        f"<b>{stats['messages']}</b>\n"
        f"🔎 Katalog so'rovlari: "
        f"<b>{stats['catalog_queries']}</b>\n"
        f"🎯 Topilgan mahsulotlar: "
        f"<b>{stats['catalog_matches']}</b>\n\n"
        f"➕ Qo'shilgan: "
        f"<b>{stats['products_added']}</b>\n"
        f"✏️ Tahrirlangan: "
        f"<b>{stats['products_edited']}</b>\n"
        f"🚫 Yashirilgan: "
        f"<b>{stats['products_hidden']}</b>\n"
        f"🗑 O'chirilgan: "
        f"<b>{stats['products_deleted']}</b>",
        parse_mode="HTML"
    )


# =========================
# ADMIN: /DONE
# =========================

async def done_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        return

    state = admin_states.get(user.id)

    if not state:
        await update.message.reply_text(
            "ℹ️ Hozir mahsulot qo'shish "
            "yoki tahrirlash jarayoni yo'q."
        )
        return

    step = state.get("step")

    # -------------------------
    # ADD PHOTOS -> NAME
    # -------------------------

    if step == "photos":

        if not state.get("pending_images"):
            await update.message.reply_text(
                "📸 Kamida 1 ta rasm yuboring."
            )
            return

        state["step"] = "name"

        await update.message.reply_text(
            "✅ Rasmlar qabul qilindi.\n\n"
            "📝 Endi mahsulot nomini yozing.\n\n"
            "Masalan:\n"
            "<b>Immer Kuller 10bsb</b>",
            parse_mode="HTML"
        )

        return

    # -------------------------
    # EDIT IMAGES -> SAVE
    # -------------------------

    if step == "edit_images":

        if not state.get("pending_images"):
            await update.message.reply_text(
                "📸 Kamida 1 ta rasm yuboring."
            )
            return

        product_id = state.get(
            "product_id"
        )

        products = github_get_products()

        target = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not target:
            admin_states.pop(
                user.id,
                None
            )

            await update.message.reply_text(
                "❌ Mahsulot topilmadi."
            )
            return

        target["images"] = state[
            "pending_images"
        ]

        target["telegram_file_id"] = (
            state["pending_images"][0]
        )

        target["raw_urls"] = state.get(
            "pending_raw_urls",
            []
        )

        if target["raw_urls"]:
            target["raw_url"] = (
                target["raw_urls"][0]
            )

        try:
            github_save_products(
                products
            )

            stats[
                "products_edited"
            ] += 1

            admin_states.pop(
                user.id,
                None
            )

            await update.message.reply_text(
                "✅ Mahsulot rasmlari "
                "muvaffaqiyatli almashtirildi."
            )

        except Exception as e:
            print(
                "EDIT IMAGES XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Rasmlarni saqlashda "
                "xatolik yuz berdi."
            )

        return

    await update.message.reply_text(
        "ℹ️ /done hozirgi bosqichda "
        "ishlamaydi."
    )


# =========================
# ADMIN: /SKIP
# =========================

async def skip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        return

    state = admin_states.get(user.id)

    if not state:
        return

    step = state.get("step")

    if step == "category":
        state["category"] = ""
        state["step"] = "description"

        await update.message.reply_text(
            "✅ Kategoriya o'tkazib yuborildi.\n\n"
            "📄 Mahsulot tavsifini yozing.\n"
            "Kerak bo'lmasa <code>/skip</code> bosing.",
            parse_mode="HTML"
        )
        return

    if step == "description":
        await save_new_product(
            update,
            context,
            state
        )
        return

    await update.message.reply_text(
        "ℹ️ /skip hozirgi bosqichda ishlamaydi."
    )


# =========================
# ADMIN: SAVE NEW PRODUCT
# =========================

async def save_new_product(
    update,
    context,
    state
):
    if not update.message:
        return

    await update.message.reply_text(
        "⏳ Mahsulot GitHub'ga "
        "saqlanmoqda..."
    )

    try:
        name = state["name"]
        price = int(state["price"])

        image_bytes_list = state[
            "image_bytes_list"
        ]

        telegram_ids = state[
            "pending_images"
        ]

        category = state.get(
            "category",
            ""
        )

        description = state.get(
            "description",
            ""
        )

        raw_urls = []

        for index, image_bytes in enumerate(
            image_bytes_list,
            start=1
        ):
            filename = (
                f"{slugify(name)}_"
                f"{int(time.time() * 1000)}_"
                f"{index}.jpg"
            )

            github_path = (
                f"{PRODUCTS_FOLDER}/"
                f"{filename}"
            )

            github_upload_file(
                github_path,
                image_bytes,
                f"Add product image: {name}"
            )

            raw_urls.append(
                "https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/"
                f"{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/"
                f"{github_path}"
            )

        products = github_get_products()

        product = {
            "id": str(
                int(
                    time.time() * 1000
                )
            ),
            "name": name,
            "price": price,
            "category": category,
            "description": description,
            "telegram_file_id": telegram_ids[0],
            "images": telegram_ids,
            "raw_url": (
                raw_urls[0]
                if raw_urls
                else ""
            ),
            "raw_urls": raw_urls,
            "github_path": (
                f"{PRODUCTS_FOLDER}/"
                f"{slugify(name)}"
            ),
            "keywords": [],
            "visible": True,
            "created_at": (
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            ),
        }

        refresh_product_keywords(
            product
        )

        products.append(
            product
        )

        github_save_products(
            products
        )

        admin_states.pop(
            update.message.from_user.id,
            None
        )

        stats[
            "products_added"
        ] += 1

        month_3, month_6, month_12 = (
            calculate_monthly(
                price
            )
        )

        await update.message.reply_text(
            "✅ <b>Mahsulot muvaffaqiyatli saqlandi!</b>\n\n"
            f"🛍 <b>{name}</b>\n"
            f"🗂 Kategoriya: "
            f"<b>{category or 'Kategoriyasiz'}</b>\n"
            f"💵 Naqd: "
            f"<b>{format_money(price)}</b>\n\n"
            f"📅 3 oy — "
            f"<b>{format_money(month_3)}/oy</b>\n"
            f"📅 6 oy — "
            f"<b>{format_money(month_6)}/oy</b>\n"
            f"📅 12 oy — "
            f"<b>{format_money(month_12)}/oy</b>\n\n"
            f"📸 Rasmlar: "
            f"<b>{len(telegram_ids)} ta</b>\n"
            "📦 Ma'lumotlar GitHub'ga saqlandi.",
            parse_mode="HTML"
        )

    except Exception as e:
        print(
            "ADD PRODUCT XATOSI:",
            repr(e)
        )

        await update.message.reply_text(
            "❌ Mahsulotni saqlashda "
            "xatolik yuz berdi.\n\n"
            "Render → Logs bo'limidan "
            "xatoni tekshiramiz."
        )

        admin_states.pop(
            update.message.from_user.id,
            None
        )


# =========================
# ADMIN STATE HANDLER
# =========================

async def handle_admin_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return False

    user = update.message.from_user

    if not user or not is_admin(user.id):
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
    # ADD: PHOTOS
    # -------------------------

    if step == "photos":

        if not update.message.photo:
            await update.message.reply_text(
                "📸 Rasm yuboring.\n"
                "Bitta yoki bir nechta rasm "
                "yuborishingiz mumkin.\n\n"
                "Tugagach /done bosing."
            )
            return True

        photo = (
            update.message.photo[-1]
        )

        try:
            telegram_file = (
                await context.bot.get_file(
                    photo.file_id
                )
            )

            image_bytes = (
                await telegram_file.download_as_bytearray()
            )

            state[
                "pending_images"
            ].append(
                photo.file_id
            )

            state.setdefault(
                "image_bytes_list",
                []
            ).append(
                bytes(image_bytes)
            )

            await update.message.reply_text(
                f"✅ {len(state['pending_images'])}-rasm qabul qilindi.\n\n"
                "Yana rasm yuboring yoki "
                "<code>/done</code> bosing.",
                parse_mode="HTML"
            )

        except Exception as e:
            print(
                "ADMIN PHOTO XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Rasmni qabul qilishda "
                "xatolik yuz berdi."
            )

        return True

    # -------------------------
    # ADD: NAME
    # -------------------------

    if step == "name":

        if not update.message.text:
            await update.message.reply_text(
                "📝 Mahsulot nomini yozing."
            )
            return True

        name = (
            update.message.text.strip()
        )

        if not name:
            await update.message.reply_text(
                "❌ Nom bo'sh bo'lishi mumkin emas."
            )
            return True

        state[
            "name"
        ] = name

        state[
            "step"
        ] = "price"

        await update.message.reply_text(
            "✅ Mahsulot nomi saqlandi.\n\n"
            "💰 Endi naqd narxni yozing.\n\n"
            "Masalan:\n"
            "<b>1500000</b>",
            parse_mode="HTML"
        )

        return True

    # -------------------------
    # ADD: PRICE
    # -------------------------

    if step == "price":

        if not update.message.text:
            await update.message.reply_text(
                "💰 Narxni raqam bilan yozing."
            )
            return True

        price = parse_price(
            update.message.text
        )

        if price is None:
            await update.message.reply_text(
                "❌ Narxni tushuna olmadim.\n"
                "Masalan: 1500000"
            )
            return True

        state[
            "price"
        ] = price

        state[
            "step"
        ] = "category"

        await update.message.reply_text(
            "✅ Narx saqlandi.\n\n"
            "🗂 Endi kategoriya nomini yozing.\n\n"
            "Masalan:\n"
            "<b>Kullerlar</b>\n\n"
            "Kategoriya kerak bo'lmasa:\n"
            "<code>/skip</code>",
            parse_mode="HTML"
        )

        return True

    # -------------------------
    # ADD: CATEGORY
    # -------------------------

    if step == "category":

        if not update.message.text:
            await update.message.reply_text(
                "🗂 Kategoriya nomini yozing yoki /skip bosing."
            )
            return True

        category = (
            update.message.text.strip()
        )

        if not category:
            await update.message.reply_text(
                "🗂 Kategoriya nomini yozing yoki /skip bosing."
            )
            return True

        state[
            "category"
        ] = category

        state[
            "step"
        ] = "description"

        await update.message.reply_text(
            "✅ Kategoriya saqlandi.\n\n"
            "📄 Endi mahsulot haqida qisqa tavsif yozing.\n\n"
            "Masalan:\n"
            "<b>Suvni sovutadi va isitadi.</b>\n\n"
            "Tavsif kerak bo'lmasa:\n"
            "<code>/skip</code>",
            parse_mode="HTML"
        )

        return True

    # -------------------------
    # ADD: DESCRIPTION
    # -------------------------

    if step == "description":

        if not update.message.text:
            await update.message.reply_text(
                "📄 Tavsifni yozing yoki /skip bosing."
            )
            return True

        state[
            "description"
        ] = (
            update.message.text.strip()
        )

        await save_new_product(
            update,
            context,
            state
        )

        return True

    # -------------------------
    # EDIT: TEXT
    # -------------------------

    if step in {
        "edit_name",
        "edit_price",
        "edit_category",
        "edit_description",
    }:

        if not update.message.text:
            await update.message.reply_text(
                "📝 Iltimos, matn yuboring."
            )
            return True

        product_id = state.get(
            "product_id"
        )

        field = state.get(
            "field"
        )

        products = github_get_products()

        target = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not target:
            admin_states.pop(
                user.id,
                None
            )

            await update.message.reply_text(
                "❌ Mahsulot topilmadi."
            )

            return True

        text = (
            update.message.text.strip()
        )

        if field == "name":

            if not text:
                await update.message.reply_text(
                    "❌ Nom bo'sh bo'lishi mumkin emas."
                )
                return True

            target[
                "name"
            ] = text

        elif field == "price":

            price = parse_price(
                text
            )

            if price is None:
                await update.message.reply_text(
                    "❌ Narx noto'g'ri.\n"
                    "Masalan: 1500000"
                )
                return True

            target[
                "price"
            ] = price

        elif field == "category":

            target[
                "category"
            ] = text

        elif field == "description":

            target[
                "description"
            ] = text

        refresh_product_keywords(
            target
        )

        try:
            github_save_products(
                products
            )

            stats[
                "products_edited"
            ] += 1

            admin_states.pop(
                user.id,
                None
            )

            await update.message.reply_text(
                "✅ Mahsulot muvaffaqiyatli "
                "tahrirlandi."
            )

        except Exception as e:
            print(
                "EDIT PRODUCT XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ O'zgarishni saqlashda "
                "xatolik yuz berdi."
            )

        return True

    # -------------------------
    # EDIT: IMAGES
    # -------------------------

    if step == "edit_images":

        if not update.message.photo:
            await update.message.reply_text(
                "📸 Rasm yuboring.\n"
                "Tugagach /done bosing."
            )
            return True

        photo = (
            update.message.photo[-1]
        )

        try:
            telegram_file = (
                await context.bot.get_file(
                    photo.file_id
                )
            )

            image_bytes = (
                await telegram_file.download_as_bytearray()
            )

            state[
                "pending_images"
            ].append(
                photo.file_id
            )

            # GitHub'ga darhol yuklaymiz.
            name = state.get(
                "name",
                "product"
            )

            index = len(
                state["pending_images"]
            )

            filename = (
                f"{slugify(name)}_edited_"
                f"{int(time.time() * 1000)}_"
                f"{index}.jpg"
            )

            github_path = (
                f"{PRODUCTS_FOLDER}/"
                f"{filename}"
            )

            github_upload_file(
                github_path,
                bytes(image_bytes),
                f"Add edited product image: {name}"
            )

            state.setdefault(
                "pending_raw_urls",
                []
            )

            state[
                "pending_raw_urls"
            ].append(
                "https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/"
                f"{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/"
                f"{github_path}"
            )

            await update.message.reply_text(
                f"✅ {index}-rasm qabul qilindi.\n\n"
                "Yana rasm yuboring yoki "
                "<code>/done</code> bosing.",
                parse_mode="HTML"
            )

        except Exception as e:
            print(
                "EDIT PHOTO XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Rasmni saqlashda "
                "xatolik yuz berdi."
            )

        return True

    return False


# =========================
# CALLBACK HANDLER
# =========================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    # Admin callback'larini himoyalaymiz.
    if data.startswith(
        (
            "edit:",
            "edfield:",
            "toggle:",
            "delconfirm:",
            "del:",
        )
    ):

        if not is_admin(
            query.from_user.id
        ):
            await query.answer(
                "❌ Sizda ruxsat yo'q.",
                show_alert=True
            )
            return

    await query.answer()

    # -------------------------
    # EDIT PRODUCT
    # -------------------------

    if data.startswith("edit:"):

        product_id = data.split(
            ":",
            1
        )[1]

        products = github_get_products()

        product = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not product:
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "📝 Nomini o'zgartirish",
                    callback_data=(
                        f"edfield:{product_id}:name"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 Narxini o'zgartirish",
                    callback_data=(
                        f"edfield:{product_id}:price"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "🗂 Kategoriyasini o'zgartirish",
                    callback_data=(
                        f"edfield:{product_id}:category"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "📄 Tavsifini o'zgartirish",
                    callback_data=(
                        f"edfield:{product_id}:description"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "📸 Rasmlarini almashtirish",
                    callback_data=(
                        f"edfield:{product_id}:images"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "🚫 Mahsulotni yashirish"
                        if visible_product(product)
                        else "👁 Mahsulotni ko'rsatish"
                    ),
                    callback_data=(
                        f"toggle:{product_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 O'chirish",
                    callback_data=(
                        f"delconfirm:{product_id}"
                    )
                )
            ],
        ]

        await query.edit_message_text(
            (
                f"✏️ <b>{product.get('name', '')}</b>\n\n"
                "Kerakli amalni tanlang:"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    # -------------------------
    # EDIT FIELD
    # -------------------------

    if data.startswith("edfield:"):

        parts = data.split(":")

        if len(parts) != 3:
            return

        product_id = parts[1]
        field = parts[2]

        products = github_get_products()

        product = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not product:
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        if field == "images":

            admin_states[
                query.from_user.id
            ] = {
                "mode": "edit",
                "step": "edit_images",
                "product_id": product_id,
                "name": product.get(
                    "name",
                    "product"
                ),
                "pending_images": [],
                "pending_raw_urls": [],
            }

            await query.message.reply_text(
                "📸 Yangi rasmlarni yuboring.\n\n"
                "Bu rasmlar eski rasmlarning "
                "o'rnini egallaydi.\n\n"
                "Tugagach <code>/done</code> bosing.\n"
                "Bekor qilish: <code>/cancel</code>",
                parse_mode="HTML"
            )

            return

        prompt_map = {
            "name": (
                "📝 Yangi mahsulot nomini yozing:"
            ),
            "price": (
                "💰 Yangi naqd narxni yozing:"
            ),
            "category": (
                "🗂 Yangi kategoriyani yozing:"
            ),
            "description": (
                "📄 Yangi tavsifni yozing:"
            ),
        }

        prompt = prompt_map.get(
            field
        )

        if not prompt:
            return

        admin_states[
            query.from_user.id
        ] = {
            "mode": "edit",
            "step": f"edit_{field}",
            "product_id": product_id,
            "field": field,
        }

        await query.message.reply_text(
            prompt
        )

        return

    # -------------------------
    # TOGGLE VISIBILITY
    # -------------------------

    if data.startswith("toggle:"):

        product_id = data.split(
            ":",
            1
        )[1]

        products = github_get_products()

        product = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not product:
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        old_visible = (
            visible_product(product)
        )

        product["visible"] = not old_visible

        try:
            github_save_products(
                products
            )

            if old_visible:
                stats[
                    "products_hidden"
                ] += 1

                message = (
                    "🚫 Mahsulot yashirildi."
                )

            else:
                message = (
                    "👁 Mahsulot yana ko'rsatildi."
                )

            await query.edit_message_text(
                (
                    f"✅ <b>{product.get('name', '')}</b>\n\n"
                    f"{message}"
                ),
                parse_mode="HTML"
            )

        except Exception as e:
            print(
                "TOGGLE XATOSI:",
                repr(e)
            )

            await query.edit_message_text(
                "❌ O'zgarishni saqlashda "
                "xatolik yuz berdi."
            )

        return

    # -------------------------
    # DELETE CONFIRM
    # -------------------------

    if data.startswith(
        "delconfirm:"
    ):

        product_id = data.split(
            ":",
            1
        )[1]

        products = github_get_products()

        product = next(
            (
                p
                for p in products
                if p.get("id") == product_id
            ),
            None
        )

        if not product:
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Ha, o'chirish",
                    callback_data=(
                        f"del:{product_id}"
                    )
                ),
                InlineKeyboardButton(
                    "❌ Yo'q",
                    callback_data=(
                        f"edit:{product_id}"
                    )
                ),
            ]
        ]

        await query.edit_message_text(
            (
                "⚠️ <b>Haqiqatan "
                "o'chirmoqchimisiz?</b>\n\n"
                f"🛍 {product.get('name', '')}\n\n"
                "Mahsulot katalogdan olib tashlanadi."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    # -------------------------
    # DELETE
    # -------------------------

    if data.startswith("del:"):

        product_id = data.split(
            ":",
            1
        )[1]

        products = github_get_products()

        new_products = [
            p
            for p in products
            if p.get("id") != product_id
        ]

        if len(new_products) == len(
            products
        ):
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        try:
            await query.edit_message_text(
                "⏳ Mahsulot o'chirilmoqda..."
            )

            github_save_products(
                new_products
            )

            stats[
                "products_deleted"
            ] += 1

            await query.edit_message_text(
                "✅ Mahsulot katalogdan "
                "o'chirildi."
            )

        except Exception as e:
            print(
                "DELETE XATOSI:",
                repr(e)
            )

            await query.edit_message_text(
                "❌ O'chirishda xatolik yuz berdi."
            )

        return

    # -------------------------
    # CATALOG CATEGORY
    # -------------------------

    if data.startswith("cat:"):

        try:
            index = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except ValueError:
            return

        products = [
            p
            for p in github_get_products()
            if visible_product(p)
        ]

        categories = {}

        for product in products:
            category = (
                product.get(
                    "category",
                    ""
                ).strip()
                or "Kategoriyasiz"
            )

            categories.setdefault(
                category,
                []
            ).append(product)

        sorted_categories = sorted(
            categories.keys(),
            key=lambda x: x.lower()
        )

        if (
            index < 0
            or index >= len(
                sorted_categories
            )
        ):
            return

        category = sorted_categories[
            index
        ]

        selected = categories[
            category
        ][:10]

        await query.message.reply_text(
            (
                f"🗂 <b>{category}</b>\n"
                f"📦 {len(categories[category])} ta mahsulot."
            ),
            parse_mode="HTML"
        )

        for product in selected:
            await send_product_to_chat(
                context.bot,
                query.message.chat_id,
                product
            )

        return


# =========================
# /CATALOG
# =========================

async def catalog_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    products = [
        p
        for p in github_get_products()
        if visible_product(p)
    ]

    if not products:
        await update.message.reply_text(
            "📦 Hozircha katalogda mahsulot yo'q."
        )
        return

    categories = {}

    for product in products:
        category = (
            product.get(
                "category",
                ""
            ).strip()
            or "Kategoriyasiz"
        )

        categories.setdefault(
            category,
            0
        )

        categories[category] += 1

    keyboard = []

    sorted_categories = sorted(
        categories.keys(),
        key=lambda x: x.lower()
    )

    for index, category in enumerate(
        sorted_categories
    ):
        keyboard.append([
            InlineKeyboardButton(
                (
                    f"🗂 {category} "
                    f"({categories[category]})"
                ),
                callback_data=(
                    f"cat:{index}"
                )
            )
        ])

    await update.message.reply_text(
        "🛍 <b>Mahsulot katalogi</b>\n\n"
        "Kategoriya tanlang:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================
# /HELP
# =========================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ <b>AKSO AI yordam</b>\n\n"
        "Mahsulotni oddiy tilda so'rang.\n\n"
        "Masalan:\n"
        "• Kuller bormi?\n"
        "• Shkaf bormi?\n"
        "• Detski shkaflar kerak\n"
        "• Kiyim osadigan mebel bormi?\n"
        "• Stol kerak\n\n"
        "Men katalogdan mos mahsulotlarni "
        "topib, rasmlari va bo'lib to'lash "
        "oylik to'lovlarini yuboraman.",
        parse_mode="HTML"
    )


# =========================
# /START
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
        "Mahsulot nomini yoki sizga kerakli "
        "mahsulotni oddiy tilda yozishingiz mumkin. 🤖",
        parse_mode="HTML"
    )


# =========================
# /ID
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
        f"<code>{user.id}</code>",
        parse_mode="HTML"
    )


# =========================
# /CANCEL
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

    if is_admin(user.id):
        admin_states.pop(
            user.id,
            None
        )

        await update.message.reply_text(
            "❌ Joriy amal bekor qilindi."
        )


# =========================
# ASOSIY AI + MAHSULOT
# =========================

async def reply_to_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    if (
        update.message.from_user
        and
        update.message.from_user.is_bot
    ):
        return

    stats["messages"] += 1

    handled = await handle_admin_state(
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

            answer = (
                response.text
            )

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

    # -------------------------
    # ODDIY MATN
    # -------------------------

    if not update.message.text:
        return

    user_text = (
        update.message.text.strip()
    )

    if not user_text:
        return

    # -------------------------
    # MAHSULOT KATALOGI
    # -------------------------

    try:
        products = github_get_products()

        if products:
            stats[
                "catalog_queries"
            ] += 1

            local_products = (
                find_local_products(
                    user_text,
                    products
                )
            )

            if local_products:
                await send_products_to_message(
                    context.bot,
                    update.message,
                    local_products
                )
                return

            ai_products = (
                await find_ai_products(
                    user_text,
                    products
                )
            )

            if ai_products:
                await send_products_to_message(
                    context.bot,
                    update.message,
                    ai_products
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
- O'zbek tilida yozilsa,
  o'zbek tilida javob ber.
- Rus tilida yozilsa,
  rus tilida javob ber.
- Ingliz tilida yozilsa,
  ingliz tilida javob ber.
- Javobni tushunarli va foydali qil.
- Keraksiz uzun javob bermagin.
- Oddiy savolga oddiy va aniq
  javob ber.
- Salomlashishga odob bilan
  javob ber.
- Foydalanuvchi xabarini
  qayta takrorlama.

Foydalanuvchi xabari:
{user_text}
"""

        response = (
            await ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt
            )
        )

        answer = (
            response.text
        )

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
# COMMAND MENUS
# =========================

async def setup_command_menus(
    application
):
    # Avval eski default va admin-chat komandalarini
    # tozalaymiz. Bu Telegram klientida eski ro'yxat
    # qolib ketishining oldini oladi.
    await application.bot.delete_my_commands()

    await application.bot.delete_my_commands(
        scope=BotCommandScopeChat(
            chat_id=ADMIN_ID
        )
    )

    # Oddiy foydalanuvchi uchun umumiy menyu.
    user_commands = [
        BotCommand(
            "start",
            "🤖 Botni ishga tushirish"
        ),
        BotCommand(
            "catalog",
            "🛍 Mahsulot katalogi"
        ),
        BotCommand(
            "help",
            "❓ Yordam"
        ),
    ]

    await application.bot.set_my_commands(
        user_commands
    )

    # Admin uchun alohida menyu.
    admin_commands = [
        BotCommand(
            "start",
            "🤖 Botni ishga tushirish"
        ),
        BotCommand(
            "catalog",
            "🛍 Mahsulot katalogi"
        ),
        BotCommand(
            "help",
            "❓ Yordam"
        ),
        BotCommand(
            "id",
            "🆔 Telegram ID"
        ),
        BotCommand(
            "addproduct",
            "➕ Mahsulot qo'shish"
        ),
        BotCommand(
            "products",
            "📦 Mahsulotlar"
        ),
        BotCommand(
            "editproduct",
            "✏️ Mahsulotni tahrirlash"
        ),
        BotCommand(
            "deleteproduct",
            "🗑 Mahsulotni o'chirish"
        ),
        BotCommand(
            "categories",
            "🗂 Kategoriyalar"
        ),
        BotCommand(
            "stats",
            "📊 Statistika"
        ),
        BotCommand(
            "done",
            "✅ Rasm kiritishni tugatish"
        ),
        BotCommand(
            "skip",
            "⏭ Bosqichni o'tkazish"
        ),
        BotCommand(
            "cancel",
            "❌ Amalni bekor qilish"
        ),
    ]

    await application.bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(
            chat_id=ADMIN_ID
        )
    )

    # O'rnatilgan admin menyusini tekshirish.
    saved_commands = await application.bot.get_my_commands(
        scope=BotCommandScopeChat(
            chat_id=ADMIN_ID
        )
    )

    print(
        "✅ Telegram command menus o'rnatildi."
    )

    print(
        "✅ Admin commands:",
        [command.command for command in saved_commands]
    )


# =========================
# APPLICATION
# =========================

telegram_app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .post_init(setup_command_menus)
    .build()
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
        "catalog",
        catalog_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "help",
        help_command
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
        "products",
        products_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "editproduct",
        edit_product_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "deleteproduct",
        delete_product_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "categories",
        categories_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "stats",
        stats_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "done",
        done_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "skip",
        skip_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "cancel",
        cancel_command
    )
)


# =========================
# CALLBACK HANDLER
# =========================

telegram_app.add_handler(
    CallbackQueryHandler(
        callback_handler
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
        )
        & ~filters.COMMAND,
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
        webhook_url=(
            f"{BASE_URL}/webhook"
        ),
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message",
            "callback_query",
        ],
        drop_pending_updates=True,
    )
