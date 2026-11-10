import asyncio
import logging
import os
import re
import html
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    InlineKeyboardButton, KeyboardButton,
    ReplyKeyboardMarkup, LinkPreviewOptions
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import DB


# ================= CONFIG =================

def load_env():
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".env"
    )
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(
                        k.strip(),
                        v.strip().strip('"').strip("'")
                    )


load_env()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

DB_PATH = os.getenv("DB_PATH", "shop.db")
db = DB(DB_PATH)

BOT_USERNAME = ""
TZ = timezone(timedelta(hours=5))

router = Router()
log = logging.getLogger("shop-bot")

NOPREVIEW = LinkPreviewOptions(is_disabled=True)

CANCEL = "❌ Bekor qilish"
SKIP = "⏭ O'tkazib yuborish"


# ================= STATUS =================

STATUS = {
    "new": "🆕 Yangi",
    "accepted": "✅ Qabul qilindi",
    "ready": "🍽 Tayyor",
    "delivered": "🚚 Yetkazildi",
    "canceled": "❌ Bekor qilindi"
}

NEXT = {
    "new": ["accepted", "canceled"],
    "accepted": ["ready", "canceled"],
    "ready": ["delivered", "canceled"]
}

BTN = {
    "accepted": "✅ Qabul qildim",
    "ready": "🍽 Tayyor",
    "delivered": "🚚 Yetkazildi",
    "canceled": "❌ Bekor qilish"
}


# ================= HELPERS =================

def esc(value):
    return html.escape(str(value or ""))


def money(value):
    return f"{int(value):,}".replace(",", " ") + " so'm"


def parse_price(value):
    digits = re.sub(r"\D", "", value or "")

    if not digits:
        return None

    price = int(digits)

    if 0 < price <= 100_000_000:
        return price

    return None


def parse_phone(value):
    digits = re.sub(r"\D", "", value or "")

    if len(digits) == 9:
        return "+998" + digits

    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits

    return None


def shop_link(shop):
    return f"https://t.me/{BOT_USERNAME}?start=d_{shop['code']}"


def day_start_ts():
    now = datetime.now(TZ)

    return int(
        now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        ).timestamp()
    )


def month_start_ts():
    now = datetime.now(TZ)

    return int(
        now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        ).timestamp()
    )


def reply_kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=x) for x in row]
            for row in rows
        ],
        resize_keyboard=True
    )


def main_kb(uid):
    rows = [
        ["🍽 Menyu", "🛒 Savatcha"],
        ["📦 Buyurtmalarim", "✍️ Shikoyat va taklif"]
    ]

    shop = db.get_shop_by_owner(uid)

    if shop:
        rows.append(["🏪 Mening do'konim", "ℹ️ Yordam"])
    else:
        rows.append(["🏬 Do'kon ochish", "ℹ️ Yordam"])

    return reply_kb(rows)


def cancel_kb():
    return reply_kb([[CANCEL]])


def contact_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(
                text="📱 Raqamimni yuborish",
                request_contact=True
            )],
            [KeyboardButton(text=CANCEL)]
        ],
        resize_keyboard=True
    )


async def safe_edit(message, text, kb=None):
    try:
        if message.photo:
            await message.edit_caption(
                caption=text,
                reply_markup=kb
            )
        else:
            await message.edit_text(
                text,
                reply_markup=kb
            )
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Edit error: %s", e)


def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


def owned_product(uid, pid):
    product = db.get_product(pid)

    if not product:
        return None

    shop = db.get_shop(product["shop_id"])

    if not shop or shop["owner_id"] != uid:
        return None

    return product


# ================= FSM STATES =================

class Reg(StatesGroup):
    name = State()
    phone = State()


class AddP(StatesGroup):
    name = State()
    price = State()
    category = State()
    photo = State()


class AddCat(StatesGroup):
    name = State()


class EditPrice(StatesGroup):
    price = State()


class Rename(StatesGroup):
    name = State()


class Order(StatesGroup):
    phone = State()
    address = State()


class AdminSetPrice(StatesGroup):
    price = State()


class AdminSetCard(StatesGroup):
    card = State()
    owner = State()


class AdminBroadcast(StatesGroup):
    text = State()


class FeedbackState(StatesGroup):
    text = State()


class AdminReply(StatesGroup):
    text = State()


class PaymentState(StatesGroup):
    shop_name = State()
    receipt = State()


# ================= KEYBOARDS =================

def status_kb(order):
    statuses = NEXT.get(order["status"], [])

    if not statuses:
        return None

    kb = InlineKeyboardBuilder()

    for status in statuses:
        kb.button(
            text=BTN[status],
            callback_data=f"st:{order['id']}:{status}"
        )

    kb.adjust(2)

    return kb.as_markup()


def product_card_customer(product):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="➕ Savatga",
        callback_data=f"add:{product['id']}"
    )

    return (
        f"<b>{esc(product['name'])}</b>\n"
        f"💰 {money(product['price'])}",
        kb.as_markup()
    )


def product_card_seller(product):
    available = bool(product["available"])

    text = (
        f"<b>{esc(product['name'])}</b>\n"
        f"💰 {money(product['price'])}\n"
        f"{'✅ Mavjud' if available else '⛔ Tugagan'}"
    )

    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="⛔ Tugadi" if available else "✅ Mavjud",
            callback_data=f"pt:{product['id']}"
        ),
        InlineKeyboardButton(
            text="💰 Narxi",
            callback_data=f"pp:{product['id']}"
        )
    )

    kb.row(
        InlineKeyboardButton(
            text="🗑 O'chirish",
            callback_data=f"pd:{product['id']}"
        )
    )

    return text, kb.as_markup()
# ================= ADMIN PANEL =================

