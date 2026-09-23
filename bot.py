import os
import json
import base64
import re
import time
import unicodedata
import asyncio
import threading
from html import escape
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from difflib import SequenceMatcher
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import unquote

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputFile,
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
ORDERS_FILE = "orders.json"
CHAT_HISTORY_FILE = "chat_history.json"
ADMIN_DRAFTS_FILE = "admin_drafts.json"
ADMINS_FILE = "admins.json"


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
admin_draft_checked_users = set()
# Qo'shimcha adminlar persistent ravishda GitHub'da saqlanadi.
additional_admin_ids = set()

# chat_id -> {user_id, products, created_at}
pending_product_confirmations = {}

# AI sales memory
conversation_memory = {}
last_product_queries = {}
order_states = {}

# Persistent customer chat history for the bot owner only.
# The history is intentionally capped so the file cannot grow without limit.
chat_history = {}
HISTORY_MAX_MESSAGES = 200
HISTORY_PAGE_SIZE = 25
history_save_task = None
history_dirty = False
history_save_lock = asyncio.Lock()

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
    "products_viewed": 0,
    "not_found_queries": 0,
    "operator_requests": 0,
    "orders": 0,
    "recommendation_requests": 0,
}

# ============================================================
# AKSO KNOWLEDGE BASE
# ============================================================

AKSO_KNOWLEDGE = {
    "store_name": "AKSO",
    "address": "Shersaxiy bozori yoni",
    "landmark": "Shersaxiy stoyanka oldi",
    "working_days": "Har kuni, dam olish kunlarisiz",
    "working_hours": "08:00 - 20:00",
    "phone": "+998 78 555-17-77",
    "telegram": "@aksouz",
    "instagram": "@akso.uz",
    "website": "akso.uz",
    "delivery": "Qorako'l va Olot tumanlari bo'ylab bepul, hech qanday shartlarsiz.",
    "installation": "Barcha mahsulotlarga montaj/o'rnatish bor va bepul.",
    "payment": "Naqd, karta va bo'lib to'lash mavjud.",
    "installment_3": "3 oy — 0% ustama.",
    "installment_6": "6 oy — odatda 18% ustama. Ammo ayrim mahsulotlarda, aksiya vaqtida yoki oldindan to'lov bilan 6 oyga ustamasiz bo'lishi mumkin.",
    "installment_12": "12 oy — 36% ustama.",
    "installment_doc": "Bo'lib to'lash uchun pasport talab qilinadi.",
    "warranty": "Kafolat mavjud, 10 yilgacha. Garantiya taloni saqlangan bo'lishi kerak.",
    "returns": "Mahsulot shikastlanmagan bo'lsa, 1 oy ichida qaytarish yoki almashtirish mumkin.",
    "promotions": "Yangi va doimiy mijozlar uchun chegirma va aksiyalar mavjud.",
    "operator": "+998-94-529-07-07",
}


def akso_knowledge_text():
    return "\n".join([
        f"Do'kon: {AKSO_KNOWLEDGE['store_name']}",
        f"Manzil: {AKSO_KNOWLEDGE['address']}",
        f"Mo'ljal: {AKSO_KNOWLEDGE['landmark']}",
        f"Ish vaqti: {AKSO_KNOWLEDGE['working_days']}, {AKSO_KNOWLEDGE['working_hours']}",
        f"Telefon: {AKSO_KNOWLEDGE['phone']}",
        f"Telegram: {AKSO_KNOWLEDGE['telegram']}",
        f"Instagram: {AKSO_KNOWLEDGE['instagram']}",
        f"Sayt: {AKSO_KNOWLEDGE['website']}",
        f"Yetkazib berish: {AKSO_KNOWLEDGE['delivery']}",
        f"Montaj: {AKSO_KNOWLEDGE['installation']}",
        f"To'lov: {AKSO_KNOWLEDGE['payment']}",
        f"3 oy: {AKSO_KNOWLEDGE['installment_3']}",
        f"6 oy: {AKSO_KNOWLEDGE['installment_6']}",
        f"12 oy: {AKSO_KNOWLEDGE['installment_12']}",
        f"Hujjat: {AKSO_KNOWLEDGE['installment_doc']}",
        f"Kafolat: {AKSO_KNOWLEDGE['warranty']}",
        f"Qaytarish/almashtirish: {AKSO_KNOWLEDGE['returns']}",
        f"Aksiyalar: {AKSO_KNOWLEDGE['promotions']}",
        f"Operator: {AKSO_KNOWLEDGE['operator']}",
    ])


# ============================================================
# BASIC HELPERS
# ============================================================

def is_owner(user_id):
    return user_id == ADMIN_ID


def is_admin(user_id):
    return user_id == ADMIN_ID or user_id in additional_admin_ids


async def github_get_admin_ids():
    """Qo'shimcha admin Telegram IDlarini GitHub'dan o'qiydi."""
    try:
        result = await github_api(
            "GET",
            f"{ADMINS_FILE}?ref={GITHUB_BRANCH}",
        )
        content = result.get("content", "")
        if not content:
            return []
        decoded = base64.b64decode(
            content.replace("\n", "")
        ).decode("utf-8")
        data = json.loads(decoded)
        if isinstance(data, dict):
            data = data.get("admin_ids", [])
        if not isinstance(data, list):
            return []
        return sorted({int(x) for x in data if str(x).strip().isdigit()})
    except Exception as e:
        if "404" in str(e):
            return []
        print("ADMINS O'QISH XATOSI:", repr(e))
        return []


