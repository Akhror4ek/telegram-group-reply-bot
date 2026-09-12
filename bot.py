import os
import json
import base64
import re
import time
import unicodedata
import asyncio
from difflib import SequenceMatcher
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "telegram-bot-secret")
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]


# ============================================================
# GITHUB
# ============================================================

GITHUB_OWNER = "Akhror4ek"
GITHUB_REPO = "telegram-group-reply-bot"
GITHUB_BRANCH = "main"
PRODUCTS_FILE = "products.json"
PRODUCTS_FOLDER = "products"


# ============================================================
# GEMINI
# ============================================================

ai_client = genai.Client(
    api_key=GEMINI_API_KEY
).aio


# ============================================================
# STATE
# ============================================================

admin_states = {}

# chat_id -> {user_id, products, created_at}
pending_product_confirmations = {}

products_cache = []
products_cache_time = 0.0
PRODUCT_CACHE_TTL = 600  # 10 daqiqa

stats = {
    "messages": 0,
    "catalog_queries": 0,
    "catalog_matches": 0,
    "products_added": 0,
    "products_edited": 0,
    "products_deleted": 0,
    "products_hidden": 0,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


def format_money(value):
    return f"{int(value):,}".replace(",", " ") + " so'm"


def calculate_monthly(price):
    return (
        round(price / 3),
        round((price * 1.18) / 6),
        round((price * 1.36) / 12),
    )


def normalize_text(text):
    text = (text or "").lower().strip()

    replacements = {
        "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
        "ё": "yo", "й": "y", "ю": "yu", "я": "ya",
        "ш": "sh", "ч": "ch", "ц": "ts", "щ": "shch",
        "ж": "j", "х": "x", "э": "e", "ъ": "", "ь": "",
        "ы": "y",
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
        "е": "e", "з": "z", "и": "i", "к": "k", "л": "l",
        "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f",
        "‘": "'", "’": "'", "`": "'", "ʻ": "'",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def stem_token(token):
    token = normalize_text(token)

    if len(token) <= 3:
        return token

    suffixes = (
        "larning", "larni", "lar",
        "ning", "dan", "dagi",
        "ga", "da", "ni", "mi",
    )

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
        word = stem_token(word)
        if len(word) >= 2:
            result.append(word)

    return result


def slugify(text):
    text = normalize_text(text).replace(" ", "_")
    return (text or "product")[:60]


def parse_price(text):
    digits = re.sub(r"\D", "", text or "")

    if not digits:
        return None

    try:
        value = int(digits)
    except ValueError:
        return None

    return value if value > 0 else None


def visible_product(product):
    return product.get("visible", True) is not False


def product_images(product):
    images = product.get("images")

    if isinstance(images, list) and images:
        return images

    file_id = product.get("telegram_file_id")
    return [file_id] if file_id else []


def refresh_product_keywords(product):
    source = " ".join(
        [
            product.get("name", ""),
            product.get("category", ""),
            product.get("description", ""),
        ]
    )

    keywords = []

    for word in normalize_text(source).split():
        word = stem_token(word)
        if len(word) >= 2 and word not in keywords:
            keywords.append(word)

    product["keywords"] = keywords


# ============================================================
# GITHUB API
# ============================================================

def _github_api_sync(method, path, data=None):
    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
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
            ensure_ascii=False,
        ).encode("utf-8")

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()

            if not raw:
                return {}

            return json.loads(
                raw.decode("utf-8")
            )

    except HTTPError as e:
        body = e.read().decode(
            "utf-8",
            errors="ignore",
        )

        raise Exception(
            f"GitHub API {e.code}: {body}"
        )


async def github_api(method, path, data=None):
    # GitHub internet so'rovi Telegram event loopini bloklamaydi.
    return await asyncio.to_thread(
        _github_api_sync,
        method,
        path,
        data,
    )


async def github_upload_file(
    path,
    file_bytes,
    commit_message,
):
    content = base64.b64encode(
        file_bytes
    ).decode("ascii")

    return await github_api(
        "PUT",
        path,
        {
            "message": commit_message,
            "content": content,
            "branch": GITHUB_BRANCH,
        },
    )


async def github_get_products(
    force_refresh=False
):
    global products_cache
    global products_cache_time

    now = time.time()

    if (
        not force_refresh
        and products_cache
        and now - products_cache_time < PRODUCT_CACHE_TTL
    ):
        return products_cache

    try:
        result = await github_api(
            "GET",
            f"{PRODUCTS_FILE}?ref={GITHUB_BRANCH}",
        )

        content = result.get(
            "content",
            ""
        )

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

        # Eski mahsulotlar bilan ham mos ishlaydi.
        for product in products:
            product.setdefault("visible", True)
            product.setdefault("category", "")
            product.setdefault("description", "")

            if "images" not in product:
                old = product.get("telegram_file_id")
                product["images"] = (
                    [old] if old else []
                )

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


async def github_save_products(products):
    global products_cache
    global products_cache_time

    content = json.dumps(
        products,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    encoded = base64.b64encode(
        content
    ).decode("ascii")

    sha = None

    try:
        current = await github_api(
            "GET",
            f"{PRODUCTS_FILE}?ref={GITHUB_BRANCH}",
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

    await github_api(
        "PUT",
        PRODUCTS_FILE,
        data,
    )

    products_cache = products
    products_cache_time = time.time()


# ============================================================
# SAFE PRODUCT INTENT DETECTION
# ============================================================

PRODUCT_INTENT_WORDS = (
    "bormi",
    "bormi?",
    "kerak",
    "qidiryapman",
    "qidiraman",
    "izlayapman",
    "korsat",
    "korsating",
    "topib ber",
    "bering",
    "sotuvda",
    "mavjud",
    "mahsulot",
    "mebel",
    "shkaf",
    "kuller",
    "kuler",
    "sandiq",
    "stol",
    "stul",
    "divan",
    "karavat",
    "yotoq",
    "oshxona",
    "spalni",
    "spalnya",
)


def likely_product_query(text):
    q = normalize_text(text)

    if not q:
        return False

    # Oddiy suhbatlar hech qachon katalogga yuborilmaydi.
    ordinary = {
        "salom",
        "assalomu alaykum",
        "rahmat",
        "raxmat",
        "ok",
        "okay",
        "ha",
        "yoq",
        "xayr",
        "mayli",
        "boladi",
        "tushunarli",
        "juda yaxshi",
        "juda zor",
        "yaxshi",
        "zor",
    }

    if q in ordinary:
        return False

    if any(
        word in q
        for word in PRODUCT_INTENT_WORDS
    ):
        return True

    # Yakka so'z faqat keyingi qat'iy fuzzy tekshiruv uchun o'tadi.
    # "juda", "yaxshi" kabi so'zlar keyin mahsulotga moslashtirilmaydi.
    return len(q.split()) == 1 and len(q) >= 4


# ============================================================
# STRICT LOCAL SEARCH
# ============================================================

def local_product_score(user_text, product):
    q = normalize_text(user_text)
    q_tokens = set(token_list(user_text))

    if not q_tokens:
        return 0

    searchable_text = " ".join(
        [
            product.get("name", ""),
            product.get("category", ""),
            product.get("description", ""),
            " ".join(product.get("keywords", [])),
        ]
    )

    searchable_tokens = set(
        token_list(searchable_text)
    )

    if not searchable_tokens:
        return 0

    score = 0

    name = normalize_text(
        product.get("name", "")
    )

    # To'liq nom juda kuchli signal.
    if name and name == q:
        score += 200

    if name and name in q:
        score += 120

    # To'liq token mosligi.
    overlap = q_tokens.intersection(
        searchable_tokens
    )

    score += len(overlap) * 80

    # Fuzzy faqat uzunroq so'zlarda va ancha yuqori thresholdda.
    for qt in q_tokens:
        if len(qt) < 4:
            continue

        best = 0.0

        for st in searchable_tokens:
            if len(st) < 4:
                continue

            similarity = SequenceMatcher(
                None,
                qt,
                st
            ).ratio()

            if similarity > best:
                best = similarity

        if best >= 0.94:
            score += 70
        elif best >= 0.90:
            score += 45

    return score


def find_local_products(
    user_text,
    products
):
    scored = []

    for product in products:
        if not visible_product(product):
            continue

        score = local_product_score(
            user_text,
            product
        )

        # Juda ehtiyotkor threshold:
        # tasodifiy so'z mahsulotga aylanib ketmasin.
        if score >= 80:
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
        for _, product in scored[:5]
    ]


# ============================================================
# AI SEMANTIC SEARCH
# ============================================================

async def find_ai_products(
    user_text,
    products
):
    visible = [
        p
        for p in products
        if visible_product(p)
    ]

    if not visible:
        return []

    # Lokal natijalar eng yuqori bo'lsin.
    ranked = []

    for product in visible:
        ranked.append(
            (
                local_product_score(
                    user_text,
                    product
                ),
                product
            )
        )

    ranked.sort(
        key=lambda item: item[0],
        reverse=True
    )

    candidates = [
        product
        for _, product in ranked[:80]
    ]

    if len(visible) <= 80:
        candidates = visible

    catalog_lines = []

    for index, product in enumerate(
        candidates
    ):
        catalog_lines.append(
            f"{index}: "
            f"{product.get('name', '')} | "
            f"kategoriya: {product.get('category', '')} | "
            f"tavsif: {product.get('description', '')} | "
            f"kalitlar: "
            f"{', '.join(product.get('keywords', []))}"
        )

    prompt = f"""
Sen AKSO SAVDO katalogidagi mahsulotlarni
mijoz so'roviga mazmunan moslashtiruvchi
aniq katalog yordamchisisan.

Mijoz so'rovi:
{user_text}

Katalog nomzodlari:
{chr(10).join(catalog_lines)}

Muhim qoidalar:

1. Mijoz mahsulot nomini xato yozishi mumkin:
   "kuler" -> "kuller".

2. O'zbekcha, ruscha va transliteratsiya
   variantlarini tushun.

3. Sinonim va vazifani tushun:
   "kiyim osadigan mebel" -> shkaf.

4. "detski shkaf" -> bolalar shkafi.

5. "shkaf bormi?" -> katalogdagi shkaflar.

6. Mijoz oddiy suhbat qilsa, mahsulot tanlama.
   Masalan:
   "juda yaxshi"
   "rahmat"
   "qalay"
   "zo'r"

7. Mijoz faqat mahsulot borligini so'rasa,
   mos katalog mahsulotini tanla.

8. Eng yaqin 1-5 ta REAL katalog mahsulotini tanla.

9. Faqat juda katta ehtimol bilan mos bo'lgan
   mahsulotlarni qaytar.

10. Shubha bo'lsa, hech narsa tanlama.

Faqat JSON:
[0, 2]

Mos kelmasa:
[]
"""

    try:
        response = await ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )

        result = (
            response.text
            or ""
        ).strip()

        match = re.search(
            r"\[[^\]]*\]",
            result,
            flags=re.DOTALL,
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
                ValueError,
            ):
                continue

            if 0 <= index < len(candidates):
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


# ============================================================
# CONFIRMATION
# ============================================================

def save_pending_confirmation(
    message,
    user_id,
    products,
):
    pending_product_confirmations[
        message.chat_id
    ] = {
        "user_id": user_id,
        "products": products,
        "created_at": time.time(),
    }


def get_pending_confirmation(
    chat_id,
    user_id
):
    data = pending_product_confirmations.get(
        chat_id
    )

    if not data:
        return None

    if data.get("user_id") != user_id:
        return None

    # Tasdiqlash 10 daqiqa amal qiladi.
    if time.time() - data.get(
        "created_at",
        0
    ) > 600:
        pending_product_confirmations.pop(
            chat_id,
            None
        )
        return None

    return data


def clear_pending_confirmation(
    chat_id
):
    pending_product_confirmations.pop(
        chat_id,
        None
    )


async def ask_product_confirmation(
    message,
    user_id,
    products
):
    if not products:
        return

    save_pending_confirmation(
        message,
        user_id,
        products,
    )

    names = [
        product.get(
            "name",
            "Nomsiz"
        )
        for product in products
    ]

    if len(names) == 1:
        product_text = (
            f"<b>{names[0]}</b>"
        )
    else:
        product_text = (
            f"<b>{len(names)} ta mos mahsulot</b>"
        )

    text = (
        f"🔎 Siz so'ragan mahsulot bo'yicha "
        f"{product_text} topildi.\n\n"
        "📸 <b>Rasmlari va bo'lib to'lash "
        "narxlarini yuboraymi?</b>"
    )

    keyboard = [[
        InlineKeyboardButton(
            "✅ Ha, yuboring",
            callback_data="confirm_products",
        ),
        InlineKeyboardButton(
            "❌ Yo'q",
            callback_data="cancel_products",
        ),
    ]]

    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# SEND PRODUCT
# ============================================================

async def send_product_to_chat(
    bot,
    chat_id,
    product,
    reply_to_message_id=None,
):
    price = int(
        product["price"]
    )

    month_3, month_6, month_12 = calculate_monthly(
        price
    )

    parts = [
        f"🛍 <b>{product['name']}</b>"
    ]

    if product.get("category"):
        parts.append(
            f"🗂 {product['category']}"
        )

    if product.get("description"):
        parts.append(
            f"\n{product['description']}"
        )

    # Naqd narx ataylab ko'rsatilmaydi.
    parts.append(
        "\n📅 <b>Bo'lib to'lash:</b>\n"
        f"• 3 oy — <b>{format_money(month_3)}/oy</b>\n"
        f"• 6 oy — <b>{format_money(month_6)}/oy</b>\n"
        f"• 12 oy — <b>{format_money(month_12)}/oy</b>"
    )

    caption = "\n".join(parts)

    images = product_images(
        product
    )

    if not images:
        kwargs = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
        }

        if reply_to_message_id is not None:
            kwargs[
                "reply_to_message_id"
            ] = reply_to_message_id

        await bot.send_message(
            **kwargs
        )
        return

    raw_urls = product.get(
        "raw_urls",
        []
    )

    if not isinstance(raw_urls, list):
        raw_urls = []

    if not raw_urls and product.get("raw_url"):
        raw_urls = [
            product["raw_url"]
        ]

    for index, image in enumerate(
        images
    ):
        kwargs = {
            "chat_id": chat_id,
            "photo": image,
        }

        if index == 0:
            kwargs[
                "caption"
            ] = caption

            kwargs[
                "parse_mode"
            ] = "HTML"

            if reply_to_message_id is not None:
                kwargs[
                    "reply_to_message_id"
                ] = reply_to_message_id

        try:
            await bot.send_photo(
                **kwargs
            )

        except Exception as e:
            print(
                "PRODUCT PHOTO XATOSI:",
                repr(e)
            )

            if index < len(raw_urls):
                fallback = {
                    "chat_id": chat_id,
                    "photo": raw_urls[index],
                }

                if index == 0:
                    fallback[
                        "caption"
                    ] = caption

                    fallback[
                        "parse_mode"
                    ] = "HTML"

                    if reply_to_message_id is not None:
                        fallback[
                            "reply_to_message_id"
                        ] = reply_to_message_id

                await bot.send_photo(
                    **fallback
                )
            else:
                raise