def admin_kb():
    paid = db.get_setting("is_paid", "0") == "1"

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💰 Do'kon narxini belgilash",
        callback_data="adm_price"
    )
    kb.button(
        text="💳 Karta sozlamalari",
        callback_data="adm_card"
    )
    kb.button(
        text=f"{'🔴 PULLIK' if paid else '🟢 BEPUL'}",
        callback_data="adm_paid"
    )
    kb.button(
        text="📊 Statistika",
        callback_data="adm_stats"
    )
    kb.button(
        text="🏪 Do'konlar",
        callback_data="adm_shops"
    )
    kb.button(
        text="💵 To'lovlar",
        callback_data="adm_payments"
    )
    kb.button(
        text="📩 Shikoyatlar",
        callback_data="adm_feedback"
    )
    kb.button(
        text="📢 Xabar tarqatish",
        callback_data="adm_broadcast"
    )

    kb.adjust(1)

    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()

    if not is_admin(message.from_user.id):
        return

    price = db.get_setting("shop_price", "0")
    card = db.get_setting("card_number", "Kiritilmagan")
    owner = db.get_setting("card_owner", "Kiritilmagan")

    await message.answer(
        "⚙️ <b>SUPER ADMIN PANEL</b>\n\n"
        f"💰 Do'kon narxi: {money(price)}\n"
        f"💳 Karta: <code>{esc(card)}</code>\n"
        f"👤 Karta egasi: {esc(owner)}",
        reply_markup=admin_kb()
    )


