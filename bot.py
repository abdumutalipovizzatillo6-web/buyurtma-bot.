"""Kichik sotuvchilar uchun mukammallashtirilgan buyurtma boti (aiogram 3 + SQLite).
Yangi funksiyalar:
- Super Admin panel (Statistika va do'kon ochishni pullik/bepul qilish)
- Kategoriyalar (Mahsulotlarni bo'limlarga ajratish)
- Shikoyat va takliflar bo'limi
- Buyurtmada yetkazib berish turi va vaqtini tanlash
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import DB

log = logging.getLogger("buyurtma-bot")


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

_default_db = "/data/shop.db" if os.path.isdir("/data") else "shop.db"
db = DB(os.environ.get("DB_PATH", _default_db))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
BOT_USERNAME = ""
TZ = timezone(timedelta(hours=5))

router = Router()
NOPREVIEW = LinkPreviewOptions(is_disabled=True)

STATUS = {
    "new": "🆕 Yangi",
    "accepted": "✅ Qabul qilindi",
    "ready": "🍽 Tayyor",
    "delivered": "🚚 Yetkazildi",
    "canceled": "❌ Bekor qilindi",
}
NEXT = {
    "new": ["accepted", "canceled"],
    "accepted": ["ready", "canceled"],
    "ready": ["delivered", "canceled"],
}
BTN = {
    "accepted": "✅ Qabul qildim",
    "ready": "🍽 Tayyor",
    "delivered": "🚚 Yetkazildi",
    "canceled": "❌ Bekor qilish",
}

CANCEL = "❌ Bekor qilish"
SKIP = "⏭ O'tkazib yuborish"


def esc(t) -> str:
    return html.escape(str(t if t is not None else ""))


def money(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " so'm"


def parse_price(t: str):
    d = re.sub(r"\D", "", t or "")
    if not d:
        return None
    v = int(d)
    return v if 0 < v <= 100_000_000 else None


def parse_phone(t: str):
    d = re.sub(r"\D", "", t or "")
    if len(d) == 9:
        return "+998" + d
    if len(d) == 12 and d.startswith("998"):
        return "+" + d
    return None


def shop_link(shop) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=d_{shop['code']}"


def day_start_ts() -> int:
    now = datetime.now(TZ)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def reply_kb(rows, one_time=False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=one_time,
    )


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [["🍽 Menyu", "🛒 Savatcha"], ["📦 Buyurtmalarim", "💡 Shikoyat va takliflar"]]
    if db.get_shop_by_owner(uid):
        rows.append(["🏪 Mening do'konim", "ℹ️ Yordam"])
    else:
        rows.append(["🏬 Do'kon ochish", "ℹ️ Yordam"])
    if ADMIN_ID and uid == ADMIN_ID:
        rows.append(["⚙️ Super Admin Panel"])
    return reply_kb(rows)


def cancel_kb() -> ReplyKeyboardMarkup:
    return reply_kb([[CANCEL]])


async def safe_edit(msg: Message, text: str, kb=None):
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit xatosi: %s", e)


def order_text(o, items, for_seller: bool) -> str:
    lines = [f"{i}. {esc(it['product_name'])} — {it['qty']} × {money(it['price'])}" for i, it in enumerate(items, 1)]
    head = f"📦 <b>Buyurtma #{o['id']}</b>\n"
    if for_seller:
        head += f"👤 {esc(o['customer_name'])}\n📞 {esc(o['phone'])}\n📍 {esc(o['address'])}\n"
        head += f"🚚 Turi: {esc(o['delivery_type'])}\n⏰ Vaqt: {esc(o['delivery_time'])}\n"
    return (
        head
        + "\n"
        + "\n".join(lines)
        + f"\n\n💰 Jami: <b>{money(o['total'])}</b>\nHolat: {STATUS.get(o['status'], o['status'])}"
    )


def status_kb(o):
    nxt = NEXT.get(o["status"])
    if not nxt:
        return None
    kb = InlineKeyboardBuilder()
    kb.row(*[InlineKeyboardButton(text=BTN[s], callback_data=f"st:{o['id']}:{s}") for s in nxt])
    return kb.as_markup()


def product_card_customer(p):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Savatga", callback_data=f"add:{p['id']}"))
    return f"<b>{esc(p['name'])}</b>\n💰 {money(p['price'])}", kb.as_markup()


def product_card_seller(p):
    state = "✅ Bor" if p["available"] else "⛔ Yo'q (mijozlarga ko'rinmaydi)"
    text = f"<b>{esc(p['name'])}</b>\n💰 {money(p['price'])}\n{state}"
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⛔ Tugadi" if p["available"] else "✅ Qayta bor",
            callback_data=f"pt:{p['id']}",
        ),
        InlineKeyboardButton(text="💰 Narxi", callback_data=f"pp:{p['id']}"),
        InlineKeyboardButton(text="🗑", callback_data=f"pd:{p['id']}"),
    )
    return text, kb.as_markup()


def cart_view(uid: int):
    items = db.cart_items(uid)
    if not items:
        return "🛒 Savatchangiz bo'sh.\n«🍽 Menyu» orqali mahsulot tanlang.", None
    lines, total = [], 0
    kb = InlineKeyboardBuilder()
    for i, it in enumerate(items, 1):
        s = it["price"] * it["qty"]
        total += s
        mark = "" if it["available"] else " ⚠️ (tugagan)"
        lines.append(f"{i}. {esc(it['name'])}{mark} — {it['qty']} × {money(it['price'])} = <b>{money(s)}</b>")
        short = it["name"] if len(it["name"]) <= 14 else it["name"][:13] + "…"
        kb.row(
            InlineKeyboardButton(text="➖", callback_data=f"dec:{it['pid']}"),
            InlineKeyboardButton(text=f"{short} · {it['qty']}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"inc:{it['pid']}"),
        )
    kb.row(
        InlineKeyboardButton(text="✅ Buyurtma berish", callback_data="checkout"),
        InlineKeyboardButton(text="🗑 Tozalash", callback_data="clear"),
    )
    text = "🛒 <b>Savatcha</b>\n\n" + "\n".join(lines) + f"\n\n💰 Jami: <b>{money(total)}</b>"
    return text, kb.as_markup()


async def send_catalog(message: Message, shop):
    categories = db.list_categories(shop["id"])
    kb = InlineKeyboardBuilder()
    if categories:
        for c in categories:
            kb.row(InlineKeyboardButton(text=f"📁 {c['name']}", callback_data=f"cat:{shop['id']}:{c['id']}"))
        await message.answer(f"🏪 <b>{esc(shop['name'])}</b>\n\nBo'limni tanlang 👇", reply_markup=kb.as_markup())
        return

    products = db.list_products(shop["id"], only_available=True)
    head = f"🏪 <b>{esc(shop['name'])}</b>"
    if shop["phone"]:
        head += f"\n📞 {esc(shop['phone'])}"
    if not products:
        await message.answer(head + "\n\nHozircha mahsulot yo'q.")
        return
    await message.answer(head + "\n\nTanlang va «➕ Savatga» ni bosing 👇")
    for p in products[:30]:
        text, pkb = product_card_customer(p)
        if p["photo"]:
            await message.answer_photo(photo=p["photo"], caption=text, reply_markup=pkb)
        else:
            await message.answer(text, reply_markup=pkb)
    c_kb = InlineKeyboardBuilder()
    c_kb.row(InlineKeyboardButton(text="🛒 Savatchani ochish", callback_data="cart"))
    await message.answer("Tanlab bo'lgach savatchani oching:", reply_markup=c_kb.as_markup())


async def send_panel(message: Message, shop):
    cnt, summ = db.day_stats(shop["id"], day_start_ts())
    text = (
        f"🏪 <b>{esc(shop['name'])}</b>\n📞 {esc(shop['phone'])}\n\n"
        f"🔗 Mijozlar uchun havola:\n{shop_link(shop)}\n\n"
        f"📊 Bugun: {cnt} ta buyurtma, {money(summ)}\n"
        f"🍽 Mahsulotlar: {db.count_products(shop['id'])} ta"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Mahsulot qo'shish", callback_data="p_add"),
        InlineKeyboardButton(text="📁 Kategoriya qo'shish", callback_data="c_add"),
    )
    kb.row(
        InlineKeyboardButton(text="📋 Mahsulotlar", callback_data="p_list"),
        InlineKeyboardButton(text="📦 Faol buyurtmalar", callback_data="o_list"),
    )
    kb.row(InlineKeyboardButton(text="✏️ Do'kon nomi", callback_data="s_name"))
    await message.answer(text, reply_markup=kb.as_markup(), link_preview_options=NOPREVIEW)


def owned_product(uid: int, pid: int):
    p = db.get_product(pid)
    if p is None:
        return None
    shop = db.get_shop(p["shop_id"])
    if shop is None or shop["owner_id"] != uid:
        return None
    return p


# ======================= Holatlar =======================

class Reg(StatesGroup):
    name = State()
    phone = State()


class AddCategory(StatesGroup):
    name = State()


class AddP(StatesGroup):
    category = State()
    name = State()
    price = State()
    photo = State()


class EditPrice(StatesGroup):
    price = State()


class Rename(StatesGroup):
    name = State()


class Order(StatesGroup):
    delivery_type = State()
    delivery_time = State()
    phone = State()
    address = State()


class FeedbackState(StatesGroup):
    msg = State()


# ======================= Buyruqlar =======================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    db.upsert_user(uid, message.from_user.full_name)
    arg = (command.args or "").strip()
    if arg.startswith("d_"):
        shop = db.get_shop_by_code(arg[2:])
        if shop:
            db.set_user_shop(uid, shop["id"])
            await message.answer("Xush kelibsiz! 👋", reply_markup=main_kb(uid))
            await send_catalog(message, shop)
            return
        await message.answer("Bu havola eskirgan yoki noto'g'ri.", reply_markup=main_kb(uid))
        return
    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Bu bot orqali:\n"
        "🛍 do'konlardan oson buyurtma berasiz — sotuvchi havolasini oching;\n"
        "🏪 o'z do'koningizni ochib, buyurtmalarni Telegram'da qabul qilasiz — «🏬 Do'kon ochish».",
        reply_markup=main_kb(uid),
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Sizning ID: <code>{message.from_user.id}</code>")


# Super Admin Panel
@router.message(F.text == "⚙️ Super Admin Panel")
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    t = db.totals()
    is_paid = db.get_setting("paid_shop", "0") == "1"
    status_text = "🔴 Pullik" if is_paid else "🟢 Bepul"
    
    text = (
        f"⚙️ <b>Super Admin Panel</b>\n\n"
        f"📊 <b>Statistika:</b>\n"
        f"👤 Foydalanuvchilar: {t['users']}\n"
        f"🏪 Do'konlar: {t['shops']}\n"
        f"🍽 Mahsulotlar: {t['products']}\n"
        f"📦 Jami buyurtmalar: {t['orders']}\n"
        f"💰 Jami aylanma: {money(t['sum'])}\n\n"
        f"🏬 Do'kon ochish rejimi: <b>{status_text}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Do'kon ochish rejimini o'zgartirish", callback_data="toggle_paid_shop"))
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "toggle_paid_shop")
async def toggle_paid(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    curr = db.get_setting("paid_shop", "0")
    new_val = "1" if curr == "0" else "0"
    db.set_setting("paid_shop", new_val)
    await cb.answer("Rejim o'zgartirildi!")
    await admin_panel(cb.message)


# Shikoyat va takliflar
@router.message(F.text == "💡 Shikoyat va takliflar")
async def feedback_start(message: Message, state: FSMContext):
    await state.set_state(FeedbackState.msg)
    await message.answer("O'z shikoyat yoki taklifingizni yozib yuboring. Biz buni albatta ko'rib chiqamiz!", reply_markup=cancel_kb())


@router.message(FeedbackState.msg, F.text)
async def feedback_save(message: Message, state: FSMContext):
    db.add_feedback(message.from_user.id, message.from_user.full_name, message.text)
    await state.clear()
    await message.answer("Rahmat! Xabaringiz qabul qilindi. 🙏", reply_markup=main_kb(message.from_user.id))


@router.message(F.text == CANCEL)
async def do_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb(message.from_user.id))


@router.message(F.text == "ℹ️ Yordam")
@router.message(Command("help"))
async def do_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Mijoz uchun:</b> sotuvchi bergan havolani oching → mahsulotni «➕ Savatga» → "
        "«🛒 Savatcha» → «✅ Buyurtma berish».\n\n"
        "<b>Sotuvchi uchun:</b> «🏬 Do'kon ochish» → mahsulotlarni qo'shing → havolani mijozlarga yuboring. "
        "Buyurtmalar shu yerga keladi.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.message(F.text == "🍽 Menyu")
async def do_menu(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_user_shop(message.from_user.id)
    if not shop:
        await message.answer(
            "Avval sotuvchining havolasini oching.",
            reply_markup=main_kb(message.from_user.id),
        )
        return
    await send_catalog(message, shop)


@router.callback_query(F.data.startswith("cat:"))
async def view_category(cb: CallbackQuery):
    _, shop_id, cat_id = cb.data.split(":")
    products = db.list_products(int(shop_id), category_id=int(cat_id), only_available=True)
    await cb.answer()
    if not products:
        await cb.message.answer("Bu bo'limda mahsulotlar yo'q.")
        return
    for p in products[:20]:
        text, pkb = product_card_customer(p)
        if p["photo"]:
            await cb.message.answer_photo(photo=p["photo"], caption=text, reply_markup=pkb)
        else:
            await cb.message.answer(text, reply_markup=pkb)


@router.message(F.text == "🛒 Savatcha")
@router.message(Command("savat"))
async def do_cart(message: Message, state: FSMContext):
    await state.clear()
    text, kb = cart_view(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "📦 Buyurtmalarim")
async def do_my_orders(message: Message, state: FSMContext):
    await state.clear()
    orders = db.user_orders(message.from_user.id, 5)
    if not orders:
        await message.answer("Hozircha buyurtmangiz yo'q.")
        return
    lines = []
    for o in orders:
        lines.append(
            f"#{o['id']} · {esc(o['shop_name'])} · {money(o['total'])}\n   {STATUS.get(o['status'], o['status'])}"
        )
    await message.answer("📦 <b>So'nggi buyurtmalaringiz</b>\n\n" + "\n\n".join(lines))


# ======================= Sotuvchi: Do'kon va Kategoriyalar =======================

@router.message(F.text == "🏬 Do'kon ochish")
async def reg_start(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        await send_panel(message, shop)
        return

    is_paid = db.get_setting("paid_shop", "0") == "1"
    if is_paid:
        await message.answer("⚠️ Hozirda do'kon ochish pullik rejimda. Iltimos, admin bilan bog'laning.")
        return

    await state.set_state(Reg.name)
    await message.answer(
        "🏬 Do'kon ochamiz!\n\nDo'koningiz nomi nima? (masalan: «Nargiza opa noni»)",
        reply_markup=cancel_kb(),
    )


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 60:
        await message.answer("Nom 2 dan 60 gacha belgi bo'lsin. Qayta yozing:")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    await message.answer(
        "📞 Mijozlar siz bilan bog'lanadigan telefon raqamni yuboring:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Raqamimni yuborish", request_contact=True)],
                [KeyboardButton(text=CANCEL)],
            ],
            resize_keyboard=True,
        ),
    )


@router.message(Reg.phone, F.contact)
@router.message(Reg.phone, F.text)
async def reg_phone(message: Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    phone = parse_phone(raw)
    if not phone:
        await message.answer("Raqam noto'g'ri. Masalan: 901234567")
        return
    data = await state.get_data()
    uid = message.from_user.id
    shop = db.create_shop(uid, data["name"], phone)
    db.set_user_shop(uid, shop["id"])
    await state.clear()
    await message.answer(
        f"🎉 Do'koningiz ochildi!\n\n🔗 Mijozlar uchun havola:\n{shop_link(shop)}",
        reply_markup=main_kb(uid),
        link_preview_options=NOPREVIEW,
    )


@router.message(F.text == "🏪 Mening do'konim")
@router.message(Command("panel"))
async def my_shop(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if not shop:
        await message.answer("Sizda hali do'kon yo'q. «🏬 Do'kon ochish» ni bosing.")
        return
    await send_panel(message, shop)


# Kategoriya qo'shish
@router.callback_query(F.data == "c_add")
async def c_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddCategory.name)
    await cb.message.answer("Yangi kategoriya (bo'lim) nomini kiriting:\n(masalan: <i>Fast Food</i>)", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AddCategory.name, F.text)
async def c_save(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        db.add_category(shop["id"], message.text.strip())
        await message.answer(f"✅ «{message.text.strip()}» kategoriyasi qo'shildi.", reply_markup=main_kb(message.from_user.id))
    await state.clear()


# Mahsulot qo'shish
@router.callback_query(F.data == "p_add")
async def p_add(cb: CallbackQuery, state: FSMContext):
    shop = db.get_shop_by_owner(cb.from_user.id)
    if not shop:
        await cb.answer("Avval do'kon oching", show_alert=True)
        return
    
    categories = db.list_categories(shop["id"])
    if categories:
        kb = InlineKeyboardBuilder()
        for c in categories:
            kb.row(InlineKeyboardButton(text=c["name"], callback_data=f"sel_cat:{c['id']}"))
        kb.row(InlineKeyboardButton(text="🚫 Kategoriya siz", callback_data="sel_cat:0"))
        await cb.message.answer("Mahsulot qaysi kategoriyaga tegishli?", reply_markup=kb.as_markup())
    else:
        await state.update_data(cat_id=None)
        await state.set_state(AddP.name)
        await cb.message.answer("Mahsulot nomi? (masalan: «Patir»)", reply_markup=cancel_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("sel_cat:"))
async def p_sel_cat(cb: CallbackQuery, state: FSMContext):
    cat_id = int(cb.data.split(":")[1])
    await state.update_data(cat_id=cat_id if cat_id > 0 else None)
    await state.set_state(AddP.name)
    await cb.message.answer("Mahsulot nomi? (masalan: «Patir»)", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AddP.name, F.text)
async def addp_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(name=name)
    await state.set_state(AddP.price)
    await message.answer("💰 Narxi necha so'm? (masalan: 5000)")


@router.message(AddP.price, F.text)
async def addp_price(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if price is None:
        await message.answer("Narxni raqam bilan yozing, masalan: 5000")
        return
    await state.update_data(price=price)
    await state.set_state(AddP.photo)
    await message.answer(
        "📷 Mahsulot rasmini yuboring yoki o'tkazib yuboring:",
        reply_markup=reply_kb([[SKIP], [CANCEL]]),
    )


async def finish_add(message: Message, state: FSMContext, photo):
    data = await state.get_data()
    shop = db.get_shop_by_owner(message.from_user.id)
    await state.clear()
    if not shop:
        return
    db.add_product(shop["id"], data["name"], data["price"], photo, data.get("cat_id"))
    await message.answer(
        f"✅ «{esc(data['name'])}» — {money(data['price'])} qo'shildi.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.message(AddP.photo, F.photo)
async def addp_photo(message: Message, state: FSMContext):
    await finish_add(message, state, message.photo[-1].file_id)


@router.message(AddP.photo, F.text == SKIP)
async def addp_skip(message: Message, state: FSMContext):
    await finish_add(message, state, None)


# ======================= Buyurtma Jarayoni (Vaqt va Yetkazish turi bilan) =======================

@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery, state: FSMContext):
    items = [i for i in db.cart_items(cb.from_user.id) if i["available"]]
    if not items:
        await cb.answer("Savatchada mavjud mahsulot yo'q", show_alert=True)
        return
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚚 Yetkazib berish"), KeyboardButton(text="🏃 Olib ketish (Samovivoz)")],
            [KeyboardButton(text=CANCEL)]
        ],
        resize_keyboard=True
    )
    await state.set_state(Order.delivery_type)
    await cb.message.answer("Buyurtma turini tanlang:", reply_markup=kb)
    await cb.answer()


@router.message(Order.delivery_type, F.text)
async def order_del_type(message: Message, state: FSMContext):
    if message.text not in ["🚚 Yetkazib berish", "🏃 Olib ketish (Samovivoz)"]:
        await message.answer("Tugmalardan birini tanlang.")
        return
    await state.update_data(delivery_type=message.text)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡ Imkon qadar tezroq")],
            [KeyboardButton(text="🕒 Bugun soat 18:00 ga"), KeyboardButton(text="🕒 Bugun soat 20:00 ga")],
            [KeyboardButton(text=CANCEL)]
        ],
        resize_keyboard=True
    )
    await state.set_state(Order.delivery_time)
    await message.answer("Qachonga yetkazilsin / tayyor bo'lsin?", reply_markup=kb)


@router.message(Order.delivery_time, F.text)
async def order_del_time(message: Message, state: FSMContext):
    await state.update_data(delivery_time=message.text)
    await state.set_state(Order.phone)
    await message.answer(
        "📱 Telefon raqamingizni yuboring:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Raqamimni yuborish", request_contact=True)],
                [KeyboardButton(text=CANCEL)],
            ],
            resize_keyboard=True,
        ),
    )


@router.message(Order.phone, F.contact)
@router.message(Order.phone, F.text)
async def order_phone(message: Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    phone = parse_phone(raw)
    if not phone:
        await message.answer("Raqam noto'g'ri. Masalan: 901234567")
        return
    await state.update_data(phone=phone)
    await state.set_state(Order.address)
    await message.answer("📍 Manzilingizni yozib yuboring (yoki lokatsiya kiriting):", reply_markup=cancel_kb())


@router.message(Order.address, F.text)
async def order_address(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = message.from_user.id
    oid = db.create_order(
        uid,
        message.from_user.full_name,
        data["phone"],
        message.text.strip(),
        data["delivery_type"],
        data["delivery_time"]
    )
    await state.clear()
    if oid is None:
        await message.answer("Savatchangiz bo'sh.", reply_markup=main_kb(uid))
        return
    
    o = db.get_order(oid)
    shop = db.get_shop(o["shop_id"])
    items = db.order_items(oid)
    await message.answer(
        f"✅ Buyurtmangiz #{oid} qabul qilindi!\nSotuvchi tez orada bog'lanadi: {esc(shop['phone'])}",
        reply_markup=main_kb(uid),
    )
    try:
        await message.bot.send_message(
            shop["owner_id"],
            "🆕 <b>Yangi buyurtma!</b>\n\n" + order_text(o, items, True),
            reply_markup=status_kb(o),
        )
    except Exception as e:
        log.error("Xabar bormadi: %s", e)


# ======================= Server funksiyasi =======================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 18012))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    threading.Thread(target=run_server, daemon=True).start()

    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Token topilmadi. Render'da BOT_TOKEN ni kiriting.")
    
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Boshlash"),
            BotCommand(command="savat", description="Savatcha"),
            BotCommand(command="panel", description="Mening do'konim"),
            BotCommand(command="help", description="Yordam"),
        ]
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot ishga tushdi: @%s", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())