async def send_pending_products(
    query,
    context,
    pending
):
    products = pending.get(
        "products",
        []
    )

    clear_pending_confirmation(
        query.message.chat_id
    )

    for product in products:
        await send_product_to_chat(
            context.bot,
            query.message.chat_id,
            product,
            reply_to_message_id=(
                query.message.message_id
            ),
        )


# ============================================================
# ADMIN: ADD PRODUCT
# ============================================================

async def add_product_command(
    update,
    context
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    admin_states[user.id] = {
        "mode": "add",
        "step": "photos",
        "pending_images": [],
        "image_bytes_list": [],
    }

    await update.message.reply_text(
        "➕ <b>Yangi mahsulot qo'shish</b>\n\n"
        "📸 1 yoki bir nechta rasm yuboring.\n\n"
        "Rasmlar tugagach: /done\n"
        "Bekor qilish: /cancel",
        parse_mode="HTML",
    )


async def save_new_product(
    update,
    context,
    state,
):
    if not update.message:
        return

    await update.message.reply_text(
        "⏳ Mahsulot GitHub'ga saqlanmoqda..."
    )

    try:
        name = state["name"]
        price = int(state["price"])
        category = state.get("category", "")
        description = state.get("description", "")

        image_bytes_list = state[
            "image_bytes_list"
        ]

        telegram_ids = state[
            "pending_images"
        ]

        raw_urls = []

        for index, image_bytes in enumerate(
            image_bytes_list,
            start=1,
        ):
            filename = (
                f"{slugify(name)}_"
                f"{int(time.time() * 1000)}_"
                f"{index}.jpg"
            )

            github_path = (
                f"{PRODUCTS_FOLDER}/{filename}"
            )

            await github_upload_file(
                github_path,
                image_bytes,
                f"Add product image: {name}",
            )

            raw_urls.append(
                "https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/{github_path}"
            )

        products = await github_get_products(
            force_refresh=True
        )

        product = {
            "id": str(
                int(time.time() * 1000)
            ),
            "name": name,
            "price": price,
            "category": category,
            "description": description,
            "telegram_file_id": telegram_ids[0],
            "images": telegram_ids,
            "raw_url": raw_urls[0] if raw_urls else "",
            "raw_urls": raw_urls,
            "keywords": [],
            "visible": True,
            "created_at": time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

        refresh_product_keywords(product)

        products.append(product)

        await github_save_products(
            products
        )

        admin_states.pop(
            update.message.from_user.id,
            None,
        )

        stats[
            "products_added"
        ] += 1

        month_3, month_6, month_12 = calculate_monthly(
            price
        )

        await update.message.reply_text(
            "✅ <b>Mahsulot muvaffaqiyatli saqlandi!</b>\n\n"
            f"🛍 <b>{name}</b>\n"
            f"🗂 Kategoriya: "
            f"<b>{category or 'Kategoriyasiz'}</b>\n"
            f"💵 Naqd: <b>{format_money(price)}</b>\n\n"
            f"📅 3 oy — <b>{format_money(month_3)}/oy</b>\n"
            f"📅 6 oy — <b>{format_money(month_6)}/oy</b>\n"
            f"📅 12 oy — <b>{format_money(month_12)}/oy</b>\n\n"
            f"📸 Rasmlar: <b>{len(telegram_ids)} ta</b>\n"
            "📦 Ma'lumotlar GitHub'ga saqlandi.",
            parse_mode="HTML",
        )

    except Exception as e:
        print(
            "ADD PRODUCT XATOSI:",
            repr(e)
        )

        admin_states.pop(
            update.message.from_user.id,
            None
        )

        await update.message.reply_text(
            "❌ Mahsulotni saqlashda xatolik yuz berdi."
        )


async def handle_admin_state(
    update,
    context,
):
    if not update.message:
        return False

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        return False

    state = admin_states.get(
        user.id
    )

    if not state:
        return False

    step = state.get("step")

    if step == "photos":
        if not update.message.photo:
            await update.message.reply_text(
                "📸 Rasm yuboring yoki /done bosing."
            )
            return True

        photo = update.message.photo[-1]

        try:
            tg_file = await context.bot.get_file(
                photo.file_id
            )

            image_bytes = (
                await tg_file.download_as_bytearray()
            )

            state[
                "pending_images"
            ].append(
                photo.file_id
            )

            state[
                "image_bytes_list"
            ].append(
                bytes(image_bytes)
            )

            count = len(
                state["pending_images"]
            )

            await update.message.reply_text(
                f"✅ {count}-rasm qabul qilindi.\n"
                "Yana rasm yuboring yoki /done bosing."
            )

        except Exception as e:
            print(
                "ADMIN PHOTO XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "❌ Rasmni qabul qilishda xatolik yuz berdi."
            )

        return True

    if step == "name":
        if not update.message.text:
            await update.message.reply_text(
                "📝 Mahsulot nomini yozing."
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
            "✅ Nom saqlandi.\n\n"
            "💰 Naqd narxni yozing.\n"
            "Masalan: 1500000"
        )

        return True

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
                "❌ Narx noto'g'ri.\nMasalan: 1500000"
            )
            return True

        state["price"] = price
        state["step"] = "category"

        await update.message.reply_text(
            "✅ Narx saqlandi.\n\n"
            "🗂 Kategoriya yozing.\n"
            "Masalan: Kullerlar\n\n"
            "Kerak bo'lmasa: /skip"
        )

        return True

    if step == "category":
        if not update.message.text:
            await update.message.reply_text(
                "🗂 Kategoriya yozing yoki /skip bosing."
            )
            return True

        state["category"] = (
            update.message.text.strip()
        )

        state["step"] = "description"

        await update.message.reply_text(
            "✅ Kategoriya saqlandi.\n\n"
            "📄 Mahsulot tavsifini yozing.\n"
            "Kerak bo'lmasa: /skip"
        )

        return True

    if step == "description":
        if not update.message.text:
            await update.message.reply_text(
                "📄 Tavsif yozing yoki /skip bosing."
            )
            return True

        state["description"] = (
            update.message.text.strip()
        )

        await save_new_product(
            update,
            context,
            state,
        )

        return True

    return False


async def done_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        return

    state = admin_states.get(
        user.id
    )

    if not state:
        await update.message.reply_text(
            "ℹ️ Hozir mahsulot qo'shish jarayoni yo'q."
        )
        return

    if state.get("step") != "photos":
        await update.message.reply_text(
            "ℹ️ /done hozirgi bosqichda ishlamaydi."
        )
        return

    if not state.get(
        "pending_images"
    ):
        await update.message.reply_text(
            "📸 Kamida 1 ta rasm yuboring."
        )
        return

    state["step"] = "name"

    await update.message.reply_text(
        "✅ Rasmlar qabul qilindi.\n\n"
        "📝 Mahsulot nomini yozing."
    )


async def skip_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        return

    state = admin_states.get(
        user.id
    )

    if not state:
        return

    step = state.get("step")

    if step == "category":
        state["category"] = ""
        state["step"] = "description"

        await update.message.reply_text(
            "✅ Kategoriya o'tkazildi.\n\n"
            "📄 Tavsif yozing yoki /skip bosing."
        )
        return

    if step == "description":
        state["description"] = ""

        await save_new_product(
            update,
            context,
            state,
        )
        return


# ============================================================
# ADMIN LIST / EDIT / DELETE / CATEGORY / STATS
# ============================================================

async def products_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    products = await github_get_products(
        force_refresh=True
    )

    if not products:
        await update.message.reply_text(
            "📦 Katalog bo'sh."
        )
        return

    lines = [
        "📦 <b>MAHSULOTLAR</b>\n"
    ]

    for i, product in enumerate(
        products[:50],
        start=1,
    ):
        status = (
            "✅"
            if visible_product(product)
            else "🚫"
        )

        lines.append(
            f"{i}. {status} "
            f"<b>{product.get('name', 'Nomsiz')}</b>"
        )

    if len(products) > 50:
        lines.append(
            f"\n… yana {len(products) - 50} ta."
        )

    lines.append(
        f"\n<b>Jami:</b> {len(products)} ta"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def edit_product_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    products = await github_get_products(
        force_refresh=True
    )

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

        keyboard.append([
            InlineKeyboardButton(
                (
                    f"{status} "
                    f"{product.get('name', 'Nomsiz')}"
                )[:60],
                callback_data=(
                    f"edit:{product['id']}"
                ),
            )
        ])

    await update.message.reply_text(
        "✏️ <b>Mahsulotni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def delete_product_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    products = await github_get_products(
        force_refresh=True
    )

    if not products:
        await update.message.reply_text(
            "📦 Katalogda mahsulotlar yo'q."
        )
        return

    keyboard = []

    for product in products[:50]:
        keyboard.append([
            InlineKeyboardButton(
                (
                    f"🗑 {product.get('name', 'Nomsiz')}"
                )[:60],
                callback_data=(
                    f"delconfirm:{product['id']}"
                ),
            )
        ])

    await update.message.reply_text(
        "🗑 <b>O'chirish uchun mahsulotni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def categories_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    products = await github_get_products(
        force_refresh=True
    )

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
            counts.get(category, 0)
            + 1
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
        key=lambda item: item[0].lower()
    ):
        lines.append(
            f"• {category} — {count} ta"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def stats_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda bu komandadan foydalanish huquqi yo'q."
        )
        return

    products = await github_get_products(
        force_refresh=True
    )

    visible_count = sum(
        1
        for p in products
        if visible_product(p)
    )

    await update.message.reply_text(
        "📊 <b>AKSO BOT STATISTIKASI</b>\n\n"
        f"📦 Jami mahsulotlar: <b>{len(products)}</b>\n"
        f"👁 Ko'rinadigan: <b>{visible_count}</b>\n"
        f"🚫 Yashirilgan: "
        f"<b>{len(products) - visible_count}</b>\n\n"
        f"💬 Xabarlar: <b>{stats['messages']}</b>\n"
        f"🔎 Katalog so'rovlari: "
        f"<b>{stats['catalog_queries']}</b>\n"
        f"🎯 Topilgan mahsulotlar: "
        f"<b>{stats['catalog_matches']}</b>\n\n"
        f"➕ Qo'shilgan: "
        f"<b>{stats['products_added']}</b>\n"
        f"✏️ Tahrirlangan: "
        f"<b>{stats['products_edited']}</b>\n"
        f"🗑 O'chirilgan: "
        f"<b>{stats['products_deleted']}</b>",
        parse_mode="HTML",
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    # -------- confirmation buttons --------

    if data == "confirm_products":
        pending = get_pending_confirmation(
            query.message.chat_id,
            query.from_user.id
        )

        if not pending:
            await query.answer(
                "⏳ Bu so'rov eskirgan. Mahsulotni yana so'rang.",
                show_alert=True
            )
            return

        await query.answer(
            "✅ Yuborilmoqda..."
        )

        await send_pending_products(
            query,
            context,
            pending
        )
        return

    if data == "cancel_products":
        pending = get_pending_confirmation(
            query.message.chat_id,
            query.from_user.id
        )

        if not pending:
            await query.answer(
                "So'rov allaqachon tugagan."
            )
            return

        clear_pending_confirmation(
            query.message.chat_id
        )

        await query.answer(
            "❌ Bekor qilindi."
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "Mayli. 📌 Boshqa mahsulotni so'rashingiz mumkin."
        )
        return

    # -------- admin callbacks --------

    admin_prefixes = (
        "edit:",
        "edfield:",
        "toggle:",
        "delconfirm:",
        "del:",
    )

    if data.startswith(admin_prefixes):
        if not is_admin(
            query.from_user.id
        ):
            await query.answer(
                "❌ Sizda ruxsat yo'q.",
                show_alert=True
            )
            return

    await query.answer()

    if data.startswith("edit:"):
        product_id = data.split(
            ":",
            1
        )[1]

        products = await github_get_products(
            force_refresh=True
        )

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
                    (
                        "🚫 Yashirish"
                        if visible_product(product)
                        else "👁 Ko'rsatish"
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
            ),
        )
        return

    if data.startswith("edfield:"):
        parts = data.split(":")

        if len(parts) != 3:
            return

        product_id = parts[1]
        field = parts[2]

        prompts = {
            "name":
                "📝 Yangi mahsulot nomini yozing:",
            "price":
                "💰 Yangi naqd narxni yozing:",
            "category":
                "🗂 Yangi kategoriyani yozing:",
            "description":
                "📄 Yangi tavsifni yozing:",
        }

        prompt = prompts.get(field)

        if not prompt:
            await query.message.reply_text(
                "ℹ️ Bu tahrirlash turi hozircha mavjud emas."
            )
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

    if data.startswith("toggle:"):
        product_id = data.split(
            ":",
            1
        )[1]

        products = await github_get_products(
            force_refresh=True
        )

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

        product["visible"] = not visible_product(
            product
        )

        await github_save_products(
            products
        )

        if product["visible"]:
            message = "👁 Mahsulot yana ko'rsatildi."
        else:
            stats["products_hidden"] += 1
            message = "🚫 Mahsulot yashirildi."

        await query.edit_message_text(
            (
                f"✅ <b>{product.get('name', '')}</b>\n\n"
                f"{message}"
            ),
            parse_mode="HTML",
        )
        return

    if data.startswith("delconfirm:"):
        product_id = data.split(
            ":",
            1
        )[1]

        products = await github_get_products(
            force_refresh=True
        )

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

        keyboard = [[
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
        ]]

        await query.edit_message_text(
            (
                "⚠️ <b>Haqiqatan o'chirmoqchimisiz?</b>\n\n"
                f"🛍 {product.get('name', '')}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )
        return

    if data.startswith("del:"):
        product_id = data.split(
            ":",
            1
        )[1]

        products = await github_get_products(
            force_refresh=True
        )

        new_products = [
            p
            for p in products
            if p.get("id") != product_id
        ]

        if len(new_products) == len(products):
            await query.edit_message_text(
                "❌ Mahsulot topilmadi."
            )
            return

        await github_save_products(
            new_products
        )

        stats["products_deleted"] += 1

        await query.edit_message_text(
            "✅ Mahsulot katalogdan o'chirildi."
        )
        return

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
            for p in await github_get_products()
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

        names = sorted(
            categories.keys(),
            key=lambda value: value.lower()
        )

        if not 0 <= index < len(names):
            return

        category = names[index]

        await query.message.reply_text(
            (
                f"🗂 <b>{category}</b>\n"
                f"📦 {len(categories[category])} ta mahsulot.\n\n"
                "📸 Rasmlari va bo'lib to'lash narxlarini ko'rsatish kerak bo'lsa, "
                "pastdagi tasdiqlash tugmasidan foydalaning."
            ),
            parse_mode="HTML",
        )

        # Kategoriya tugmasidan kelgan mahsulotlar ham tasdiqlashdan o'tadi.
        await ask_product_confirmation(
            query.message,
            query.from_user.id,
            categories[category][:10],
        )
        return


# ============================================================
# EDIT STATE
# ============================================================

async def handle_edit_state(
    update,
    context,
):
    if not update.message:
        return False

    user = update.message.from_user

    if not user or not is_admin(
        user.id
    ):
        return False

    state = admin_states.get(
        user.id
    )

    if not state:
        return False

    step = state.get("step")

    if step not in {
        "edit_name",
        "edit_price",
        "edit_category",
        "edit_description",
    }:
        return False

    if not update.message.text:
        await update.message.reply_text(
            "📝 Matn yuboring."
        )
        return True

    products = await github_get_products()

    product = next(
        (
            p
            for p in products
            if p.get("id")
            == state.get("product_id")
        ),
        None
    )

    if not product:
        admin_states.pop(
            user.id,
            None
        )

        await update.message.reply_text(
            "❌ Mahsulot topilmadi."
        )
        return True

    text = update.message.text.strip()
    field = state["field"]

    if field == "name":
        if not text:
            await update.message.reply_text(
                "❌ Nom bo'sh bo'lishi mumkin emas."
            )
            return True

        product["name"] = text

    elif field == "price":
        price = parse_price(text)

        if price is None:
            await update.message.reply_text(
                "❌ Narx noto'g'ri."
            )
            return True

        product["price"] = price

    elif field == "category":
        product["category"] = text

    elif field == "description":
        product["description"] = text

    refresh_product_keywords(
        product
    )

    await github_save_products(
        products
    )

    stats["products_edited"] += 1

    admin_states.pop(
        user.id,
        None
    )

    await update.message.reply_text(
        "✅ Mahsulot muvaffaqiyatli tahrirlandi."
    )

    return True


# ============================================================
# /CATALOG
# ============================================================

async def catalog_command(
    update,
    context,
):
    if not update.message:
        return

    products = [
        p
        for p in await github_get_products()
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

        categories[category] = (
            categories.get(category, 0)
            + 1
        )

    names = sorted(
        categories.keys(),
        key=lambda value: value.lower()
    )

    keyboard = []

    for index, category in enumerate(names):
        keyboard.append([
            InlineKeyboardButton(
                (
                    f"🗂 {category} "
                    f"({categories[category]})"
                ),
                callback_data=f"cat:{index}",
            )
        ])

    await update.message.reply_text(
        "🛍 <b>Mahsulot katalogi</b>\n\n"
        "Kategoriya tanlang:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# REPLY KEYBOARDS
# ============================================================

CUSTOMER_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("🛍 Mahsulotlar"),
            KeyboardButton("🔎 Mahsulot qidirish"),
        ],
        [
            KeyboardButton("❓ Yordam"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("➕ Mahsulot qo'shish"),
            KeyboardButton("📦 Mahsulotlar"),
        ],
        [
            KeyboardButton("✏️ Tahrirlash"),
            KeyboardButton("🗑 O'chirish"),
        ],
        [
            KeyboardButton("🗂 Kategoriyalar"),
            KeyboardButton("📊 Statistika"),
        ],
        [
            KeyboardButton("🛍 Katalog"),
            KeyboardButton("❓ Yordam"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


async def show_main_keyboard(
    update,
):
    if not update.message:
        return

    user = update.message.from_user
    keyboard = (
        ADMIN_KEYBOARD
        if user and is_admin(user.id)
        else CUSTOMER_KEYBOARD
    )

    await update.message.reply_text(
        "Quyidagi tugmalardan foydalanishingiz mumkin:",
        reply_markup=keyboard,
    )


async def handle_menu_button(
    update,
    context,
):
    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    user = update.message.from_user

    if user and is_admin(user.id):
        admin_actions = {
            "➕ Mahsulot qo'shish": add_product_command,
            "📦 Mahsulotlar": products_command,
            "✏️ Tahrirlash": edit_product_command,
            "🗑 O'chirish": delete_product_command,
            "🗂 Kategoriyalar": categories_command,
            "📊 Statistika": stats_command,
            "🛍 Katalog": catalog_command,
            "❓ Yordam": help_command,
        }

        action = admin_actions.get(text)

        if action:
            await action(update, context)
            return True

    customer_actions = {
        "🛍 Mahsulotlar": catalog_command,
        "🔎 Mahsulot qidirish": help_command,
        "❓ Yordam": help_command,
    }

    action = customer_actions.get(text)

    if action:
        await action(update, context)
        return True

    return False


# ============================================================
# /HELP /START /ID /CANCEL
# ============================================================

async def help_command(
    update,
    context,
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
        "Men mos mahsulotlarni topaman va "
        "rasm hamda bo'lib to'lash narxlarini "
        "faqat siz tasdiqlaganingizdan keyin yuboraman.",
        parse_mode="HTML",
    )


async def start_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    keyboard = (
        ADMIN_KEYBOARD
        if user and is_admin(user.id)
        else CUSTOMER_KEYBOARD
    )

    await update.message.reply_text(
        "👋 Assalomu alaykum! Men AKSO AI botman.\n\n"
        "Sizga kerakli mahsulotni oddiy tilda yozing. "
        "Men mos mahsulotni topishga harakat qilaman.",
        reply_markup=keyboard,
    )


async def my_id_command(
    update,
    context,
):
    if not update.message:
        return

    user = update.message.from_user

    if not user:
        return

    await update.message.reply_text(
        f"🆔 Sizning Telegram ID'ingiz:\n\n"
        f"<code>{user.id}</code>",
        parse_mode="HTML",
    )


async def cancel_command(
    update,
    context,
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

    clear_pending_confirmation(
        update.message.chat_id
    )

    user_keyboard = (
        ADMIN_KEYBOARD
        if is_admin(user.id)
        else CUSTOMER_KEYBOARD
    )

    await update.message.reply_text(
        "❌ Joriy amal bekor qilindi.",
        reply_markup=user_keyboard,
    )


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def reply_to_message(
    update,
    context,
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

    # Admin jarayonlari birinchi o'rinda.
    if await handle_edit_state(
        update,
        context,
    ):
        return

    if await handle_admin_state(
        update,
        context,
    ):
        return

    # Oddiy / oddiy suhbatga javob.
    if update.message.photo:
        # Rasm tahlili saqlanadi.
        try:
            photo = update.message.photo[-1]

            file = await context.bot.get_file(
                photo.file_id
            )

            image_bytes = (
                await file.download_as_bytearray()
            )

            user_text = (
                update.message.caption.strip()
                if update.message.caption
                else
                "Bu rasmda nima borligini batafsil tushuntir."
            )

            prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Rasmni diqqat bilan tahlil qil.

Qoidalar:
- O'zbekcha savolga o'zbekcha javob ber.
- Ruscha savolga ruscha javob ber.
- Inglizcha savolga inglizcha javob ber.
- Ko'rinadigan narsalarni aniq ayt.
- Bilmagan narsani fakt sifatida aytma.
- Keraksiz uzun javob bermagin.

Savol:
{user_text}
"""

            image_part = types.Part.from_bytes(
                data=bytes(image_bytes),
                mime_type="image/jpeg",
            )

            response = await ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    image_part,
                    prompt,
                ],
            )

            answer = (
                response.text
                or
                "Kechirasiz, rasmni tahlil qila olmadim."
            )

            if len(answer) > 4000:
                answer = answer[:4000] + "..."

            kwargs = {
                "text": answer,
            }

            if update.message.chat.type in (
                "group",
                "supergroup",
            ):
                kwargs[
                    "reply_to_message_id"
                ] = update.message.message_id

            await update.message.reply_text(
                **kwargs
            )

        except Exception as e:
            print(
                "RASM GEMINI XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "Kechirasiz, rasmni tahlil qilishda texnik xatolik yuz berdi."
            )

        return

    if not update.message.text:
        return

    user_text = (
        update.message.text.strip()
    )

    if not user_text:
        return

    # Reply keyboard tugmalari bosilganda ularni oddiy
    # mahsulot qidiruvi deb qabul qilmaymiz.
    if await handle_menu_button(
        update,
        context,
    ):
        return

    # --------------------------------------------------------
    # PENDING CONFIRMATION: "ha", "xa", "albatta", "yubor"
    # --------------------------------------------------------

    confirmation_words = {
        "ha",
        "xa",
        "xaaa",
        "albatta",
        "ha yuboring",
        "yuboring",
        "rasmlarni yuboring",
        "yubor",
        "mayli yuboring",
    }

    normalized_user_text = normalize_text(
        user_text
    )

    if normalized_user_text in {
        normalize_text(word)
        for word in confirmation_words
    }:
        pending = get_pending_confirmation(
            update.message.chat_id,
            update.message.from_user.id
            if update.message.from_user
            else 0,
        )

        if pending:
            clear_pending_confirmation(
                update.message.chat_id
            )

            for product in pending[
                "products"
            ]:
                await send_product_to_chat(
                    context.bot,
                    update.message.chat_id,
                    product,
                    reply_to_message_id=(
                        update.message.message_id
                    ),
                )

            return

    # "yo'q" -> pendingni bekor qilish.
    if normalized_user_text in {
        "yoq",
        "kerak emas",
        "yoq rahmat",
        "not",
    }:
        pending = get_pending_confirmation(
            update.message.chat_id,
            update.message.from_user.id
            if update.message.from_user
            else 0,
        )

        if pending:
            clear_pending_confirmation(
                update.message.chat_id
            )

            await update.message.reply_text(
                "Mayli. 📌 Boshqa mahsulotni so'rashingiz mumkin."
            )
            return

    # --------------------------------------------------------
    # PRODUCT CATALOG SEARCH
    # --------------------------------------------------------

    if likely_product_query(
        user_text
    ):
        try:
            products = await github_get_products()

            if products:
                stats[
                    "catalog_queries"
                ] += 1

                # 1. Qat'iy local/fuzzy.
                matches = find_local_products(
                    user_text,
                    products,
                )

                # 2. Topilmasa, faqat savdo maqsadli so'rovga AI.
                if not matches:
                    matches = await find_ai_products(
                        user_text,
                        products,
                    )

                if matches:
                    await ask_product_confirmation(
                        update.message,
                        update.message.from_user.id
                        if update.message.from_user
                        else 0,
                        matches,
                    )
                    return

                # Mahsulot so'rovi bo'lib,
                # mos tovar topilmagan bo'lsa,
                # "bizda yo'q" deb keskin xulosa qilmaymiz.
                await update.message.reply_text(
                    "🔎 So'rovingizni tushundim, "
                    "lekin katalogdan aynan mos mahsulotni "
                    "aniq topa olmadim.\n\n"
                    "Mahsulot nomini yoki turini biroz boshqacharoq yozib ko'ring."
                )
                return

        except Exception as e:
            print(
                "KATALOG QIDIRUV XATOSI:",
                repr(e)
            )

            await update.message.reply_text(
                "🔎 Mahsulot katalogini tekshirishda "
                "vaqtinchalik texnik muammo yuz berdi."
            )
            return

    # --------------------------------------------------------
    # NORMAL AI CHAT
    # --------------------------------------------------------

    try:
        prompt = f"""
Sen Telegramdagi AKSO AI yordamchisisan.

Foydalanuvchiga tabiiy, foydali va aniq javob ber.

Qoidalar:
- O'zbekcha bo'lsa o'zbekcha.
- Ruscha bo'lsa ruscha.
- Inglizcha bo'lsa inglizcha.
- Keraksiz uzun javob bermagin.
- Oddiy suhbatga mahsulot rasmi yuborma.
- Foydalanuvchi mahsulotni aniq so'ramasa,
  katalogdagi mahsulotlarni o'zboshimchalik bilan tavsiya qilma.

Foydalanuvchi:
{user_text}
"""

        response = await ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )

        answer = (
            response.text
            or
            "Kechirasiz, hozir javob bera olmadim."
        )

        if len(answer) > 4000:
            answer = answer[:4000] + "..."

        kwargs = {
            "text": answer,
        }

        if update.message.chat.type in (
            "group",
            "supergroup",
        ):
            kwargs[
                "reply_to_message_id"
            ] = update.message.message_id

        await update.message.reply_text(
            **kwargs
        )

    except Exception as e:
        print(
            "GEMINI XATOSI:",
            repr(e)
        )

        try:
            kwargs = {
                "text":
                    "Kechirasiz, hozir javob berishda "
                    "texnik xatolik yuz berdi."
            }

            if update.message.chat.type in (
                "group",
                "supergroup",
            ):
                kwargs[
                    "reply_to_message_id"
                ] = update.message.message_id

            await update.message.reply_text(
                **kwargs
            )

        except Exception as telegram_error:
            print(
                "TELEGRAM JAVOB XATOSI:",
                repr(telegram_error)
            )


# ============================================================
# COMMAND MENUS
# ============================================================

async def setup_command_menus(
    application,
):
    await application.bot.delete_my_commands()

    await application.bot.delete_my_commands(
        scope=BotCommandScopeChat(
            chat_id=ADMIN_ID
        )
    )

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
            "✅ Rasmlarni tugatish"
        ),
        BotCommand(
            "skip",
            "⏭ O'tkazib yuborish"
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

    print(
        "✅ Telegram command menus o'rnatildi."
    )

    print(
        "✅ Admin commands:",
        [
            c.command
            for c in admin_commands
        ]
    )


# ============================================================
# APPLICATION
# ============================================================

telegram_app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .post_init(
        setup_command_menus
    )
    .build()
)


# ============================================================
# HANDLERS
# ============================================================

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

telegram_app.add_handler(
    CallbackQueryHandler(
        callback_handler
    )
)

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


# ============================================================
# WEBHOOK
# ============================================================

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