@router.callback_query(F.data == "adm_paid")
async def adm_paid(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q!", show_alert=True)
        return

    current = db.get_setting("is_paid", "0")
    new_value = "0" if current == "1" else "1"

    db.set_setting("is_paid", new_value)

    await cb.answer("To'lov rejimi o'zgartirildi!")

    await safe_edit(
        cb.message,
        "⚙️ <b>SUPER ADMIN PANEL</b>",
        admin_kb()
    )


@router.callback_query(F.data == "adm_price")
async def adm_price(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    await state.set_state(AdminSetPrice.price)

    await cb.message.answer(
        "Do'kon ochish narxini kiriting:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AdminSetPrice.price, F.text)
async def adm_save_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    price = parse_price(message.text)

    if not price:
        await message.answer("Narx noto'g'ri. Qayta kiriting:")
        return

    db.set_setting("shop_price", price)

    await state.clear()

    await message.answer(
        f"✅ Yangi narx: {money(price)}",
        reply_markup=main_kb(message.from_user.id)
    )


@router.callback_query(F.data == "adm_card")
async def adm_card(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    await state.set_state(AdminSetCard.card)

    await cb.message.answer(
        "Karta raqamini kiriting:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AdminSetCard.card, F.text)
async def adm_card_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    card = re.sub(r"\D", "", message.text)

    if not 16 <= len(card) <= 19:
        await message.answer("Karta raqamini tekshirib qayta kiriting:")
        return

    await state.update_data(card=card)
    await state.set_state(AdminSetCard.owner)

    await message.answer("Karta egasining F.I.SH ni kiriting:")


@router.message(AdminSetCard.owner, F.text)
async def adm_owner_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()

    db.set_setting("card_number", data["card"])
    db.set_setting("card_owner", message.text.strip())

    await state.clear()

    await message.answer(
        "✅ Karta sozlamalari saqlandi.",
        reply_markup=main_kb(message.from_user.id)
    )


# ================= STATISTICS =================

@router.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    stats = db.totals()

    await cb.message.answer(
        "📊 <b>BOT STATISTIKASI</b>\n\n"
        f"👥 Foydalanuvchilar: {stats['users']}\n"
        f"🏪 Do'konlar: {stats['shops']}\n"
        f"🍽 Mahsulotlar: {stats['products']}\n"
        f"📦 Buyurtmalar: {stats['orders']}\n"
        f"💰 Umumiy aylanma: {money(stats['sum'])}"
    )

    await cb.answer()


# ================= SHOP MANAGEMENT =================

@router.callback_query(F.data == "adm_shops")
async def adm_shops(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    await cb.answer()

    with db.get_conn() as conn:
        shops = conn.execute(
            "SELECT * FROM shops ORDER BY id DESC LIMIT 30"
        ).fetchall()

    if not shops:
        await cb.message.answer("Hozircha do'konlar yo'q.")
        return

    for shop in shops:
        active = bool(shop["is_active"])

        kb = InlineKeyboardBuilder()

        kb.button(
            text="🔒 Bloklash" if active else "🔓 Ochish",
            callback_data=f"adm_shop:{shop['id']}"
        )

        await cb.message.answer(
            f"🏪 <b>{esc(shop['name'])}</b>\n"
            f"🆔 ID: {shop['id']}\n"
            f"👤 Egasi: <code>{shop['owner_id']}</code>\n"
            f"📞 {esc(shop['phone'])}\n"
            f"Holati: {'✅ Faol' if active else '🔴 Bloklangan'}",
            reply_markup=kb.as_markup()
        )


@router.callback_query(F.data.startswith("adm_shop:"))
async def adm_shop_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    try:
        sid = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto'g'ri ID!")
        return

    shop = db.get_shop(sid)

    if not shop:
        await cb.answer("Do'kon topilmadi!")
        return

    new_state = not bool(shop["is_active"])

    db.set_shop_active(sid, new_state)

    await cb.answer(
        "Do'kon faollashtirildi!" if new_state else "Do'kon bloklandi!",
        show_alert=True
    )

    await safe_edit(
        cb.message,
        f"🏪 {esc(shop['name'])}\n"
        f"Holati: {'✅ Faol' if new_state else '🔴 Bloklangan'}",
        None
    )


# ================= BROADCAST =================

@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    await state.set_state(AdminBroadcast.text)

    await cb.message.answer(
        "Barcha foydalanuvchilarga yuboriladigan xabarni yozing:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AdminBroadcast.text, F.text)
async def adm_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text.strip()

    if len(text) > 4000:
        await message.answer("Xabar juda uzun. Qisqartiring.")
        return

    await state.clear()

    users = db.all_users()
    sent = 0

    await message.answer(
        f"📢 {len(users)} ta foydalanuvchiga yuborilmoqda..."
    )

    for user in users:
        try:
            await message.bot.send_message(
                user["id"], text
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await message.answer(
        f"✅ Yetkazildi: {sent}/{len(users)}"
    )


# ================= FEEDBACK =================

@router.message(F.text == "✍️ Shikoyat va taklif")
async def feedback_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FeedbackState.text)

    await message.answer(
        "✍️ Shikoyat yoki taklifingizni yozing:",
        reply_markup=cancel_kb()
    )


@router.message(FeedbackState.text, F.text)
async def feedback_save(message: Message, state: FSMContext):
    uid = message.from_user.id
    name = message.from_user.full_name
    text = message.text.strip()

    if not text or len(text) > 3000:
        await message.answer("Xabar 1-3000 belgi bo'lishi kerak.")
        return

    fid = db.add_feedback(uid, name, text)

    await state.clear()

    await message.answer(
        "✅ Xabaringiz adminga yuborildi.",
        reply_markup=main_kb(uid)
    )

    if ADMIN_ID:
        kb = InlineKeyboardBuilder()

        kb.button(
            text="✍️ Javob yozish",
            callback_data=f"fb_reply:{fid}"
        )

        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"📩 <b>Yangi murojaat #{fid}</b>\n\n"
                f"👤 {esc(name)}\n"
                f"🆔 <code>{uid}</code>\n\n"
                f"💬 {esc(text)}",
                reply_markup=kb.as_markup()
            )
        except Exception:
            log.exception("Feedback admin'ga yuborilmadi")


@router.callback_query(F.data.startswith("fb_reply:"))
async def feedback_reply_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    fid = int(cb.data.split(":")[1])

    with db.get_conn() as conn:
        feedback = conn.execute(
            "SELECT * FROM feedback WHERE id=?",
            (fid,)
        ).fetchone()

    if not feedback:
        await cb.answer("Murojaat topilmadi!")
        return

    await state.set_state(AdminReply.text)
    await state.update_data(target_uid=feedback["user_id"])

    await cb.message.answer(
        f"Javobingizni yozing. Foydalanuvchi ID: "
        f"{feedback['user_id']}",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AdminReply.text, F.text)
async def feedback_reply_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    uid = data.get("target_uid")

    await state.clear()

    try:
        await message.bot.send_message(
            uid,
            "👨‍💻 <b>Admin javobi:</b>\n\n"
            + esc(message.text)
        )

        await message.answer("✅ Javob yuborildi.")

    except Exception:
        await message.answer("❌ Xabarni yuborib bo'lmadi.")
# ================= START =================

@router.message(CommandStart())
async def start(message: Message, command: CommandObject,
                state: FSMContext):
    await state.clear()

    uid = message.from_user.id
    db.upsert_user(uid, message.from_user.full_name)

    arg = (command.args or "").strip()

    if arg.startswith("d_"):
        shop = db.get_shop_by_code(arg[2:])

        if not shop:
            await message.answer("Do'kon havolasi noto'g'ri yoki faol emas.")
            return

        db.set_user_shop(uid, shop["id"])

        await message.answer(
            f"🏪 <b>{esc(shop['name'])}</b> do'koniga xush kelibsiz!",
            reply_markup=main_kb(uid)
        )

        await send_catalog(message, shop)
        return

    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "🛒 Buyurtma berish yoki o'z do'koningizni ochish "
        "uchun botimizdan foydalaning.",
        reply_markup=main_kb(uid)
    )


@router.message(Command("id"))
async def get_id(message: Message):
    await message.answer(
        f"Telegram ID: <code>{message.from_user.id}</code>"
    )


@router.message(F.text == CANCEL)
async def cancel(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "❌ Amal bekor qilindi.",
        reply_markup=main_kb(message.from_user.id)
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Yordam")
async def help_user(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "<b>🛒 MIJOZLAR UCHUN</b>\n"
        "Do'kon havolasini oching, mahsulot tanlang va buyurtma bering.\n\n"
        "<b>🏪 SOTUVCHILAR UCHUN</b>\n"
        "Do'kon oching, mahsulot qo'shing va havolangizni ulashing.\n\n"
        "📦 Buyurtmalaringizni bot orqali kuzatishingiz mumkin."
    )


# ================= CUSTOMER MENU =================

async def send_catalog(message, shop, cat_id=None):
    if not shop["is_active"]:
        await message.answer("Bu do'kon hozir faol emas.")
        return

    categories = db.get_categories(shop["id"])

    products = db.list_products(
        shop["id"],
        category_id=cat_id,
        only_available=True
    )

    kb = InlineKeyboardBuilder()

    for category in categories:
        kb.button(
            text=f"📁 {category['name']}",
            callback_data=f"cat:{shop['id']}:{category['id']}"
        )

    if categories:
        kb.button(
            text="📋 Barcha mahsulotlar",
            callback_data=f"cat:{shop['id']}:all"
        )

    kb.adjust(2)

    await message.answer(
        f"🏪 <b>{esc(shop['name'])}</b>\n"
        f"📞 {esc(shop['phone'])}\n\n"
        "Mahsulot tanlang:",
        reply_markup=kb.as_markup() if categories else None
    )

    if not products:
        await message.answer("Hozircha mahsulot mavjud emas.")
        return

    for product in products[:30]:
        text, keyboard = product_card_customer(product)

        if product["photo"]:
            await message.answer_photo(
                product["photo"],
                caption=text,
                reply_markup=keyboard
            )
        else:
            await message.answer(
                text,
                reply_markup=keyboard
            )

    kb_cart = InlineKeyboardBuilder()
    kb_cart.button(
        text="🛒 Savatchani ochish",
        callback_data="cart"
    )

    await message.answer(
        "Mahsulot tanlab bo'lgach savatchani oching:",
        reply_markup=kb_cart.as_markup()
    )


@router.callback_query(F.data.startswith("cat:"))
async def category_open(cb: CallbackQuery):
    try:
        _, sid, cid = cb.data.split(":")
        sid = int(sid)
    except ValueError:
        await cb.answer("Noto'g'ri so'rov!")
        return

    shop = db.get_shop(sid)

    if not shop or not shop["is_active"]:
        await cb.answer("Do'kon mavjud emas!", show_alert=True)
        return

    if cid != "all":
        category = next(
            (c for c in db.get_categories(sid)
             if c["id"] == int(cid)),
            None
        )

        if not category:
            await cb.answer("Kategoriya topilmadi!")
            return

        category_id = int(cid)
    else:
        category_id = None

    await cb.answer()

    await send_catalog(
        cb.message,
        shop,
        category_id
    )


# ================= CART =================

def cart_view(uid):
    items = db.cart_items(uid)

    if not items:
        return "🛒 Savatcha bo'sh.", None

    total = 0
    lines = []
    kb = InlineKeyboardBuilder()

    for item in items:
        summ = item["price"] * item["qty"]
        total += summ

        lines.append(
            f"• {esc(item['name'])} "
            f"({item['qty']} × {money(item['price'])})"
            f" = {money(summ)}"
            + ("" if item["available"] else " ⚠️ Tugagan")
        )

        kb.row(
            InlineKeyboardButton(
                text="➖",
                callback_data=f"dec:{item['pid']}"
            ),
            InlineKeyboardButton(
                text=f"{item['qty']} dona",
                callback_data="noop"
            ),
            InlineKeyboardButton(
                text="➕",
                callback_data=f"inc:{item['pid']}"
            )
        )

    kb.row(
        InlineKeyboardButton(
            text="✅ Buyurtma berish",
            callback_data="checkout"
        ),
        InlineKeyboardButton(
            text="🗑 Tozalash",
            callback_data="clear"
        )
    )

    text = (
        "🛒 <b>SAVATCHA</b>\n\n"
        + "\n".join(lines)
        + f"\n\n💰 <b>Jami: {money(total)}</b>"
    )

    return text, kb.as_markup()


@router.message(F.text == "🛒 Savatcha")
@router.message(Command("savat"))
async def cart_message(message: Message, state: FSMContext):
    await state.clear()

    text, kb = cart_view(message.from_user.id)

    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cart")
async def cart_callback(cb: CallbackQuery):
    text, kb = cart_view(cb.from_user.id)

    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("add:"))
async def cart_add(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    product = db.get_product(pid)

    if not product or not product["available"]:
        await cb.answer("Mahsulot mavjud emas!", show_alert=True)
        return

    shop = db.get_shop(product["shop_id"])

    if not shop or not shop["is_active"]:
        await cb.answer("Do'kon faol emas!", show_alert=True)
        return

    db.upsert_user(cb.from_user.id, cb.from_user.full_name)

    qty, cleared = db.cart_add(cb.from_user.id, pid)

    if qty == 0:
        await cb.answer("Mahsulotni qo'shib bo'lmadi!")
        return

    text = f"✅ Savatda {qty} dona."

    if cleared:
        text += "\nBoshqa do'kon savatchasi tozalandi."

    await cb.answer(text, show_alert=cleared)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("inc:"))
async def cart_inc(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    product = db.get_product(pid)

    if not product or not product["available"]:
        await cb.answer("Mahsulot tugagan!", show_alert=True)
        return

    db.cart_change(cb.from_user.id, pid, 1)

    text, kb = cart_view(cb.from_user.id)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("dec:"))
async def cart_dec(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])

    db.cart_change(cb.from_user.id, pid, -1)

    text, kb = cart_view(cb.from_user.id)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "clear")
async def cart_clear(cb: CallbackQuery):
    db.cart_clear(cb.from_user.id)

    text, kb = cart_view(cb.from_user.id)

    await safe_edit(cb.message, text, kb)
    await cb.answer("Savatcha tozalandi!")


# ================= SHOP REGISTRATION =================

@router.message(F.text == "🏬 Do'kon ochish")
async def registration_start(message: Message, state: FSMContext):
    await state.clear()

    shop = db.get_shop_by_owner(message.from_user.id)

    if shop:
        await send_panel(message, shop)
        return

    if db.get_setting("is_paid", "0") == "1":
        await state.set_state(PaymentState.shop_name)

        await message.answer(
            f"🏪 <b>Do'kon ochish narxi: "
            f"{money(db.get_setting('shop_price', '0'))}</b>\n\n"
            "Avval do'koningiz nomini yozing:",
            reply_markup=cancel_kb()
        )
        return

    await state.set_state(Reg.name)

    await message.answer(
        "🏪 Do'koningiz nomini kiriting:",
        reply_markup=cancel_kb()
    )


@router.message(Reg.name, F.text)
async def registration_name(message: Message, state: FSMContext):
    name = message.text.strip()

    if not 2 <= len(name) <= 60:
        await message.answer("Nom 2-60 belgi bo'lishi kerak.")
        return

    await state.update_data(name=name)
    await state.set_state(Reg.phone)

    await message.answer(
        "📞 Telefon raqamingizni yuboring:",
        reply_markup=contact_kb()
    )


@router.message(Reg.phone, F.contact)
@router.message(Reg.phone, F.text)
async def registration_phone(message: Message, state: FSMContext):
    raw = (
        message.contact.phone_number
        if message.contact else message.text
    )

    phone = parse_phone(raw)

    if not phone:
        await message.answer("Telefon raqami noto'g'ri.")
        return

    data = await state.get_data()

    try:
        shop = db.create_shop(
            message.from_user.id,
            data["name"],
            phone
        )
        db.set_user_shop(message.from_user.id, shop["id"])

    except Exception:
        log.exception("Shop registration error")
        await message.answer("Do'kon yaratishda xatolik.")
        return

    await state.clear()

    await message.answer(
        f"🎉 <b>Do'koningiz ochildi!</b>\n\n"
        f"🔗 {shop_link(shop)}",
        reply_markup=main_kb(message.from_user.id),
        link_preview_options=NOPREVIEW
    )


# ================= PAID REGISTRATION =================

@router.message(PaymentState.shop_name, F.text)
async def paid_shop_name(message: Message, state: FSMContext):
    name = message.text.strip()

    if not 2 <= len(name) <= 60:
        await message.answer("Do'kon nomi 2-60 belgi bo'lsin.")
        return

    await state.update_data(shop_name=name)
    await state.set_state(PaymentState.receipt)

    card = db.get_setting("card_number", "")
    owner = db.get_setting("card_owner", "")
    price = db.get_setting("shop_price", "0")

    await message.answer(
        f"💰 To'lov: <b>{money(price)}</b>\n\n"
        f"💳 Karta: <code>{esc(card)}</code>\n"
        f"👤 Karta egasi: <b>{esc(owner)}</b>\n\n"
        "To'lovdan keyin chek rasmini yuboring.",
        reply_markup=cancel_kb()
    )


@router.message(PaymentState.receipt, F.photo)
async def paid_receipt(message: Message, state: FSMContext):
    data = await state.get_data()

    receipt = message.photo[-1].file_id

    payment_id = db.add_payment(
        message.from_user.id,
        data["shop_name"],
        receipt,
        int(db.get_setting("shop_price", "0"))
    )

    await state.clear()

    await message.answer(
        "✅ Chekingiz adminga yuborildi.\n"
        "Tasdiqlanishini kuting.",
        reply_markup=main_kb(message.from_user.id)
    )

    if ADMIN_ID:
        kb = InlineKeyboardBuilder()

        kb.button(
            text="✅ Tasdiqlash",
            callback_data=f"pay_ok:{payment_id}"
        )
        kb.button(
            text="❌ Rad etish",
            callback_data=f"pay_no:{payment_id}"
        )
        kb.adjust(2)

        await message.bot.send_photo(
            ADMIN_ID,
            receipt,
            caption=(
                f"💳 <b>Yangi to'lov #{payment_id}</b>\n\n"
                f"👤 ID: <code>{message.from_user.id}</code>\n"
                f"🏪 {esc(data['shop_name'])}\n"
                f"💰 {money(db.get_setting('shop_price', '0'))}"
            ),
            reply_markup=kb.as_markup()
        )


@router.callback_query(F.data.startswith("pay_ok:"))
async def payment_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    pid = int(cb.data.split(":")[1])
    payment = db.get_payment(pid)

    if not payment or payment["status"] != "pending":
        await cb.answer("To'lov allaqachon ko'rib chiqilgan!")
        return

    if db.get_shop_by_owner(payment["user_id"]):
        db.set_payment_status(pid, "rejected")
        await cb.answer("Bu foydalanuvchida do'kon mavjud!")
        return

    if not db.set_payment_status(pid, "approved"):
        await cb.answer("To'lov holati o'zgargan!")
        return

    try:
        shop = db.create_shop(
            payment["user_id"],
            payment["shop_name"],
            ""
        )

        db.set_user_shop(payment["user_id"], shop["id"])

        await cb.bot.send_message(
            payment["user_id"],
            "🎉 To'lovingiz tasdiqlandi!\n"
            "Do'koningiz yaratildi.\n"
            "Endi telefon raqamingizni sozlash uchun "
            "/panel buyrug'idan foydalaning."
        )

        await cb.answer("To'lov tasdiqlandi!")

        await safe_edit(
            cb.message,
            f"✅ To'lov #{pid} tasdiqlandi.",
            None
        )

    except Exception:
        log.exception("Payment approval error")
        await cb.answer("Do'kon yaratishda xatolik!", show_alert=True)


@router.callback_query(F.data.startswith("pay_no:"))
async def payment_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    pid = int(cb.data.split(":")[1])
    payment = db.get_payment(pid)

    if not payment or not db.set_payment_status(pid, "rejected"):
        await cb.answer("To'lov topilmadi yoki ko'rib chiqilgan.")
        return

    await cb.bot.send_message(
        payment["user_id"],
        "❌ To'lovingiz rad etildi. Batafsil ma'lumot uchun admin bilan bog'laning."
    )

    await cb.answer("To'lov rad etildi!")

    await safe_edit(
        cb.message,
        f"❌ To'lov #{pid} rad etildi.",
        None
    )


@router.callback_query(F.data == "adm_payments")
async def admin_payments(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    payments = db.pending_payments()

    await cb.answer()

    if not payments:
        await cb.message.answer("Kutilayotgan to'lovlar yo'q.")
        return

    for payment in payments:
        kb = InlineKeyboardBuilder()

        kb.button(
            text="✅ Tasdiqlash",
            callback_data=f"pay_ok:{payment['id']}"
        )
        kb.button(
            text="❌ Rad etish",
            callback_data=f"pay_no:{payment['id']}"
        )
        kb.adjust(2)

        await cb.message.answer(
            f"💳 To'lov #{payment['id']}\n"
            f"🏪 {esc(payment['shop_name'])}\n"
            f"💰 {money(payment['amount'])}\n"
            f"👤 ID: {payment['user_id']}",
            reply_markup=kb.as_markup()
        )
# ================= SELLER PANEL =================

async def send_panel(message, shop):
    if not shop["is_active"]:
        await message.answer("Do'koningiz bloklangan.")
        return

    count, total = db.day_stats(
        shop["id"],
        day_start_ts()
    )

    kb = InlineKeyboardBuilder()

    kb.button(
        text="➕ Mahsulot qo'shish",
        callback_data="p_add"
    )
    kb.button(
        text="📁 Kategoriya qo'shish",
        callback_data="c_add"
    )
    kb.button(
        text="📋 Mahsulotlar",
        callback_data="p_list"
    )
    kb.button(
        text="📦 Buyurtmalar",
        callback_data="o_list"
    )
    kb.button(
        text="✏️ Do'kon nomi",
        callback_data="s_name"
    )
    kb.button(
        text="📞 Telefon raqami",
        callback_data="s_phone"
    )
    kb.button(
        text="📊 Statistika",
        callback_data="s_stats"
    )
    kb.adjust(2)

    await message.answer(
        f"🏪 <b>{esc(shop['name'])}</b>\n"
        f"📞 {esc(shop['phone'])}\n\n"
        f"🔗 <code>{shop_link(shop)}</code>\n\n"
        f"📊 Bugungi buyurtmalar: {count}\n"
        f"💰 Aylanma: {money(total)}\n"
        f"🍽 Mahsulotlar: {db.count_products(shop['id'])}",
        reply_markup=kb.as_markup(),
        link_preview_options=NOPREVIEW
    )


@router.message(F.text == "🏪 Mening do'konim")
@router.message(Command("panel"))
async def seller_panel(message: Message, state: FSMContext):
    await state.clear()

    shop = db.get_shop_by_owner(message.from_user.id)

    if not shop:
        await message.answer("Sizda hali do'kon mavjud emas.")
        return

    await send_panel(message, shop)


@router.callback_query(F.data == "s_stats")
async def seller_stats(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)

    if not shop:
        await cb.answer("Ruxsat yo'q!")
        return

    day_count, day_sum = db.day_stats(
        shop["id"], day_start_ts()
    )

    month_count, month_sum = db.monthly_stats(
        shop["id"], month_start_ts()
    )

    await cb.message.answer(
        "📊 <b>SAVDO STATISTIKASI</b>\n\n"
        f"Bugun: {day_count} ta buyurtma\n"
        f"💰 {money(day_sum)}\n\n"
        f"Shu oy: {month_count} ta buyurtma\n"
        f"💰 {money(month_sum)}"
    )

    await cb.answer()


# ================= SHOP EDIT =================

@router.callback_query(F.data == "s_name")
async def shop_name_start(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return

    await state.set_state(Rename.name)

    await cb.message.answer(
        "Yangi do'kon nomini kiriting:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(Rename.name, F.text)
async def shop_name_save(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)
    name = message.text.strip()

    if not 2 <= len(name) <= 60:
        await message.answer("Nom 2-60 belgi bo'lishi kerak.")
        return

    if shop:
        db.rename_shop(shop["id"], name)

    await state.clear()

    await message.answer(
        "✅ Do'kon nomi o'zgartirildi.",
        reply_markup=main_kb(message.from_user.id)
    )


@router.callback_query(F.data == "s_phone")
async def shop_phone_start(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return

    await state.set_state(Reg.phone)

    await cb.message.answer(
        "Yangi telefon raqamingizni yuboring:",
        reply_markup=contact_kb()
    )

    await cb.answer()


# ================= CATEGORY =================

@router.callback_query(F.data == "c_add")
async def category_start(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return

    await state.set_state(AddCat.name)

    await cb.message.answer(
        "Kategoriya nomini kiriting:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AddCat.name, F.text)
async def category_save(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)
    name = message.text.strip()

    if not 2 <= len(name) <= 40:
        await message.answer("Kategoriya nomi 2-40 belgi bo'lsin.")
        return

    if shop:
        db.add_category(shop["id"], name)

    await state.clear()

    await message.answer(
        "✅ Kategoriya qo'shildi.",
        reply_markup=main_kb(message.from_user.id)
    )


# ================= ADD PRODUCT =================

@router.callback_query(F.data == "p_add")
async def product_add_start(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return

    await state.set_state(AddP.name)

    await cb.message.answer(
        "Mahsulot nomini kiriting:",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(AddP.name, F.text)
async def product_add_name(message: Message, state: FSMContext):
    name = message.text.strip()

    if not 2 <= len(name) <= 80:
        await message.answer("Mahsulot nomi 2-80 belgi bo'lsin.")
        return

    await state.update_data(name=name)
    await state.set_state(AddP.price)

    await message.answer("Mahsulot narxini kiriting:")


@router.message(AddP.price, F.text)
async def product_add_price(message: Message, state: FSMContext):
    price = parse_price(message.text)

    if not price:
        await message.answer("Narx noto'g'ri. Qayta kiriting:")
        return

    await state.update_data(price=price)

    shop = db.get_shop_by_owner(message.from_user.id)
    categories = db.get_categories(shop["id"])

    if categories:
        await state.set_state(AddP.category)

        kb = InlineKeyboardBuilder()

        for cat in categories:
            kb.button(
                text=cat["name"],
                callback_data=f"pcat:{cat['id']}"
            )

        kb.button(
            text="O'tkazib yuborish",
            callback_data="pcat:0"
        )

        kb.adjust(1)

        await message.answer(
            "📁 Mahsulot kategoriyasini tanlang:",
            reply_markup=kb.as_markup()
        )
    else:
        await state.update_data(category_id=None)
        await state.set_state(AddP.photo)

        await message.answer(
            "Mahsulot rasmini yuboring yoki o'tkazib yuboring:",
            reply_markup=reply_kb([[SKIP], [CANCEL]])
        )


@router.callback_query(F.data.startswith("pcat:"))
async def product_category_select(cb: CallbackQuery, state: FSMContext):
    shop = db.get_shop_by_owner(cb.from_user.id)

    if not shop or await state.get_state() != AddP.category.state:
        await cb.answer("So'rov eskirgan!")
        return

    cid = int(cb.data.split(":")[1])

    if cid:
        category = next(
            (c for c in db.get_categories(shop["id"])
             if c["id"] == cid),
            None
        )

        if not category:
            await cb.answer("Kategoriya topilmadi!")
            return

    await state.update_data(
        category_id=cid or None
    )

    await state.set_state(AddP.photo)

    await cb.message.answer(
        "📷 Mahsulot rasmini yuboring yoki o'tkazib yuboring:",
        reply_markup=reply_kb([[SKIP], [CANCEL]])
    )

    await cb.answer()


async def finish_product(message, state, photo):
    data = await state.get_data()
    shop = db.get_shop_by_owner(message.from_user.id)

    if shop:
        db.add_product(
            shop["id"],
            data["name"],
            data["price"],
            photo,
            data.get("category_id")
        )

    await state.clear()

    await message.answer(
        f"✅ {esc(data['name'])} qo'shildi.",
        reply_markup=main_kb(message.from_user.id)
    )


@router.message(AddP.photo, F.photo)
async def product_add_photo(message: Message, state: FSMContext):
    await finish_product(
        message,
        state,
        message.photo[-1].file_id
    )


@router.message(AddP.photo, F.text == SKIP)
async def product_add_skip(message: Message, state: FSMContext):
    await finish_product(message, state, None)


# ================= PRODUCT LIST =================

@router.callback_query(F.data == "p_list")
async def product_list(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)

    if not shop:
        await cb.answer("Ruxsat yo'q!")
        return

    products = db.list_products(shop["id"])

    await cb.answer()

    if not products:
        await cb.message.answer("Mahsulotlar mavjud emas.")
        return

    for product in products[:30]:
        text, kb = product_card_seller(product)

        if product["photo"]:
            await cb.message.answer_photo(
                product["photo"],
                caption=text,
                reply_markup=kb
            )
        else:
            await cb.message.answer(
                text,
                reply_markup=kb
            )


# ================= PRODUCT ACTIONS =================

@router.callback_query(F.data.startswith("pt:"))
async def product_toggle(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    product = owned_product(cb.from_user.id, pid)

    if not product:
        await cb.answer("Ruxsat yo'q!")
        return

    db.set_available(pid, not product["available"])

    product = db.get_product(pid)
    text, kb = product_card_seller(product)

    await safe_edit(cb.message, text, kb)
    await cb.answer("Mahsulot holati yangilandi!")


@router.callback_query(F.data.startswith("pd:"))
async def product_delete(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    product = owned_product(cb.from_user.id, pid)

    if not product:
        await cb.answer("Ruxsat yo'q!")
        return

    db.delete_product(pid)

    await safe_edit(
        cb.message,
        "🗑 Mahsulot o'chirildi.",
        None
    )

    await cb.answer("O'chirildi!")


@router.callback_query(F.data.startswith("pp:"))
async def product_price_start(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    product = owned_product(cb.from_user.id, pid)

    if not product:
        await cb.answer("Ruxsat yo'q!")
        return

    await state.set_state(EditPrice.price)
    await state.update_data(pid=pid)

    await cb.message.answer(
        f"Yangi narxni kiriting:\n"
        f"Hozirgi narx: {money(product['price'])}",
        reply_markup=cancel_kb()
    )

    await cb.answer()


@router.message(EditPrice.price, F.text)
async def product_price_save(message: Message, state: FSMContext):
    price = parse_price(message.text)

    if not price:
        await message.answer("Narx noto'g'ri.")
        return

    data = await state.get_data()
    pid = data["pid"]

    if owned_product(message.from_user.id, pid):
        db.set_price(pid, price)

    await state.clear()

    await message.answer(
        "✅ Narx o'zgartirildi.",
        reply_markup=main_kb(message.from_user.id)
    )


# ================= SELLER PHONE SAVE =================

@router.message(Reg.phone, F.contact)
@router.message(Reg.phone, F.text)
async def seller_phone_save(message: Message, state: FSMContext):
    raw = (
        message.contact.phone_number
        if message.contact else message.text
    )

    phone = parse_phone(raw)

    if not phone:
        await message.answer("Telefon raqami noto'g'ri.")
        return

    shop = db.get_shop_by_owner(message.from_user.id)

    if not shop:
        await message.answer("Do'kon topilmadi.")
        await state.clear()
        return

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE shops SET phone=? WHERE id=?",
            (phone, shop["id"])
        )

    await state.clear()

    await message.answer(
        "✅ Telefon raqami saqlandi.",
        reply_markup=main_kb(message.from_user.id)
    )


# ================= CHECKOUT =================

@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery, state: FSMContext):
    items = db.cart_items(cb.from_user.id)

    if not items or any(not i["available"] for i in items):
        await cb.answer(
            "Savatchani tekshiring. Ayrim mahsulotlar tugagan.",
            show_alert=True
        )
        return

    await state.set_state(Order.phone)

    await cb.message.answer(
        "📱 Telefon raqamingizni yuboring:",
        reply_markup=contact_kb()
    )

    await cb.answer()


@router.message(Order.phone, F.contact)
@router.message(Order.phone, F.text)
async def order_phone(message: Message, state: FSMContext):
    raw = (
        message.contact.phone_number
        if message.contact else message.text
    )

    phone = parse_phone(raw)

    if not phone:
        await message.answer("Telefon raqami noto'g'ri.")
        return

    await state.update_data(phone=phone)
    await state.set_state(Order.address)

    await message.answer(
        "📍 Yetkazib berish manzilini yozing:",
        reply_markup=cancel_kb()
    )


@router.message(Order.address, F.text)
async def order_address(message: Message, state: FSMContext):
    address = message.text.strip()

    if not 5 <= len(address) <= 300:
        await message.answer("Manzilni to'g'ri kiriting.")
        return

    data = await state.get_data()

    oid = db.create_order(
        message.from_user.id,
        message.from_user.full_name,
        data["phone"],
        address
    )

    await state.clear()

    if not oid:
        await message.answer(
            "❌ Buyurtma yaratilmadi. Savatchani qayta tekshiring.",
            reply_markup=main_kb(message.from_user.id)
        )
        return

    order = db.get_order(oid)
    shop = db.get_shop(order["shop_id"])

    await message.answer(
        f"✅ Buyurtmangiz #{oid} qabul qilindi!",
        reply_markup=main_kb(message.from_user.id)
    )

    try:
        await message.bot.send_message(
            shop["owner_id"],
            "🆕 <b>YANGI BUYURTMA!</b>\n\n"
            + order_text(order, db.order_items(oid), True),
            reply_markup=status_kb(order)
        )
    except Exception:
        log.exception("Seller notification error")


# ================= ORDER STATUS =================

def order_text(order, items, seller=False):
    lines = []

    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. {esc(item['product_name'])} — "
            f"{item['qty']} × {money(item['price'])}"
        )

    text = (
        f"📦 <b>BUYURTMA #{order['id']}</b>\n\n"
        + "\n".join(lines)
        + f"\n\n💰 Jami: {money(order['total'])}"
        + f"\n📌 Holat: {STATUS.get(order['status'], order['status'])}"
    )

    if seller:
        text = (
            text
            + f"\n\n👤 {esc(order['customer_name'])}"
            + f"\n📞 {esc(order['phone'])}"
            + f"\n📍 {esc(order['address'])}"
        )

    return text


@router.callback_query(F.data.startswith("st:"))
async def order_status_change(cb: CallbackQuery):
    try:
        _, oid, new_status = cb.data.split(":")
        oid = int(oid)
    except ValueError:
        await cb.answer("Noto'g'ri so'rov!")
        return

    order = db.get_order(oid)

    if not order:
        await cb.answer("Buyurtma topilmadi!")
        return

    shop = db.get_shop(order["shop_id"])

    if not shop or shop["owner_id"] != cb.from_user.id:
        await cb.answer("Bu buyurtma sizga tegishli emas!", show_alert=True)
        return

    if new_status not in NEXT.get(order["status"], []):
        await cb.answer("Bu amalni bajarib bo'lmaydi!")
        return

    db.set_status(oid, new_status)

    order = db.get_order(oid)

    await safe_edit(
        cb.message,
        order_text(order, db.order_items(oid), True),
        status_kb(order)
    )

    await cb.answer("Buyurtma yangilandi!")

    try:
        await cb.bot.send_message(
            order["customer_id"],
            f"📦 Buyurtmangiz #{oid} holati:\n"
            f"{STATUS[new_status]}"
        )
    except Exception:
        log.exception("Customer notification error")


# ================= CUSTOMER ORDERS =================

@router.message(F.text == "📦 Buyurtmalarim")
async def customer_orders(message: Message, state: FSMContext):
    await state.clear()

    orders = db.user_orders(message.from_user.id, 10)

    if not orders:
        await message.answer("Hozircha buyurtmalaringiz yo'q.")
        return

    for order in orders:
        kb = InlineKeyboardBuilder()

        if order["status"] == "new":
            kb.button(
                text="❌ Bekor qilish",
                callback_data=f"oc:{order['id']}"
            )

        await message.answer(
            order_text(
                order,
                db.order_items(order["id"])
            )
            + f"\n🏪 {esc(order['shop_name'])}",
            reply_markup=kb.as_markup() if order["status"] == "new" else None
        )


@router.callback_query(F.data.startswith("oc:"))
async def customer_cancel_order(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])

    success = db.cancel_order(
        oid,
        cb.from_user.id
    )

    if not success:
        await cb.answer(
            "Buyurtmani bekor qilib bo'lmaydi.",
            show_alert=True
        )
        return

    order = db.get_order(oid)
    shop = db.get_shop(order["shop_id"])

    await cb.answer("Buyurtma bekor qilindi!")

    await safe_edit(
        cb.message,
        order_text(order, db.order_items(oid)),
        None
    )

    try:
        await cb.bot.send_message(
            shop["owner_id"],
            f"❌ #{oid} buyurtma mijoz tomonidan bekor qilindi."
        )
    except Exception:
        log.exception("Cancel notification error")


# ================= PHONE FOR PAID SHOP =================

@router.message(Command("telefon"))
async def phone_command(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)

    if not shop:
        await message.answer("Sizda do'kon mavjud emas.")
        return

    await state.set_state(Reg.phone)

    await message.answer(
        "Do'kon telefon raqamini yuboring:",
        reply_markup=contact_kb()
    )


# ================= HTTP SERVER =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, *args):
        pass


def run_server():
    port = int(os.getenv("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    server.serve_forever()


# ================= MAIN =================

async def main():
    global BOT_USERNAME

    logging.basicConfig(level=logging.INFO)

    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi!")

    if not ADMIN_ID:
        raise SystemExit("ADMIN_ID topilmadi!")

    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    me = await bot.get_me()
    BOT_USERNAME = me.username

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="savat", description="Savatcha"),
        BotCommand(command="panel", description="Do'kon paneli"),
        BotCommand(command="telefon", description="Telefon raqami"),
        BotCommand(command="admin", description="Admin panel"),
        BotCommand(command="help", description="Yordam")
    ])

    dp = Dispatcher(
        storage=MemoryStorage()
    )

    dp.include_router(router)

    log.info("Bot ishga tushdi: @%s", BOT_USERNAME)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())