async def github_save_admin_ids(admin_ids):
    """Qo'shimcha adminlarni kichik JSON faylga saqlaydi."""
    unique_ids = sorted({int(x) for x in admin_ids if int(x) != ADMIN_ID})
    payload_obj = {"admin_ids": unique_ids}
    encoded = base64.b64encode(
        json.dumps(
            payload_obj,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
    ).decode("ascii")

    sha = None
    try:
        current = await github_api(
            "GET",
            f"{ADMINS_FILE}?ref={GITHUB_BRANCH}",
        )
        sha = current.get("sha")
    except Exception as e:
        if "404" not in str(e):
            raise

    data = {
        "message": "Update AKSO admins",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        data["sha"] = sha

    await github_api(
        "PUT",
        ADMINS_FILE,
        data,
    )


async def load_additional_admins():
    """Bot ishga tushganda qo'shimcha adminlarni xotiraga yuklaydi."""
    global additional_admin_ids
    try:
        additional_admin_ids = set(await github_get_admin_ids())
        additional_admin_ids.discard(ADMIN_ID)
        print(f"✅ Qo'shimcha adminlar yuklandi: {sorted(additional_admin_ids)}")
    except Exception as e:
        additional_admin_ids = set()
        print("ADMINS YUKLASH XATOSI:", repr(e))


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
    """Return every known image source, including legacy product records."""
    sources = []

    # Current format.
    current = product.get("images")
    if isinstance(current, list):
        sources.extend(x for x in current if isinstance(x, str) and x.strip())

    # Legacy / alternate formats used by earlier bot versions.
    for key in (
        "telegram_file_ids",
        "raw_urls",
        "image_urls",
        "photos",
    ):
        value = product.get(key)
        if isinstance(value, list):
            sources.extend(x for x in value if isinstance(x, str) and x.strip())

    for key in ("telegram_file_id", "raw_url", "image_url"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            sources.append(value)

    # Keep order but remove duplicates.
    result = []
    seen = set()
    for source in sources:
        if source not in seen:
            seen.add(source)
            result.append(source)
    return result


async def _download_http_image_bytes(image):
    """Download a product image to bytes before uploading it to Telegram."""
    raw_prefix = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/"
    )

    def _download(url):
        request = Request(
            url,
            headers={
                "User-Agent": "AKSO-Telegram-Bot/1.0",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=30) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            data = response.read()
        if not data:
            raise ValueError("empty image response")
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("image is larger than 20 MB")
        # GitHub/raw should never return an HTML error page as an image.
        if "text/html" in content_type:
            raise ValueError(f"unexpected content type: {content_type}")
        return data

    # For our own public GitHub files, try raw.githubusercontent.com first.
    # This avoids dependence on the GitHub Contents API/token for image reads.
    urls = [image]
    if image.startswith(raw_prefix):
        urls.append(
            "https://github.com/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}/raw/refs/heads/"
            f"{GITHUB_BRANCH}/"
            f"{unquote(image[len(raw_prefix):]).split('?', 1)[0]}"
        )

    last_error = None
    for url in urls:
        try:
            return await asyncio.to_thread(_download, url)
        except Exception as e:
            last_error = e
            print("PRODUCT IMAGE DOWNLOAD ATTEMPT XATOSI:", repr(e), url)

    # Last fallback: GitHub Contents API with the RAW media type.
    if image.startswith(raw_prefix) and GITHUB_TOKEN:
        relative_path = unquote(image[len(raw_prefix):]).split("?", 1)[0]
        api_url = (
            f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/"
            f"contents/{relative_path}?ref={GITHUB_BRANCH}"
        )

        def _download_github_api():
            request = Request(
                api_url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.raw",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "AKSO-Telegram-Bot/1.0",
                },
            )
            with urlopen(request, timeout=30) as response:
                data = response.read()
            if not data:
                raise ValueError("empty GitHub image response")
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("image is larger than 20 MB")
            return data

        try:
            return await asyncio.to_thread(_download_github_api)
        except Exception as e:
            last_error = e
            print("GITHUB RAW IMAGE DOWNLOAD XATOSI:", repr(e), image)

    raise last_error or ValueError("image download failed")


async def prepare_telegram_photo_source(image, index=1, bot=None, force_upload=False):
    """Prepare a Telegram photo source. HTTP URLs are converted to bytes."""
    if not isinstance(image, str):
        return image

    if image.startswith(("http://", "https://")):
        data = await _download_http_image_bytes(image)
        return data

    if force_upload and bot and image:
        try:
            tg_file = await bot.get_file(image)
            data = bytes(await tg_file.download_as_bytearray())
            if not data:
                raise ValueError("empty Telegram file response")
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("image is larger than 20 MB")
            return data
        except Exception as e:
            print("PRODUCT TELEGRAM FILE DOWNLOAD XATOSI:", repr(e), image)
            raise

    return image


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


async def github_get_admin_drafts():
    """GitHub'dagi aktiv admin mahsulot qo'shish draftlarini o'qiydi."""
    try:
        result = await github_api(
            "GET",
            f"{ADMIN_DRAFTS_FILE}?ref={GITHUB_BRANCH}",
        )
        content = result.get("content", "")
        if not content:
            return {}

        decoded = base64.b64decode(
            content.replace("\n", "")
        ).decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}

    except Exception as e:
        if "404" in str(e):
            return {}
        raise


async def github_save_admin_drafts(drafts):
    """Admin draftlarini GitHub'da kichik JSON fayl sifatida saqlaydi."""
    content = json.dumps(
        drafts,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    encoded = base64.b64encode(content).decode("ascii")
    sha = None

    try:
        current = await github_api(
            "GET",
            f"{ADMIN_DRAFTS_FILE}?ref={GITHUB_BRANCH}",
        )
        sha = current.get("sha")
    except Exception as e:
        if "404" not in str(e):
            raise

    data = {
        "message": "Update admin product draft",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }

    if sha:
        data["sha"] = sha

    await github_api(
        "PUT",
        ADMIN_DRAFTS_FILE,
        data,
    )


async def persist_admin_draft(user_id):
    """Faqat JSONga sig'adigan admin mahsulot qo'shish holatini saqlaydi."""
    state = admin_states.get(user_id)
    if not state or state.get("mode") != "add":
        return

    draft = {
        "mode": "add",
        "step": state.get("step", "photos"),
        "pending_images": list(state.get("pending_images", [])),
        "image_kinds": list(state.get("image_kinds", [])),
        "image_extensions": list(state.get("image_extensions", [])),
        "name": state.get("name", ""),
        "price": state.get("price"),
        "category": state.get("category", ""),
        "description": state.get("description", ""),
        "admin_id": int(user_id),
        "updated_at": int(time.time()),
    }

    drafts = await github_get_admin_drafts()
    drafts[str(user_id)] = draft
    await github_save_admin_drafts(drafts)


def _draft_to_state(draft):
    if not isinstance(draft, dict):
        return None
    if draft.get("mode") != "add":
        return None

    return {
        "mode": "add",
        "step": draft.get("step", "photos"),
        "pending_images": list(draft.get("pending_images", [])),
        "image_bytes_list": [],
        "image_kinds": list(draft.get("image_kinds", [])),
        "image_extensions": list(draft.get("image_extensions", [])),
        "name": draft.get("name", ""),
        "price": draft.get("price"),
        "category": draft.get("category", ""),
        "description": draft.get("description", ""),
        "admin_id": int(draft.get("admin_id") or ADMIN_ID),
    }


async def restore_admin_draft(user_id):
    """Agar Render qayta ishga tushgan bo'lsa, aktiv add-product holatini tiklaydi."""
    if user_id in admin_draft_checked_users:
        return admin_states.get(user_id)

    admin_draft_checked_users.add(user_id)

    try:
        drafts = await github_get_admin_drafts()
        draft = drafts.get(str(user_id))
        state = _draft_to_state(draft)
        if state:
            admin_states[user_id] = state
            print(
                f"✅ Admin mahsulot drafti tiklandi: user={user_id}, step={state.get('step')}"
            )
            return state
    except Exception as e:
        print("ADMIN DRAFT RESTORE XATOSI:", repr(e))

    return None


async def clear_admin_draft(user_id):
    admin_states.pop(user_id, None)
    admin_draft_checked_users.add(user_id)

    try:
        drafts = await github_get_admin_drafts()
        if str(user_id) in drafts:
            drafts.pop(str(user_id), None)
            await github_save_admin_drafts(drafts)
    except Exception as e:
        print("ADMIN DRAFT CLEAR XATOSI:", repr(e))


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

# ============================================================
# AI SALES ASSISTANT V2
# ============================================================

MAX_MEMORY_TURNS = 12


def _history_key(key):
    if not key:
        return None
    try:
        return f"{key[0]}:{key[1]}"
    except Exception:
        return str(key)


def _mark_history_dirty():
    global history_dirty, history_save_task
    history_dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if history_save_task is None or history_save_task.done():
        history_save_task = loop.create_task(_history_save_worker())


def record_history_user(update):
    if not update or not update.message or not update.message.from_user:
        return None
    user = update.message.from_user
    key = _history_key((update.message.chat_id, user.id))
    if not key:
        return None
    item = chat_history.setdefault(key, {
        "chat_id": update.message.chat_id,
        "user_id": user.id,
        "full_name": user.full_name or "Noma'lum",
        "username": user.username or "",
        "messages": [],
        "last_activity": int(time.time()),
    })
    item["full_name"] = user.full_name or item.get("full_name") or "Noma'lum"
    item["username"] = user.username or ""
    item["last_activity"] = int(time.time())
    return key


def remember_turn_by_key(key, role, text):
    if not key or not text:
        return

    clean_text = str(text)[:4000]

    turns = conversation_memory.setdefault(key, [])
    turns.append({"role": role, "text": clean_text[:1500]})
    if len(turns) > MAX_MEMORY_TURNS:
        del turns[:-MAX_MEMORY_TURNS]

    # Separate persistent history is used for the owner-facing history viewer.
    hkey = _history_key(key)
    if hkey:
        item = chat_history.setdefault(hkey, {
            "chat_id": key[0],
            "user_id": key[1],
            "full_name": "Noma'lum",
            "username": "",
            "messages": [],
            "last_activity": int(time.time()),
        })
        item["last_activity"] = int(time.time())
        item["messages"].append({
            "role": role,
            "text": clean_text,
            "time": int(time.time()),
        })
        if len(item["messages"]) > HISTORY_MAX_MESSAGES:
            del item["messages"][:-HISTORY_MAX_MESSAGES]
        _mark_history_dirty()


async def github_get_chat_history():
    try:
        result = await github_api(
            "GET",
            f"{CHAT_HISTORY_FILE}?ref={GITHUB_BRANCH}",
        )
        content = result.get("content", "")
        if not content:
            return {}
        data = json.loads(
            base64.b64decode(
                content.replace("\n", "")
            ).decode("utf-8")
        )
        return data if isinstance(data, dict) else {}
    except Exception as e:
        if "404" in str(e):
            return {}
        raise


async def github_save_chat_history(history):
    encoded = base64.b64encode(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
    ).decode("ascii")

    sha = None
    try:
        result = await github_api(
            "GET",
            f"{CHAT_HISTORY_FILE}?ref={GITHUB_BRANCH}",
        )
        sha = result.get("sha")
    except Exception as e:
        if "404" not in str(e):
            raise

    payload = {
        "message": "Update AKSO chat history",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    await github_api(
        "PUT",
        CHAT_HISTORY_FILE,
        payload,
    )


async def _history_save_worker():
    global history_dirty, history_save_task
    # Debounce frequent messages so we do not write to GitHub on every turn.
    await asyncio.sleep(5)
    while True:
        async with history_save_lock:
            if not history_dirty:
                history_save_task = None
                return
            snapshot = json.loads(
                json.dumps(chat_history, ensure_ascii=False)
            )
            history_dirty = False
            try:
                await github_save_chat_history(snapshot)
            except Exception as e:
                print("CHAT HISTORY SAQLASH XATOSI:", repr(e))
                history_dirty = True
        if not history_dirty:
            history_save_task = None
            return
        await asyncio.sleep(5)


async def flush_chat_history():
    global history_dirty, history_save_task
    if history_save_task is not None and not history_save_task.done():
        try:
            await history_save_task
        except Exception as e:
            print("CHAT HISTORY WORKER XATOSI:", repr(e))
    if history_dirty:
        async with history_save_lock:
            snapshot = json.loads(
                json.dumps(chat_history, ensure_ascii=False)
            )
            history_dirty = False
            try:
                await github_save_chat_history(snapshot)
            except Exception as e:
                history_dirty = True
                print("CHAT HISTORY YAKUNIY SAQLASH XATOSI:", repr(e))


def get_memory_key(update):
    if not update or not update.message or not update.message.from_user:
        return None
    return (update.message.chat_id, update.message.from_user.id)


def get_conversation_context(key):
    turns = conversation_memory.get(key, [])
    lines = []
    for t in turns[-MAX_MEMORY_TURNS:]:
        who = "Mijoz" if t.get("role") == "user" else "AKSO"
        lines.append(f"{who}: {t.get('text', '')}")
    return "\n".join(lines)


def extract_budget(text):
    q = normalize_text(text)
    pats = [
        (r'(\d+(?:[\.,]\d+)?)\s*(mln|million|mlrd|milliard)', 1_000_000),
        (r'(\d+(?:[\.,]\d+)?)\s*(ming|minglik)', 1_000),
        (r'(\d+(?:[\.,]\d+)?)\s*(?:som|sum)', 1),
    ]
    for pat,m in pats:
        mm=re.search(pat,q)
        if mm:
            try:
                return int(float(mm.group(1).replace(',','.'))*m)
            except Exception:
                pass
    return None


def extract_months(text):
    q=normalize_text(text)
    m=re.search(r'\b(3|6|12)\s*(oy|oyga|oylik|month|months)\b',q)
    return int(m.group(1)) if m else None


def extract_monthly_budget(text):
    q=normalize_text(text)
    if not any(x in q for x in ("oyiga","oylik","har oy","oy uchun")):
        return None
    return extract_budget(text)


def extract_colors(text):
    q=normalize_text(text)
    return [c for c in ("oq","qora","kulrang","jigarrang","bej","krem","yashil","kok","sariq","qizil","pushti","moviy") if c in q]


def extract_dimensions(text):
    q=normalize_text(text); out=[]
    for m in re.finditer(r'\b(\d+(?:[\.,]\d+)?)\s*(m|metr|sm|cm)\b',q): out.append(m.group(0))
    m=re.search(r'\b\d+(?:[\.,]\d+)?\s*[xх×]\s*\d+(?:[\.,]\d+)?\b',q)
    if m: out.append(m.group(0))
    return out


def monthly_payment(price, months):
    if months == 3: return round(price/3)
    if months == 6: return round((price*1.18)/6)
    if months == 12: return round((price*1.36)/12)
    return price


def filter_products_for_request(products, text):
    budget=extract_budget(text); monthly=extract_monthly_budget(text); months=extract_months(text) or 3
    colors=extract_colors(text); dims=extract_dimensions(text)
    filtered=[]
    for product in products:
        try: price=int(product.get("price",0))
        except Exception: continue
        searchable=normalize_text(" ".join([product.get("name",""),product.get("category",""),product.get("description","")," ".join(product.get("keywords",[]))]))
        if monthly is not None and monthly_payment(price,months)>monthly: continue
        if monthly is None and budget is not None and price>budget: continue
        if colors and not any(c in searchable for c in colors): continue
        # Dimensions are a soft constraint because older catalog descriptions may omit them.
        filtered.append(product)
    return filtered,{"budget":budget,"monthly":monthly,"months":months,"colors":colors,"dimensions":dims}


def build_search_query(key, text):
    prev = last_product_queries.get(key, "")
    q = normalize_text(text)

    ordinary = {
        "ha", "xa", "albatta", "mayli", "rahmat", "raxmat",
        "ok", "okay", "yoq", "yaxshi", "zor", "tushunarli",
    }

    # Agar mijoz yangi mahsulot nomini aniq aytsa, avvalgi mahsulot
    # kontekstini aralashtirmaymiz. Masalan:
    #   "kuller" -> keyin "divan"
    # natijada "kuller divan" bo'lmasligi kerak.
    current_topics = product_topics(text)
    if current_topics:
        return text

    # Mahsulot aytilmagan qisqa davomiy savollar avvalgi mahsulotga tegishli
    # bo'lishi mumkin: "oq", "oyiga 500 ming", "kupe" va hokazo.
    qualifier = (
        q not in ordinary
        and (
            len(q.split()) <= 5
            or any(
                x in q
                for x in (
                    "uchun", "oyiga", "oylik", "million", "mln",
                    "ming", "oq", "qora", "kulrang", "kupe",
                    "detski", "bolalar", "kiyim",
                )
            )
        )
    )

    if prev and qualifier:
        return f"{prev} {text}"

    return text


def is_guided_need(text, matches):
    q=normalize_text(text)
    broad={"shkaf","mebel","divan","stol","stul","karavat","oshxona"}
    return bool(set(token_list(text)) & broad) and any(x in q for x in ("menga","kerak","izlayapman","qidiryapman")) and not any(x in q for x in ("oq","qora","kupe","detski","bolalar","kiyim","million","mln","ming","oyiga","oylik")) and len(matches)>=4


def guided_question(text):
    q=normalize_text(text)
    if "shkaf" in q:
        return "Albatta. 😊 Qaysi turdagi shkaf kerak?\n\n• 👕 Kiyim uchun\n• 🚪 Kupe shkaf\n• 🧸 Bolalar shkafi\n• 🏠 Boshqa turdagi shkaf\n\nByudjetingiz yoki oyiga qulay to'lovingizni ham yozsangiz, variantlarni yanada aniq tanlayman."
    if "divan" in q:
        return "Albatta. 😊 Qaysi divan kerak?\n\n• 🛋 Oddiy divan\n• 🛏 Divan-karavot\n• 📐 Burchakli divan\n• 🔄 Transformator divan\n\nByudjetingizni yozing, mos variantlarni ajrataman."
    if "oshxona" in q:
        return "Oshxona mebelidan qaysi biri kerak: garnitur, stol-stul yoki boshqa mahsulot? Byudjet yoki taxminiy o'lchamni yozsangiz, tanlovni toraytiraman. 😊"
    if "stol" in q:
        return "Stolning qaysi turi kerak: oshxona, ovqatlanish, yozuv yoki kompyuter stoli? Byudjetingizni ham yozing. 😊"
    return "Sizga mosini topishim uchun mahsulot turi va byudjetingizni yozing. 😊"


# Mahsulotga oid aniq mavzular. "kerak", "bormi", "bering" kabi umumiy
# so'zlar yolg'iz o'zi katalog qidiruvini ishga tushirmaydi.
PRODUCT_TOPIC_TERMS = (
    "muzlatgich", "holodilnik", "kir yuvish", "kiryuvish", "konditsioner",
    "gaz plita", "tok plita", "mikrovolnovka", "duxovka", "changyutgich",
    "pylesos", "kuller", "kuler", "sharbat siqgich", "go'sht maydalagich",
    "televizor", "televizr", "smart tv", "smarttv", "tv", "shkaf", "kupe",
    "komod", "tumbochka", "yotoqxona", "spalnya", "spalni", "divan",
    "kreslo", "karavot", "krovat", "sandiq", "matras", "stol", "stul",
    "oshxona", "mehmonxona", "ugolok", "stelaj", "polka", "vitrina",
    "gilam", "kovyor", "velosiped", "skuter", "elektro skuter",
    "o'yinchoq", "o'yinchoqlar", "kartlar",
)
PRODUCT_TOPIC_ALIASES = {
    "tv": "televizor", "smart tv": "televizor", "smarttv": "televizor",
    "televizr": "televizor", "kuler": "kuller", "holodilnik": "muzlatgich",
    "pylesos": "changyutgich", "krovat": "karavot", "kovyor": "gilam",
    "spalnya": "yotoqxona", "spalni": "yotoqxona",
}

def product_topics(text):
    q = normalize_text(text)
    out = set()
    for term in PRODUCT_TOPIC_TERMS:
        nt = normalize_text(term)
        if nt in q:
            out.add(PRODUCT_TOPIC_ALIASES.get(nt, nt))
    for token in token_list(text):
        if len(token) < 5:
            continue
        for term in PRODUCT_TOPIC_TERMS:
            nt = normalize_text(term)
            if " " not in nt and len(nt) >= 5 and SequenceMatcher(None, token, nt).ratio() >= 0.92:
                out.add(PRODUCT_TOPIC_ALIASES.get(nt, nt))
                break
    return out

def product_topic_matches(user_text, product):
    topics = product_topics(user_text)
    if not topics:
        return False
    text = normalize_text(" ".join([str(product.get("name", "")), str(product.get("category", "")), str(product.get("description", "")), " ".join(product.get("keywords", []) or [])]))
    return any(topic in text for topic in topics)

def likely_product_query(text):
    q = normalize_text(text)
    if not q:
        return False
    ordinary = {"salom", "assalomu alaykum", "rahmat", "raxmat", "ok", "okay", "ha", "yoq", "xayr", "mayli", "boladi", "tushunarli", "juda yaxshi", "juda zor", "yaxshi", "zor"}
    if q in ordinary:
        return False
    # Faqat aniq mahsulot mavzusi bo'lsa katalogga o'tamiz.
    return bool(product_topics(text))


def is_akso_knowledge_question(text):
    """AKSO do\'koni haqidagi savollarni mahsulot qidiruvidan ajratadi."""
    q = normalize_text(text)
    if not q:
        return False

    # Mijoz mahsulot emas, do\'kon xizmati/shartlari haqida so\'rasa,
    # katalog qidiruvi umuman ishga tushmasligi kerak.
    knowledge_phrases = (
        "hujjat", "dokument", "pasport",
        "muddatli tolov", "bolib tolash", "bolibtolash",
        "tolov sharti", "tolov turi", "ustama",
        "yetkazib berish", "dostavka",
        "montaj", "ornatib ber", "ornatish",
        "kafolat", "garantiya", "qaytarish", "almashtirish",
        "ish vaqti", "nechigacha ishl", "soat nech",
        "manzil", "qayerda", "qayerda joylash",
        "telefon raqam", "telefon",
        "operator", "aksiya", "chegirma",
    )

    return any(phrase in q for phrase in knowledge_phrases)



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

        if not product_topic_matches(user_text, product):
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

                if not product_topic_matches(user_text, product):
                    continue

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
    price = int(product.get("price", 0))
    month_3, month_6, month_12 = calculate_monthly(price)

    parts = [f"🛍 <b>{escape(str(product.get('name', 'Nomsiz')))}</b>"]

    if product.get("category"):
        parts.append(f"🗂 {escape(str(product['category']))}")
    if product.get("description"):
        parts.append(f"\n{escape(str(product['description']))}")

    parts.append(
        "\n📅 <b>Bo'lib to'lash:</b>\n"
        f"• 3 oy — <b>{format_money(month_3)}/oy</b>\n"
        f"• 6 oy — <b>{format_money(month_6)}/oy</b>\n"
        f"• 12 oy — <b>{format_money(month_12)}/oy</b>"
    )
    caption = "\n".join(parts)

    product_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🛒 Buyurtma berish",
            callback_data=f"order:{product.get('id', '')}",
        ),
        InlineKeyboardButton(
            "👨‍💼 Operator",
            callback_data="operator",
        ),
    ]])

    images = [img for img in product_images(product) if img]

    if not images:
        kwargs = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": product_keyboard,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        await bot.send_message(**kwargs)
        return

    # Telegram media group 2-10 ta media qabul qiladi.
    # Bitta rasm bo'lsa send_photo ishlatamiz.
    if len(images) == 1:
        try:
            prepared = await prepare_telegram_photo_source(
                images[0], 1, bot=bot, force_upload=False
            )
            if isinstance(prepared, (bytes, bytearray)):
                prepared = InputFile(bytes(prepared), filename=f"product_1.jpg")
            kwargs = {
                "chat_id": chat_id,
                "photo": prepared,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
            await bot.send_photo(**kwargs)
        except Exception as e:
            print("PRODUCT SINGLE IMAGE XATOSI:", repr(e))
            await send_product_text_fallback(
                bot, chat_id, product, reply_to_message_id=reply_to_message_id
            )
            return
    else:
        # 2-10 ta rasm: Telegram albumi.
        # Avval Telegram file_id'larni to'g'ridan-to'g'ri ishlatamiz,
        # GitHub URL'larni esa botning o'zi yuklab InputFile qiladi.
        # Bu usul ortiqcha Telegram->Telegram download/re-uploadni oldini oladi.
        for chunk_start in range(0, len(images), 10):
            chunk = images[chunk_start:chunk_start + 10]
            media = []

            try:
                for local_index, image in enumerate(chunk):
                    prepared_image = await prepare_telegram_photo_source(
                        image,
                        chunk_start + local_index + 1,
                        bot=bot,
                        force_upload=False,
                    )
                    media_kwargs = {"media": prepared_image}
                    if isinstance(prepared_image, (bytes, bytearray)):
                        media_kwargs["filename"] = f"product_{chunk_start + local_index + 1}.jpg"
                    if chunk_start == 0 and local_index == 0:
                        media_kwargs["caption"] = caption
                        media_kwargs["parse_mode"] = "HTML"
                    media.append(InputMediaPhoto(**media_kwargs))

                send_kwargs = {"chat_id": chat_id, "media": media}
                if reply_to_message_id is not None and chunk_start == 0:
                    send_kwargs["reply_to_message_id"] = reply_to_message_id

                await bot.send_media_group(**send_kwargs)

            except Exception as first_error:
                print("PRODUCT ALBUM XATOSI (direct):", repr(first_error))

                # Ikkinchi urinish: hamma media'ni yangi fayl sifatida yuklaymiz.
                # Bu eski/nomos Telegram file_id yoki aralash media manbalarida yordam beradi.
                try:
                    await asyncio.sleep(0.5)
                    retry_media = []
                    for local_index, image in enumerate(chunk):
                        prepared_image = await prepare_telegram_photo_source(
                            image,
                            chunk_start + local_index + 1,
                            bot=bot,
                            force_upload=True,
                        )
                        media_kwargs = {"media": prepared_image}
                        if chunk_start == 0 and local_index == 0:
                            media_kwargs["caption"] = caption
                            media_kwargs["parse_mode"] = "HTML"
                        retry_media.append(InputMediaPhoto(**media_kwargs))

                    retry_kwargs = {"chat_id": chat_id, "media": retry_media}
                    if reply_to_message_id is not None and chunk_start == 0:
                        retry_kwargs["reply_to_message_id"] = reply_to_message_id
                    await bot.send_media_group(**retry_kwargs)

                except Exception as second_error:
                    print("PRODUCT ALBUM XATOSI (upload retry):", repr(second_error))
                    if chunk_start == 0:
                        await send_product_text_fallback(
                            bot,
                            chat_id,
                            product,
                            reply_to_message_id=reply_to_message_id,
                        )
                    continue

    # Media groupga inline tugmalar biriktirib bo'lmaydi, shuning uchun
    # mahsulot tugmalari albomdan keyin bitta alohida xabarda chiqadi.
    await bot.send_message(
        chat_id=chat_id,
        text="🛒 <b>Mahsulot bo'yicha buyurtma yoki operator bilan bog'lanish:</b>",
        parse_mode="HTML",
        reply_markup=product_keyboard,
    )


async def send_product_text_fallback(bot, chat_id, product, reply_to_message_id=None):
    """Mahsulot rasmini yuborishda xato bo'lsa, hech bo'lmaganda ma'lumotini yuboramiz."""
    try:
        price = int(product.get("price", 0))
    except Exception:
        price = 0

    month_3, month_6, month_12 = calculate_monthly(price)
    parts = [f"🛍 <b>{escape(str(product.get('name', 'Nomsiz')))}</b>"]
    if product.get("category"):
        parts.append(f"🗂 {escape(str(product.get('category')))}")
    if product.get("description"):
        parts.append(f"\n{escape(str(product.get('description')))}")
    if price:
        parts.append(
            "\n📅 <b>Bo'lib to'lash:</b>\n"
            f"• 3 oy — <b>{format_money(month_3)}/oy</b>\n"
            f"• 6 oy — <b>{format_money(month_6)}/oy</b>\n"
            f"• 12 oy — <b>{format_money(month_12)}/oy</b>"
        )

    kwargs = {
        "chat_id": chat_id,
        "text": "\n".join(parts),
        "parse_mode": "HTML",
        "reply_markup": InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🛒 Buyurtma berish",
                callback_data=f"order:{product.get('id', '')}",
            ),
            InlineKeyboardButton(
                "👨‍💼 Operator",
                callback_data="operator",
            ),
        ]]),
    }
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id
    await bot.send_message(**kwargs)


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

    stats["products_viewed"] += len(products)
    failed = 0

    # Har bir mahsulot alohida himoyalangan: bitta mahsulotdagi rasm/API xatosi
    # qolgan mahsulotlarni yuborishni to'xtatmasligi kerak.
    for index, product in enumerate(products):
        if index:
            # Telegram API rate-limitiga yaqinlashmaslik uchun juda kichik tanaffus.
            await asyncio.sleep(0.35)

        try:
            await send_product_to_chat(
                context.bot,
                query.message.chat_id,
                product,
                reply_to_message_id=(
                    query.message.message_id
                    if index == 0 else None
                ),
            )
        except Exception as e:
            failed += 1
            print(
                "PRODUCT SEND XATOSI:",
                repr(e),
                "product_id=",
                product.get("id"),
                "name=",
                product.get("name"),
            )
            try:
                await send_product_text_fallback(
                    context.bot,
                    query.message.chat_id,
                    product,
                    reply_to_message_id=(
                        query.message.message_id
                        if index == 0 else None
                    ),
                )
            except Exception as fallback_error:
                print(
                    "PRODUCT TEXT FALLBACK XATOSI:",
                    repr(fallback_error),
                )

    if failed:
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    f"ℹ️ {failed} ta mahsulot rasmi yuborishda texnik xatolik yuz berdi. "
                    "Qolgan mos mahsulotlar yuborildi."
                ),
            )
        except Exception as e:
            print("PRODUCT ERROR NOTICE XATOSI:", repr(e))


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
        # Har bir rasmning manbasi saqlanadi: photo yoki document.
        # Fayl sifatida yuborilgan rasmlar keyinchalik Telegram file_id emas,
        # GitHub raw URL orqali yuboriladi.
        "image_kinds": [],
        "image_extensions": [],
        "admin_id": user.id,
    }
    admin_draft_checked_users.add(user.id)
    await persist_admin_draft(user.id)

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

        image_bytes_list = state.get(
            "image_bytes_list",
            [],
        )

        telegram_ids = state.get(
            "pending_images",
            [],
        )

        # Draft Render restart/deploydan keyin tiklangan bo'lsa,
        # image_bytes_list bo'sh bo'ladi. Telegram file_id orqali rasmlarni
        # qayta yuklab olamiz.
        if len(image_bytes_list) < len(telegram_ids):
            restored_bytes = []
            for file_id in telegram_ids:
                tg_file = await context.bot.get_file(file_id)
                data = await tg_file.download_as_bytearray()
                restored_bytes.append(bytes(data))
            image_bytes_list = restored_bytes
            state["image_bytes_list"] = restored_bytes
        image_kinds = state.get(
            "image_kinds",
            [],
        )
        image_extensions = state.get(
            "image_extensions",
            [],
        )

        raw_urls = []

        for index, image_bytes in enumerate(
            image_bytes_list,
            start=1,
        ):
            ext = ".jpg"
            if index - 1 < len(image_extensions):
                candidate_ext = image_extensions[index - 1]
                if candidate_ext in (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                ):
                    ext = candidate_ext

            filename = (
                f"{slugify(name)}_"
                f"{int(time.time() * 1000)}_"
                f"{index}{ext}"
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

        # Oddiy Telegram rasmi uchun file_id ishlatiladi.
        # "File" sifatida yuborilgan rasm uchun esa Telegram file_id ni
        # send_photo qabul qilmaydi, shuning uchun GitHub raw URL ishlatiladi.
        product_images_list = []
        for index, telegram_id in enumerate(telegram_ids):
            kind = image_kinds[index] if index < len(image_kinds) else "photo"
            if kind == "document" and index < len(raw_urls):
                product_images_list.append(raw_urls[index])
            else:
                product_images_list.append(telegram_id)

        first_image = product_images_list[0] if product_images_list else ""

        product = {
            "id": str(
                int(time.time() * 1000)
            ),
            "name": name,
            "price": price,
            "category": category,
            "description": description,
            "telegram_file_id": first_image,
            "images": product_images_list,
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

        owner_id = (
            state.get("admin_id")
            or update.message.from_user.id
        )
        await clear_admin_draft(owner_id)

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

        try:
            await persist_admin_draft(
                state.get("admin_id")
                or update.message.from_user.id
            )
        except Exception as persist_error:
            print("ADMIN DRAFT PERSIST XATOSI:", repr(persist_error))

        await update.message.reply_text(
            "❌ Mahsulotni saqlashda xatolik yuz berdi.\n"
            "✅ Mahsulot qo'shish jarayoni saqlab qolindi. "
            "Muammoni tuzatgach qayta davom etishingiz mumkin."
        )


async def show_add_category_selector(update, context, state):
    """Yangi mahsulot uchun mavjud kategoriyani tanlash yoki yangi kategoriya yaratish."""
    products = await github_get_products(force_refresh=True)

    categories = sorted(
        {
            (p.get("category", "") or "").strip()
            for p in products
            if (p.get("category", "") or "").strip()
        },
        key=lambda value: value.lower(),
    )

    keyboard = []
    for index, category in enumerate(categories):
        keyboard.append([
            InlineKeyboardButton(
                f"📁 {category}",
                callback_data=f"addcat:{index}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "➕ Yangi kategoriya yaratish",
            callback_data="addcat:new",
        )
    ])
    keyboard.append([
        InlineKeyboardButton(
            "⏭ Kategoriyasiz saqlash",
            callback_data="addcat:skip",
        )
    ])

    await update.message.reply_text(
        "✅ Mahsulot ma'lumotlari tayyor.\n\n"
        "🗂 <b>Kategoriyani tanlang:</b>\n"
        "Quyidagi mavjud kategoriyalardan birini tanlang yoki yangi kategoriya yarating.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
        state = await restore_admin_draft(user.id)

    if not state:
        return False

    # Add-product jarayonida boshqa handlerlarga umuman o'tmaymiz.
    if state.get("mode") != "add":
        return False

    step = state.get("step")

    if step == "photos":
        photo_file_id = None
        document_file_id = None
        document_ext = ".jpg"

        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
        elif update.message.document:
            document = update.message.document
            mime_type = (document.mime_type or "").lower()
            file_name = (document.file_name or "").lower()

            allowed_exts = (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            )
            guessed_ext = next(
                (ext for ext in allowed_exts if file_name.endswith(ext)),
                ".jpg",
            )

            if mime_type.startswith("image/") or file_name.endswith(allowed_exts):
                document_file_id = document.file_id
                document_ext = guessed_ext

        if not photo_file_id and not document_file_id:
            await update.message.reply_text(
                "📸 Rasm yuboring (oddiy rasm yoki <b>File</b> sifatida) yoki /done bosing.",
                parse_mode="HTML",
            )
            return True

        file_id = photo_file_id or document_file_id
        kind = "photo" if photo_file_id else "document"

        try:
            tg_file = await context.bot.get_file(file_id)

            image_bytes = (
                await tg_file.download_as_bytearray()
            )

            state[
                "pending_images"
            ].append(
                file_id
            )

            state[
                "image_bytes_list"
            ].append(
                bytes(image_bytes)
            )

            state[
                "image_kinds"
            ].append(
                kind
            )

            state[
                "image_extensions"
            ].append(
                document_ext if kind == "document" else ".jpg"
            )

            count = len(
                state["pending_images"]
            )

            source_text = (
                "oddiy rasm"
                if kind == "photo"
                else "fayl sifatidagi rasm"
            )

            await persist_admin_draft(user.id)

            await update.message.reply_text(
                f"✅ {count}-rasm qabul qilindi ({source_text}).\n"
                "Yana rasm yuboring yoki /done bosing."
            )

        except Exception as e:
            print(
                "ADMIN IMAGE XATOSI:",
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
        await persist_admin_draft(user.id)

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
        state["step"] = "description"
        await persist_admin_draft(user.id)

        await update.message.reply_text(
            "✅ Narx saqlandi.\n\n"
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
        state["step"] = "category_selection"
        await persist_admin_draft(user.id)

        await show_add_category_selector(
            update,
            context,
            state,
        )

        return True

    if step == "category_new":
        if not update.message.text:
            await update.message.reply_text(
                "🗂 Yangi kategoriya nomini yozing."
            )
            return True

        category = update.message.text.strip()
        if not category:
            await update.message.reply_text(
                "❌ Kategoriya nomi bo'sh bo'lishi mumkin emas."
            )
            return True

        state["category"] = category
        await persist_admin_draft(user.id)
        await save_new_product(
            update,
            context,
            state,
        )
        return True

    if step == "category_selection":
        await update.message.reply_text(
            "🗂 Kategoriya tanlash uchun quyidagi tugmalardan foydalaning."
        )
        return True

    return False


async def save_edited_product_images(
    update,
    context,
    state,
):
    if not update.message:
        return

    user = update.message.from_user
    if not user or not is_admin(user.id):
        return

    image_bytes_list = state.get("edit_image_bytes_list", [])
    image_extensions = state.get("edit_image_extensions", [])

    if not image_bytes_list:
        await update.message.reply_text(
            "📸 Kamida 1 ta yangi rasm yuboring."
        )
        return

    product_id = state.get("product_id")
    products = await github_get_products(force_refresh=True)
    product = next(
        (p for p in products if p.get("id") == product_id),
        None,
    )

    if not product:
        admin_states.pop(user.id, None)
        await update.message.reply_text("❌ Mahsulot topilmadi.")
        return

    name = str(product.get("name", "product"))
    await update.message.reply_text("⏳ Yangi rasmlar GitHub'ga yuklanmoqda...")

    raw_urls = []
    timestamp = int(time.time() * 1000)

    try:
        for index, image_bytes in enumerate(image_bytes_list, start=1):
            ext = ".jpg"
            if index - 1 < len(image_extensions):
                candidate = image_extensions[index - 1]
                if candidate in (".jpg", ".jpeg", ".png", ".webp"):
                    ext = candidate

            filename = f"{slugify(name)}_edit_{timestamp}_{index}{ext}"
            github_path = f"{PRODUCTS_FOLDER}/{filename}"

            await github_upload_file(
                github_path,
                image_bytes,
                f"Update product images: {name}",
            )

            raw_urls.append(
                "https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/{github_path}"
            )

        if not raw_urls:
            raise ValueError("new image URLs were not created")

        product["images"] = raw_urls
        product["raw_urls"] = raw_urls
        product["raw_url"] = raw_urls[0]
        product["telegram_file_id"] = raw_urls[0]

        await github_save_products(products)
        stats["products_edited"] += 1
        admin_states.pop(user.id, None)

        await update.message.reply_text(
            "✅ <b>Mahsulot rasmlari muvaffaqiyatli o\'zgartirildi!</b>\n\n"
            f"🛍 <b>{escape(name)}</b>\n"
            f"🖼 Yangi rasmlar: <b>{len(raw_urls)} ta</b>\n"
            "📦 Eski rasmlar o\'rniga yangi rasmlar saqlandi.",
            parse_mode="HTML",
        )
    except Exception as e:
        print("EDIT PRODUCT IMAGES XATOSI:", repr(e))
        await update.message.reply_text(
            "❌ Yangi rasmlarni saqlashda xatolik yuz berdi.\n"
            "Eski rasmlar o\'zgartirilmagan."
        )


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
        state = await restore_admin_draft(user.id)

    if not state:
        await update.message.reply_text(
            "ℹ️ Hozir mahsulot qo'shish yoki tahrirlash jarayoni yo'q."
        )
        return

    if state.get("mode") == "edit" and state.get("step") == "edit_images":
        await save_edited_product_images(update, context, state)
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
    await persist_admin_draft(user.id)

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
        # Eski holat bilan moslik: agar shu bosqich qolib ketgan bo'lsa, kategoriyasiz o'tamiz.
        state["category"] = ""
        state["step"] = "description"
        await persist_admin_draft(user.id)

        await update.message.reply_text(
            "✅ Kategoriya o'tkazildi.\n\n"
            "📄 Tavsif yozing yoki /skip bosing."
        )
        return

    if step == "description":
        state["description"] = ""
        state["step"] = "category_selection"
        await persist_admin_draft(user.id)

        await show_add_category_selector(
            update,
            context,
            state,
        )
        return

    if step == "category_selection":
        state["category"] = ""
        await save_new_product(
            update,
            context,
            state,
        )
        return

    if step == "category_new":
        state["category"] = ""
        await save_new_product(
            update,
            context,
            state,
        )
        return


# ============================================================
# ADMIN MANAGEMENT (OWNER ONLY)
# ============================================================

async def add_admin_command(update, context):
    if not update.message:
        return
    user = update.message.from_user
    if not user or not is_owner(user.id):
        await update.message.reply_text("❌ Yangi adminni faqat bot egasi tayinlay oladi.")
        return

    if not context.args:
        await update.message.reply_text(
            "👑 <b>Admin tayinlash</b>\n\n"
            "Yangi adminning Telegram ID raqamini yuboring.\n\n"
            "Masalan: <code>/addadmin 123456789</code>\n\n"
            "ID olish uchun foydalanuvchi botga /id yuborishi mumkin.",
            parse_mode="HTML",
        )
        return

    raw = context.args[0].strip()
    if not raw.isdigit():
        await update.message.reply_text(
            "❌ Telegram ID faqat raqamlardan iborat bo'lishi kerak.\n"
            "Masalan: <code>/addadmin 123456789</code>",
            parse_mode="HTML",
        )
        return

    new_id = int(raw)
    if new_id == ADMIN_ID:
        await update.message.reply_text("👑 Bu ID allaqachon bot egasiga tegishli.")
        return
    if new_id in additional_admin_ids:
        await update.message.reply_text("ℹ️ Bu foydalanuvchi allaqachon admin.")
        return

    additional_admin_ids.add(new_id)
    try:
        await github_save_admin_ids(additional_admin_ids)
    except Exception as e:
        additional_admin_ids.discard(new_id)
        print("ADMIN QO'SHISH XATOSI:", repr(e))
        await update.message.reply_text(
            "❌ Adminni saqlashda xatolik yuz berdi. GitHub ulanishini tekshiring."
        )
        return

    await update.message.reply_text(
        "✅ <b>Yangi admin tayinlandi.</b>\n\n"
        f"🆔 Telegram ID: <code>{new_id}</code>\n\n"
        "U botga /start yuborgandan keyin admin panelidan foydalanishi mumkin.",
        parse_mode="HTML",
    )


async def remove_admin_command(update, context):
    if not update.message:
        return
    user = update.message.from_user
    if not user or not is_owner(user.id):
        await update.message.reply_text("❌ Adminni olib tashlashni faqat bot egasi amalga oshiradi.")
        return

    if not context.args or not context.args[0].strip().isdigit():
        await update.message.reply_text(
            "🗑 <b>Adminni olib tashlash</b>\n\n"
            "Masalan: <code>/removeadmin 123456789</code>",
            parse_mode="HTML",
        )
        return

    old_id = int(context.args[0])
    if old_id == ADMIN_ID:
        await update.message.reply_text("❌ Bot egasini olib tashlab bo'lmaydi.")
        return
    if old_id not in additional_admin_ids:
        await update.message.reply_text("ℹ️ Bu ID qo'shimcha adminlar ro'yxatida yo'q.")
        return

    additional_admin_ids.discard(old_id)
    try:
        await github_save_admin_ids(additional_admin_ids)
    except Exception as e:
        additional_admin_ids.add(old_id)
        print("ADMIN OLIB TASHLASH XATOSI:", repr(e))
        await update.message.reply_text("❌ Adminni ro'yxatdan o'chirishda xatolik yuz berdi.")
        return

    await update.message.reply_text(
        f"✅ <b>Admin olib tashlandi.</b>\n\n🆔 Telegram ID: <code>{old_id}</code>",
        parse_mode="HTML",
    )


async def admins_command(update, context):
    if not update.message:
        return
    user = update.message.from_user
    if not user or not is_owner(user.id):
        await update.message.reply_text("❌ Adminlar ro'yxatini faqat bot egasi ko'ra oladi.")
        return

    lines = [
        "👑 <b>AKSO administratorlari</b>",
        "",
        f"1. 👑 Bot egasi — <code>{ADMIN_ID}</code>",
    ]
    for idx, admin_id in enumerate(sorted(additional_admin_ids), start=2):
        lines.append(f"{idx}. 👤 Admin — <code>{admin_id}</code>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


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
        f"<b>{stats['catalog_matches']}</b>\n"
        f"👁 Ko'rilgan: <b>{stats['products_viewed']}</b>\n"
        f"❓ Topilmagan so'rovlar: <b>{stats['not_found_queries']}</b>\n"
        f"🛒 Buyurtmalar: <b>{stats['orders']}</b>\n"
        f"👨‍💼 Operator so'rovlari: <b>{stats['operator_requests']}</b>\n\n"
        f"➕ Qo'shilgan: "
        f"<b>{stats['products_added']}</b>\n"
        f"✏️ Tahrirlangan: "
        f"<b>{stats['products_edited']}</b>\n"
        f"🗑 O'chirilgan: "
        f"<b>{stats['products_deleted']}</b>",
        parse_mode="HTML",
    )


# ============================================================
# ORDERS / OPERATOR
# ============================================================

async def github_get_orders():
    try:
        result=await github_api("GET",f"{ORDERS_FILE}?ref={GITHUB_BRANCH}")
        content=result.get("content","")
        if not content: return []
        data=json.loads(base64.b64decode(content.replace("\n","")).decode("utf-8"))
        return data if isinstance(data,list) else []
    except Exception as e:
        if "404" in str(e): return []
        raise


async def github_save_orders(orders):
    encoded=base64.b64encode(json.dumps(orders,ensure_ascii=False,indent=2).encode("utf-8")).decode("ascii")
    sha=None
    try:
        sha=(await github_api("GET",f"{ORDERS_FILE}?ref={GITHUB_BRANCH}")).get("sha")
    except Exception as e:
        if "404" not in str(e): raise
    payload={"message":"Update AKSO orders","content":encoded,"branch":GITHUB_BRANCH}
    if sha: payload["sha"]=sha
    await github_api("PUT",ORDERS_FILE,payload)


async def notify_operator(context, chat_id, user_id, full_name, username, history):
    stats["operator_requests"] += 1
    username_text = f"@{username}" if username else "username yo'q"
    text = (
        "🔔 <b>OPERATOR SO'ROVI</b>\n\n"
        f"👤 {full_name}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📲 {username_text}\n"
        f"💬 Chat ID: <code>{chat_id}</code>\n\n"
        "🧠 So'nggi suhbat:\n"
        f"{history[-2500:] if history else "yoq"}"
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        print("OPERATOR XATOSI:", repr(e))
        return False


async def operator_request(update, context):
    if not update.message: return
    user = update.message.from_user
    if not user: return
    key = (update.message.chat_id, user.id)
    sent = await notify_operator(
        context, update.message.chat_id, user.id, user.full_name, user.username,
        get_conversation_context(key)
    )
    if sent:
        await update.message.reply_text(
            "👨‍💼 So'rovingiz operatorga yuborildi.\n"
            f"📞 {AKSO_KNOWLEDGE['operator']}"
        )
    else:
        await update.message.reply_text(
            "⚠️ Operatorga ulanishda vaqtinchalik muammo bo'ldi.\n"
            f"📞 {AKSO_KNOWLEDGE['operator']} raqamiga murojaat qiling."
        )


async def start_order_from_button(update, context, product_id):
    query = update.callback_query
    user = query.from_user
    products = await github_get_products()
    product = next((p for p in products if p.get("id") == product_id and visible_product(p)), None)
    if not product:
        await query.message.reply_text("❌ Bu mahsulot hozir mavjud emas.")
        return
    order_states[user.id] = {
        "user_id": user.id,
        "user_name": user.full_name,
        "username": user.username,
        "chat_id": query.message.chat_id,
        "product_id": product_id,
        "product_name": product.get("name", "Nomsiz"),
        "step": "name",
        "created_at": time.time(),
    }
    await query.message.reply_text(
        f"🛒 <b>{product.get('name', 'Mahsulot')}</b> uchun buyurtmani boshlaymiz.\n\n"
        "1️⃣ Ism-familiyangizni yozing:",
        parse_mode="HTML",
    )


async def finalize_order_from_state(update, context, state):
    orders = await github_get_orders()
    num = 1001 + len(orders)
    order = {
        "id": f"AKSO-{num}",
        "created_at": int(time.time()),
        "chat_id": state.get("chat_id"),
        "user_id": state.get("user_id"),
        "name": state.get("name", ""),
        "phone": state.get("phone", ""),
        "location": state.get("location", ""),
        "product_id": state.get("product_id", ""),
        "product_name": state.get("product_name", ""),
        "payment": state.get("payment", ""),
        "installment_months": state.get("installment_months"),
        "status": "new",
    }
    orders.append(order)
    try:
        await github_save_orders(orders)
    except Exception as e:
        print("ORDER SAVE XATOSI:", repr(e))
        await update.message.reply_text(
            "❌ Buyurtmani saqlashda texnik xatolik yuz berdi. Operatorga murojaat qiling."
        )
        return
    stats["orders"] += 1
    order_states.pop(state.get("user_id"), None)
    admin = (
        "🛍 <b>YANGI BUYURTMA</b>\n\n"
        f"🔖 <b>{order['id']}</b>\n"
        f"👤 {order['name']}\n"
        f"📞 {order['phone']}\n"
        f"📍 {order['location']}\n"
        f"📦 {order['product_name']}\n"
        f"💳 {order['payment']}"
        + (f"\n📅 {order['installment_months']} oy" if order.get("installment_months") else "")
        + f"\n🆔 Chat: <code>{order['chat_id']}</code>"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin, parse_mode="HTML")
    await update.message.reply_text(
        f"✅ <b>Buyurtmangiz qabul qilindi!</b>\n\n"
        f"🔖 {order['id']}\n"
        f"📦 {order['product_name']}\n"
        "Tez orada operator siz bilan bog'lanadi.",
        parse_mode="HTML",
    )


async def handle_order_state(update,context):
    if not update.message or not update.message.text: return False
    user=update.message.from_user
    if not user: return False
    state=order_states.get(user.id)
    if not state or state.get("chat_id")!=update.message.chat_id: return False
    if time.time()-state.get("created_at",0)>1800:
        order_states.pop(user.id,None); await update.message.reply_text("⏳ Buyurtma jarayoni eskirgan. Yangidan boshlang."); return True
    text=update.message.text.strip(); step=state.get("step")
    if step=="name":
        state["name"]=text; state["step"]="phone"; await update.message.reply_text("2️⃣ Telefon raqamingizni yozing.\nMasalan: +998901234567"); return True
    if step=="phone":
        if len(re.sub(r"\\D","",text))<9: await update.message.reply_text("📞 Telefon raqam noto'g'ri. Qaytadan yuboring."); return True
        state["phone"]=text; state["step"]="location"; await update.message.reply_text("3️⃣ Manzil yoki qaysi tumanligini yozing. Qorako'l/Olot bo'lsa yetkazib berish bepul."); return True
    if step=="location":
        state["location"]=text; state["step"]="payment"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("💵 Naqd",callback_data="pay:cash"),InlineKeyboardButton("💳 Karta",callback_data="pay:card")],[InlineKeyboardButton("📅 Bo'lib to'lash",callback_data="pay:installment")]])
        await update.message.reply_text("4️⃣ To'lov turini tanlang:",reply_markup=kb); return True
    if step=="installment_months":
        months=extract_months(text)
        if months not in (3,6,12): await update.message.reply_text("📅 3 oy, 6 oy yoki 12 oy deb yozing."); return True
        state["installment_months"]=months; await finalize_order_from_state(update,context,state); return True
    return False


async def sales_callback_handler(update,context):
    query=update.callback_query
    if not query: return
    data=query.data or ""
    if data=="operator":
        await query.answer("👨‍💼 Operatorga yuborilmoqda...")
        user=query.from_user
        key=(query.message.chat_id,user.id)
        sent=await notify_operator(context,query.message.chat_id,user.id,user.full_name,user.username,get_conversation_context(key))
        if sent:
            await query.message.reply_text(f"👨‍💼 So'rovingiz operatorga yuborildi.\n📞 {AKSO_KNOWLEDGE['operator']}")
        else:
            await query.message.reply_text(f"⚠️ Operatorga ulanishda muammo bo'ldi.\n📞 {AKSO_KNOWLEDGE['operator']}")
        return
    if data.startswith("order:"):
        await query.answer("🛒 Buyurtma boshlandi")
        await start_order_from_button(update,context,data.split(":",1)[1]); return
    if data.startswith("pay:"):
        state=order_states.get(query.from_user.id)
        if not state or state.get("chat_id")!=query.message.chat_id:
            await query.answer("⏳ Buyurtma oynasi eskirgan.",show_alert=True); return
        p=data.split(":",1)[1]; state["payment"]={"cash":"Naqd","card":"Karta","installment":"Bo'lib to'lash"}.get(p,p)
        await query.answer()
        if p=="installment":
            state["step"]="installment_months"; await query.message.reply_text("5️⃣ Necha oyga bo'lib to'lashni xohlaysiz? 3 oy, 6 oy yoki 12 oy deb yozing.")
        else:
            fake=type("U",(),{"message":query.message})(); await finalize_order_from_state(fake,context,state)


def format_order_date(timestamp):
    try:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=5))
        return datetime.fromtimestamp(int(timestamp), tz=tz).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def order_status_label(status):
    status = str(status or "new").strip().lower()
    return {
        "new": "🆕 Yangi",
        "processing": "⚙️ Jarayonda",
        "confirmed": "✅ Tasdiqlangan",
        "completed": "📦 Yakunlangan",
        "cancelled": "❌ Bekor qilingan",
    }.get(status, f"📌 {status}")


def build_orders_list_markup(orders, page=0, per_page=8):
    total = len(orders)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    items = list(reversed(orders))[start:start + per_page]

    keyboard = []
    for o in items:
        oid = str(o.get("id", "NOMA'LUM"))
        product = str(o.get("product_name", "Mahsulot"))
        customer = str(o.get("name", "Mijoz"))
        label_product = product[:26] + ("…" if len(product) > 26 else "")
        label_customer = customer[:18] + ("…" if len(customer) > 18 else "")
        keyboard.append([
            InlineKeyboardButton(
                f"🔖 {oid} | 👤 {label_customer}",
                callback_data=f"ordview:{oid}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"ordpage:{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="ordnoop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"ordpage:{page+1}"))
    keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)


def build_orders_list_text(orders, page=0, per_page=8):
    total = len(orders)
    counts = {}
    for o in orders:
        key = str(o.get("status", "new")).lower()
        counts[key] = counts.get(key, 0) + 1

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    items = list(reversed(orders))[page * per_page:(page + 1) * per_page]

    text = (
        "🛒 <b>AKSO BUYURTMALARI</b>\n\n"
        f"📦 Jami: <b>{total}</b> ta\n"
        f"🆕 Yangi: <b>{counts.get('new', 0)}</b>  |  "
        f"⚙️ Jarayonda: <b>{counts.get('processing', 0)}</b>  |  "
        f"✅ Yakunlangan: <b>{counts.get('completed', 0)}</b>\n\n"
        "👇 Buyurtmani batafsil ko‘rish uchun tugmani bosing.\n"
    )

    for idx, o in enumerate(items, 1):
        text += (
            f"\n<b>{idx}.</b> 🔖 <b>{escape(str(o.get('id', '—')))}</b> "
            f"— {order_status_label(o.get('status'))}\n"
            f"   👤 {escape(str(o.get('name', '—')))}\n"
            f"   📦 {escape(str(o.get('product_name', '—')))}\n"
            f"   🕐 {format_order_date(o.get('created_at'))}"
        )

    return text


async def send_orders_page(target, orders, page=0):
    text = build_orders_list_text(orders, page=page)
    markup = build_orders_list_markup(orders, page=page)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def orders_command(update, context):
    if not update.message:
        return
    user = update.message.from_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Sizda ruxsat yo‘q.")
        return

    orders = await github_get_orders()
    if not orders:
        await update.message.reply_text(
            "🛒 <b>Buyurtmalar</b>\n\nHozircha buyurtmalar yo‘q.",
            parse_mode="HTML"
        )
        return

    await send_orders_page(update.message, orders, page=0)


async def show_order_detail(query, order):
    text = (
        "🔖 <b>BUYURTMA MA’LUMOTLARI</b>\n\n"
        f"🆔 Buyurtma: <b>{escape(str(order.get('id', '—')))}</b>\n"
        f"📌 Holati: {order_status_label(order.get('status'))}\n"
        f"🕐 Sana: <b>{format_order_date(order.get('created_at'))}</b>\n\n"
        f"👤 <b>Mijoz:</b> {escape(str(order.get('name', '—')))}\n"
        f"📞 <b>Telefon:</b> {escape(str(order.get('phone', '—')))}\n"
        f"📍 <b>Manzil:</b> {escape(str(order.get('location', '—')))}\n\n"
        f"📦 <b>Mahsulot:</b> {escape(str(order.get('product_name', '—')))}\n"
        f"💳 <b>To‘lov:</b> {escape(str(order.get('payment', '—')))}"
    )
    if order.get("installment_months"):
        text += f"\n📅 <b>Muddat:</b> {escape(str(order.get('installment_months')))} oy"
    text += f"\n🆔 <b>Chat ID:</b> <code>{escape(str(order.get('chat_id', '—')))}</code>"

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Buyurtmalar ro‘yxati", callback_data="ordback:0")
    ]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


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

    # -------- admin orders UI --------
    if data in {"ordnoop"} or data.startswith(("ordpage:", "ordview:", "ordback:")):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Sizda ruxsat yo‘q.", show_alert=True)
            return

        orders = await github_get_orders()
        if data == "ordnoop":
            await query.answer()
            return

        if data.startswith("ordpage:") or data.startswith("ordback:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            if not orders:
                await query.edit_message_text("🛒 Hozircha buyurtmalar yo‘q.")
                await query.answer()
                return
            await query.answer()
            await send_orders_page(query, orders, page=page)
            return

        oid = data.split(":", 1)[1]
        order = next((o for o in orders if str(o.get("id")) == str(oid)), None)
        if not order:
            await query.answer("❌ Buyurtma topilmadi.", show_alert=True)
            return
        await query.answer()
        await show_order_detail(query, order)
        return

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

    # -------- add product category callbacks --------

    if data.startswith("addcat:"):
        if not is_admin(query.from_user.id):
            await query.answer(
                "❌ Sizda ruxsat yo'q.",
                show_alert=True,
            )
            return

        state = admin_states.get(query.from_user.id)
        if not state:
            state = await restore_admin_draft(query.from_user.id)
        if not state or state.get("mode") != "add" or state.get("step") != "category_selection":
            await query.answer(
                "⏳ Bu mahsulot qo'shish oynasi eskirgan.",
                show_alert=True,
            )
            return

        action = data.split(":", 1)[1]

        if action == "new":
            await query.answer()
            state["step"] = "category_new"
            await persist_admin_draft(query.from_user.id)
            await query.message.reply_text(
                "➕ <b>Yangi kategoriya</b>\n\n"
                "🗂 Yangi kategoriya nomini yozing.",
                parse_mode="HTML",
            )
            return

        if action == "skip":
            await query.answer("Kategoriyasiz saqlanmoqda...")
            state["category"] = ""
            await persist_admin_draft(query.from_user.id)
            await save_new_product(query, context, state)
            return

        try:
            index = int(action)
        except ValueError:
            await query.answer("❌ Noto'g'ri kategoriya.", show_alert=True)
            return

        products = await github_get_products(force_refresh=True)
        categories = sorted(
            {
                (p.get("category", "") or "").strip()
                for p in products
                if (p.get("category", "") or "").strip()
            },
            key=lambda value: value.lower(),
        )

        if not 0 <= index < len(categories):
            await query.answer("❌ Kategoriya topilmadi.", show_alert=True)
            return

        state["category"] = categories[index]
        await persist_admin_draft(query.from_user.id)
        await query.answer("✅ Kategoriya tanlandi")
        await save_new_product(query, context, state)
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
                    f"🖼 Rasmlarini o\'zgartirish ({len(product_images(product))} ta)",
                    callback_data=(
                        f"edfield:{product_id}:images"
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

        if field == "images":
            admin_states[query.from_user.id] = {
                "mode": "edit",
                "step": "edit_images",
                "product_id": product_id,
                "field": field,
                "edit_image_bytes_list": [],
                "edit_image_extensions": [],
            }

            await query.message.reply_text(
                "🖼 <b>Mahsulot rasmlarini o\'zgartirish</b>\n\n"
                f"Hozirgi rasmlar: <b>{len(product_images(product))} ta</b>.\n\n"
                "📸 Yangi 1 yoki bir nechta rasm yuboring.\n"
                "✅ Saqlanganda eski rasmlar yangi rasmlar bilan to\'liq almashtiriladi.\n\n"
                "Rasmlar tugagach: /done\n"
                "Bekor qilish: /cancel",
                parse_mode="HTML",
            )
            return

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
            categories[category],
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

    if step == "edit_images":
        photo_file_id = None
        document_file_id = None
        document_ext = ".jpg"

        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
        elif update.message.document:
            document = update.message.document
            mime_type = (document.mime_type or "").lower()
            file_name = (document.file_name or "").lower()
            allowed_exts = (".jpg", ".jpeg", ".png", ".webp")
            guessed_ext = next(
                (ext for ext in allowed_exts if file_name.endswith(ext)),
                ".jpg",
            )
            if mime_type.startswith("image/") or file_name.endswith(allowed_exts):
                document_file_id = document.file_id
                document_ext = guessed_ext

        if not photo_file_id and not document_file_id:
            await update.message.reply_text(
                "📸 Rasm yuboring (oddiy rasm yoki File sifatida).\n"
                "Rasmlar tugagach /done bosing."
            )
            return True

        file_id = photo_file_id or document_file_id
        try:
            tg_file = await context.bot.get_file(file_id)
            image_bytes = await tg_file.download_as_bytearray()
            state.setdefault("edit_image_bytes_list", []).append(bytes(image_bytes))
            state.setdefault("edit_image_extensions", []).append(
                ".jpg" if photo_file_id else document_ext
            )
            count = len(state["edit_image_bytes_list"])
            source_text = "oddiy rasm" if photo_file_id else "fayl sifatidagi rasm"
            await update.message.reply_text(
                f"✅ {count}-rasm qabul qilindi ({source_text}).\n"
                "Yana rasm yuboring yoki /done bosing."
            )
        except Exception as e:
            print("EDIT IMAGE RECEIVE XATOSI:", repr(e))
            await update.message.reply_text(
                "❌ Rasmni qabul qilishda xatolik yuz berdi. Yana bir bor yuboring."
            )
        return True

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
            KeyboardButton("🏪 AKSO haqida"),
            KeyboardButton("🛒 Buyurtma berish"),
        ],
        [
            KeyboardButton("👨‍💼 Operator"),
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
            KeyboardButton("🏪 AKSO haqida"),
        ],
        [
            KeyboardButton("🛒 Buyurtmalar"),
            KeyboardButton("👥 Foydalanuvchilar"),
        ],
        [
            KeyboardButton("❓ Yordam"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


async def handle_menu_button(update, context):
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
            "🏪 AKSO haqida": store_command,
            "🛒 Buyurtmalar": orders_command,
            "👥 Foydalanuvchilar": users_command,
            "👨‍💼 Operator": operator_request,
            "❓ Yordam": help_command,
        }
        action = admin_actions.get(text)
        if action:
            await action(update, context)
            return True

    customer_actions = {
        "🛍 Mahsulotlar": catalog_command,
        "🔎 Mahsulot qidirish": help_command,
        "🏪 AKSO haqida": store_command,
        "🛒 Buyurtma berish": help_command,
        "👨‍💼 Operator": operator_request,
        "❓ Yordam": help_command,
    }
    action = customer_actions.get(text)
    if action:
        await action(update, context)
        return True

    return False


async def show_main_keyboard(update):
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


# ============================================================
# ============================================================
# /STORE
# ============================================================

async def store_command(update, context):
    if not update.message:
        return

    await update.message.reply_text(
        "🏪 <b>AKSO do'koni</b>\n\n"
        f"📍 Manzil: <b>{AKSO_KNOWLEDGE['address']}</b>\n"
        f"🧭 Mo'ljal: <b>{AKSO_KNOWLEDGE['landmark']}</b>\n"
        f"🕐 Ish vaqti: <b>{AKSO_KNOWLEDGE['working_days']}, {AKSO_KNOWLEDGE['working_hours']}</b>\n"
        f"📞 Telefon: <b>{AKSO_KNOWLEDGE['phone']}</b>\n"
        f"📲 Telegram: <b>{AKSO_KNOWLEDGE['telegram']}</b>\n"
        f"📸 Instagram: <b>{AKSO_KNOWLEDGE['instagram']}</b>\n"
        f"🌐 Sayt: <b>{AKSO_KNOWLEDGE['website']}</b>\n\n"
        "🚚 Qorako'l va Olot bo'ylab yetkazib berish — <b>bepul</b>.\n"
        "🛠 Barcha mahsulotlarga montaj — <b>bepul</b>.\n"
        "🛡 Kafolat — <b>10 yilgacha</b>.",
        parse_mode="HTML",
    )


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
    if user and is_admin(user.id):
        try:
            await setup_admin_commands_for_chat(context.application, user.id)
        except Exception as e:
            print("ADMIN COMMAND MENU XATOSI:", repr(e))

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


async def users_command(update, context):
    if not update.message:
        return
    user = update.message.from_user
    if not user or not is_owner(user.id):
        await update.message.reply_text("❌ Chat tarixini faqat bot egasi ko'ra oladi.")
        return
    await show_users_page(update, context, 0, edit=False)


def _sorted_history_users():
    items = []
    for key, item in chat_history.items():
        try:
            uid = int(item.get("user_id"))
        except Exception:
            continue
        if uid == ADMIN_ID:
            continue
        if not item.get("messages"):
            continue
        items.append((key, item))
    items.sort(key=lambda x: x[1].get("last_activity", 0), reverse=True)
    return items


async def show_users_page(update, context, page=0, edit=False):
    users = _sorted_history_users()
    total_pages = max(1, (len(users) + 7) // 8)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * 8:(page + 1) * 8]

    if not chunk:
        text = (
            "👥 <b>Foydalanuvchilar</b>\n\n"
            "Hozircha yozishma tarixi mavjud emas."
        )
        keyboard = [[
            InlineKeyboardButton(
                "🔄 Yangilash",
                callback_data="hist:list:0",
            )
        ]]
    else:
        lines = [
            f"👥 <b>Foydalanuvchilar</b> — {len(users)} ta",
            "",
            "Kerakli foydalanuvchini tanlang:",
        ]
        keyboard = []
        for index, (_, item) in enumerate(chunk, start=1):
            name = item.get("full_name") or "Noma'lum"
            username = item.get("username")
            label = f"{index}. {name}"
            if username:
                label += f" (@{username})"
            keyboard.append([
                InlineKeyboardButton(
                    label[:60],
                    callback_data=(
                        f"hist:user:{item.get('chat_id')}"
                        f":{item.get('user_id')}:0"
                    ),
                )
            ])
        text = "\n".join(lines)
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "⬅️ Oldingi",
                    callback_data=f"hist:list:{page - 1}",
                )
            )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "Keyingi ➡️",
                    callback_data=f"hist:list:{page + 1}",
                )
            )
        if nav:
            keyboard.append(nav)

    markup = InlineKeyboardMarkup(keyboard)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )


def _history_item_by_ids(chat_id, user_id):
    return chat_history.get(f"{chat_id}:{user_id}")


async def show_history_page(update, context, chat_id, user_id, page=0):
    query = update.callback_query
    item = _history_item_by_ids(chat_id, user_id)
    if not item:
        await query.edit_message_text(
            "❌ Bu foydalanuvchi uchun tarix topilmadi."
        )
        return

    messages = item.get("messages") or []
    total_pages = max(
        1,
        (len(messages) + HISTORY_PAGE_SIZE - 1)
        // HISTORY_PAGE_SIZE,
    )
    page = max(0, min(page, total_pages - 1))
    start = page * HISTORY_PAGE_SIZE
    chunk = messages[start:start + HISTORY_PAGE_SIZE]

    name = escape(item.get("full_name") or "Noma'lum")
    username = item.get("username")
    username_text = (
        f"@{escape(username)}"
        if username
        else "username yo'q"
    )

    header = (
        "👤 <b>Foydalanuvchi tarixi</b>\n\n"
        f"Ism: <b>{name}</b>\n"
        f"Username: <b>{username_text}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
    )

    lines = []
    for entry in chunk:
        role = entry.get("role")
        who = "👤 Mijoz" if role == "user" else "🤖 AKSO"
        stamp = entry.get("time")
        if stamp:
            tm = time.strftime(
                "%d.%m.%Y %H:%M",
                time.localtime(stamp),
            )
            stamp_text = f" [{tm}]"
        else:
            stamp_text = ""

        msg = escape(str(entry.get("text", "")))
        if len(msg) > 700:
            msg = msg[:700] + "…"
        lines.append(
            f"{who}{stamp_text}:\n{msg}"
        )

    body = (
        "\n\n".join(lines)
        if lines
        else "Tarix bo'sh."
    )
    text = header + body
    if len(text) > 3900:
        text = text[:3900] + "\n…"

    keyboard = []
    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                "⬅️ Oldingi",
                callback_data=(
                    f"hist:user:{chat_id}:{user_id}:{page - 1}"
                ),
            )
        )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                "Keyingi ➡️",
                callback_data=(
                    f"hist:user:{chat_id}:{user_id}:{page + 1}"
                ),
            )
        )
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([
        InlineKeyboardButton(
            "👥 Foydalanuvchilar",
            callback_data="hist:list:0",
        )
    ])

    await query.edit_message_text(
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_callback_handler(update, context):
    query = update.callback_query
    if not query:
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "⛔ Faqat bot egasi foydalanishi mumkin.",
            show_alert=True,
        )
        return

    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    try:
        if len(parts) >= 3 and parts[1] == "list":
            await show_users_page(
                update,
                context,
                int(parts[2]),
                edit=True,
            )
            return

        if len(parts) >= 5 and parts[1] == "user":
            chat_id = int(parts[2])
            user_id = int(parts[3])
            page = int(parts[4])
            await show_history_page(
                update,
                context,
                chat_id,
                user_id,
                page,
            )
            return
    except Exception as e:
        print("HISTORY CALLBACK XATOSI:", repr(e))
        await query.edit_message_text(
            "❌ Tarixni ochishda xatolik yuz berdi."
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
        await clear_admin_draft(user.id)

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

    # Guruhda Telegram adminlari yozgan oddiy xabarlarga bot javob bermaydi.
    # Private chatdagi admin yozishmalari esa odatdagidek ishlaydi.
    # Adminni faqat get_chat_member orqali tekshirish ba'zi holatlarda yetarli
    # bo'lmaydi, shuning uchun guruh administratorlari ro'yxatini ham tekshiramiz.
    if update.message.chat.type in ("group", "supergroup") and update.message.from_user:
        chat_id = update.message.chat_id
        user_id = update.message.from_user.id

        try:
            administrators = await context.bot.get_chat_administrators(chat_id)
            admin_ids = {member.user.id for member in administrators if member.user}
            if user_id in admin_ids:
                return
        except Exception as e:
            print("GROUP ADMIN LIST TEKSHIRUV XATOSI:", repr(e))

            # Zaxira tekshiruv.
            try:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if getattr(member, "status", "") in ("administrator", "creator", "owner"):
                    return
            except Exception as inner_e:
                print("GROUP ADMIN MEMBER TEKSHIRUV XATOSI:", repr(inner_e))
                # Adminni aniqlay olmasak, guruhdagi xabarga javob bermaymiz.
                # Bu admin xabariga tasodifan javob berishning oldini oladi.
                return

    stats["messages"] += 1

    # --------------------------------------------------------
    # ADMIN WORKFLOW — MUTLAQ USTUVORLIK
    # --------------------------------------------------------
    # Admin /addproduct yoki /editproduct jarayonida turgan bo'lsa,
    # uning matni HECH QACHON mijoz mahsulot qidiruviga tushmaydi.
    if update.message.from_user and is_admin(update.message.from_user.id):
        owner_id = update.message.from_user.id
        admin_state = admin_states.get(owner_id)

        if not admin_state:
            admin_state = await restore_admin_draft(owner_id)

        if admin_state and admin_state.get("mode") == "add":
            if await handle_admin_state(update, context):
                return
            await update.message.reply_text(
                "⏳ Mahsulot qo'shish jarayoni davom etmoqda. "
                "Iltimos, bot so'ragan ma'lumotni yuboring yoki /cancel bosing."
            )
            return

        if admin_state and admin_state.get("mode") == "edit":
            if await handle_edit_state(update, context):
                return

    # Reply keyboard tugmalari hech qachon mahsulot qidiruvi sifatida
    # qabul qilinmasin.
    if await handle_menu_button(update, context):
        return

    # Buyurtma jarayoni.
    if await handle_order_state(update, context):
        return

    # Admin tahrirlash/add jarayonlari uchun zaxira tekshiruv.
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

    key = get_memory_key(update)
    record_history_user(update)
    remember_turn_by_key(key, "user", user_text)
    search_query = build_search_query(key, user_text)

    normalized_operator = normalize_text(user_text)
    if any(x in normalized_operator for x in ("operator", "odam bilan gaplash", "konsultant bilan gaplash", "sotuvchi bilan gaplash")):
        await operator_request(update, context)
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
    # AKSO KNOWLEDGE QUESTIONS
    # --------------------------------------------------------
    # Masalan: "Bo\'lib to\'lashga qanday hujjat kerak?"
    # Bu yerda "kerak" so\'zi borligi uchun savol katalogga tushib
    # ketmasligi kerak. Avval bilim bazasi savoli sifatida ko\'ramiz.
    knowledge_question = is_akso_knowledge_question(user_text)

    # --------------------------------------------------------
    # PRODUCT CATALOG SEARCH
    # --------------------------------------------------------

    if not knowledge_question and likely_product_query(search_query):
        try:
            products = await github_get_products()
            if products:
                stats["catalog_queries"] += 1
                matches = find_local_products(search_query, products)
                if not matches:
                    matches = await find_ai_products(search_query, products)
                if matches:
                    filtered, constraints = filter_products_for_request(matches, search_query)
                    if constraints["budget"] is not None or constraints["monthly"] is not None or constraints["colors"]:
                        matches = filtered
                if matches:
                    last_product_queries[key] = search_query
                    if is_guided_need(search_query, matches):
                        stats["recommendation_requests"] += 1
                        answer = guided_question(search_query)
                        remember_turn_by_key(key, "assistant", answer)
                        await update.message.reply_text(answer)
                        return
                    await ask_product_confirmation(update.message, update.message.from_user.id if update.message.from_user else 0, matches)
                    return
                stats["not_found_queries"] += 1
                await update.message.reply_text("🔎 So'rovingizni tushundim, lekin aynan mos mahsulotni topa olmadim.\n\nMahsulot turi, rang, o'lcham, qancha pulgacha yoki oyiga qancha to'lov qulayligini yozing.")
                return
        except Exception as e:
            print("KATALOG QIDIRUV XATOSI:",repr(e))
            await update.message.reply_text("🔎 Katalogni tekshirishda vaqtinchalik texnik muammo yuz berdi.")
            return

    # --------------------------------------------------------
    # AKSO FAQ FAST ANSWERS
    # --------------------------------------------------------
    # Eng muhim FAQ savollariga AI chaqirmasdan aniq javob beramiz.
    # Bu Gemini xatosi yoki katalog qidiruvi sabab noto\'g\'ri javob chiqishini oldini oladi.
    if knowledge_question:
        q = normalize_text(user_text)
        if any(x in q for x in ("hujjat", "dokument", "pasport")) and any(
            x in q for x in ("bolib tolash", "muddatli tolov", "tolov")
        ):
            answer = (
                "📄 Bo\'lib to\'lash uchun <b>pasport</b> talab qilinadi.\n\n"
                "Agar bo\'lib to\'lash shartlari haqida boshqa savolingiz bo\'lsa, yozavering. 😊"
            )
            remember_turn_by_key(key, "assistant", answer)
            await update.message.reply_text(answer, parse_mode="HTML")
            return

    # --------------------------------------------------------
    # NORMAL AI CHAT
    # --------------------------------------------------------

    try:
        prompt = f"""
Sen AKSO do'konining virtual sotuvchisi va yordamchisisan.

AKSO bo'yicha ishonchli ma'lumotlar:
{akso_knowledge_text()}

QAT'IY QOIDALAR:
- AKSO do'koni, manzil, ish vaqti, mahsulot yo'nalishlari, yetkazib berish, montaj, to'lov, bo'lib to'lash, kafolat, qaytarish va aksiya haqidagi savollarga FAQAT yuqoridagi AKSO ma'lumotlariga tayanib javob ber.
- Bilim bazasida yo'q ma'lumotni o'ylab topma. Ishonching bo'lmasa, operatorga murojaat qilishni tavsiya qil.
- Oddiy suhbatga mahsulot rasmi yuborma.
- Mijoz mahsulotni aniq so'ramasa, katalogdagi mahsulotni o'zboshimchalik bilan taklif qilma.
- Mijozga naqd narx yoki jami bo'lib to'lash summasini aytma.
- 6 oy odatda 18%, lekin aksiya, ayrim mahsulot yoki oldindan to'lov bilan 6 oyga ustamasiz variant bo'lishi mumkin.
- O'zbekcha bo'lsa o'zbekcha, ruscha bo'lsa ruscha javob ber.
- Keraksiz uzun javob bermagin.

Suhbat konteksti:
{get_conversation_context(key)}

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
        remember_turn_by_key(key, "assistant", answer)

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

async def get_admin_commands():
    return [
        BotCommand("start", "🤖 Botni ishga tushirish"),
        BotCommand("catalog", "🛍 Mahsulot katalogi"),
        BotCommand("store", "🏪 AKSO haqida"),
        BotCommand("help", "❓ Yordam"),
        BotCommand("id", "🆔 Telegram ID"),
        BotCommand("addproduct", "➕ Mahsulot qo'shish"),
        BotCommand("products", "📦 Mahsulotlar"),
        BotCommand("editproduct", "✏️ Mahsulotni tahrirlash"),
        BotCommand("deleteproduct", "🗑 Mahsulotni o'chirish"),
        BotCommand("categories", "🗂 Kategoriyalar"),
        BotCommand("stats", "📊 Statistika"),
        BotCommand("orders", "🛒 Buyurtmalar"),
        BotCommand("users", "👥 Foydalanuvchilar va chat tarixi"),
        BotCommand("done", "✅ Rasmlarni tugatish"),
        BotCommand("skip", "⏭ O'tkazib yuborish"),
        BotCommand("cancel", "❌ Amalni bekor qilish"),
        BotCommand("addadmin", "👑 Admin tayinlash (faqat egasi)"),
        BotCommand("removeadmin", "🗑 Adminni olib tashlash (faqat egasi)"),
        BotCommand("admins", "👑 Adminlar ro'yxati (faqat egasi)"),
    ]


async def setup_admin_commands_for_chat(application, chat_id):
    await application.bot.set_my_commands(
        await get_admin_commands(),
        scope=BotCommandScopeChat(chat_id=chat_id),
    )


async def setup_command_menus(application):
    await load_additional_admins()
    await application.bot.delete_my_commands()

    await application.bot.delete_my_commands(
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

    user_commands = [
        BotCommand("start", "🤖 Botni ishga tushirish"),
        BotCommand("catalog", "🛍 Mahsulot katalogi"),
        BotCommand("store", "🏪 AKSO haqida"),
        BotCommand("help", "❓ Yordam"),
    ]

    await application.bot.set_my_commands(user_commands)
    await setup_admin_commands_for_chat(application, ADMIN_ID)

    print("✅ Telegram command menus o'rnatildi.")
    print("✅ Admin commands o'rnatildi.")


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
        "store",
        store_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "orders",
        orders_command
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
    CommandHandler(
        "users",
        users_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "addadmin",
        add_admin_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "removeadmin",
        remove_admin_command
    )
)

telegram_app.add_handler(
    CommandHandler(
        "admins",
        admins_command
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        history_callback_handler,
        pattern=r"^hist:"
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        sales_callback_handler,
        pattern=r"^(order:|pay:|operator$)"
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
            | filters.Document.ALL
        )
        & ~filters.COMMAND,
        reply_to_message
    )
)


# ============================================================
# WEBHOOK + HEALTH CHECK
# ============================================================

class HealthWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Render/UptimeRobot health so'rovlari logni ortiqcha to'ldirmasin.
        return

    def _send(self, status, body, content_type="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health" or self.path == "/health/":
            self._send(200, "OK")
            return

        self._send(404, "Not Found")

    def do_HEAD(self):
        if self.path == "/health" or self.path == "/health/":
            self._send(200, "OK")
            return

        self._send(404, "Not Found")

    def do_POST(self):
        if self.path.rstrip("/") != "/webhook":
            self._send(404, "Not Found")
            return

        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            self._send(403, "Forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            update = Update.de_json(payload, telegram_app.bot)

            loop = TELEGRAM_LOOP
            if loop is None or loop.is_closed():
                self._send(503, "Service Unavailable")
                return

            future = asyncio.run_coroutine_threadsafe(
                telegram_app.update_queue.put(update),
                loop,
            )
            future.result(timeout=5)
            self._send(200, "OK")
        except Exception as e:
            print("WEBHOOK UPDATE XATOSI:", repr(e))
            self._send(500, "Internal Server Error")


TELEGRAM_LOOP = None


async def run_application():
    global TELEGRAM_LOOP
    TELEGRAM_LOOP = asyncio.get_running_loop()

    await telegram_app.initialize()

    global chat_history
    try:
        chat_history = await github_get_chat_history()
        print(f"✅ Chat history yuklandi: {len(chat_history)} ta foydalanuvchi")
    except Exception as e:
        chat_history = {}
        print("CHAT HISTORY YUKLASH XATOSI:", repr(e))

    # post_init() avtomatik chaqirilmagani uchun command menyusini qo'lda o'rnatamiz.
    await setup_command_menus(telegram_app)

    await telegram_app.start()

    await telegram_app.bot.set_webhook(
        url=f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthWebhookHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="health-webhook-server",
        daemon=True,
    )
    server_thread.start()

    print("✅ AKSO webhook server ishga tushdi.")
    print("✅ Health URL:", f"{BASE_URL}/health")
    print("✅ Webhook URL:", f"{BASE_URL}/webhook")

    try:
        await asyncio.Event().wait()
    finally:
        await flush_chat_history()
        server.shutdown()
        server.server_close()
        try:
            await telegram_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            print("WEBHOOK O'CHIRISH XATOSI:", repr(e))
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == "__main__":
    print("Bot ishga tushmoqda...")
    print("Render URL:", BASE_URL)
    asyncio.run(run_application())
