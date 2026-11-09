"""Kichik sotuvchilar uchun buyurtma boti (aiogram 3 + SQLite).
Admin panel, to'lov sozlamalari, kategoriyalar va shikoyat bo'limi qo'shilgan.
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


# ======================= YORDAMCHI FUNKSIYALAR =======================

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
    rows = [["🍽 Menyu", "🛒 Savatcha"], ["📦 Buyurtmalarim", "✍️ Shikoyat va taklif"]]
    if db.get_shop_by_owner(uid):
        rows.append(["🏪 Mening do'konim", "ℹ️ Yordam"])
    else:
        rows.append(["🏬 Do'kon ochish", "ℹ️ Yordam"])
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


async def send_catalog(message: Message, shop, cat_id=None):
    cats = db.get_categories(shop["id"])
    products = db.list_products(shop["id"], category_id=cat_id, only_available=True)
    
    head = f"🏪 <b>{esc(shop['name'])}</b>"
    if shop["phone"]:
        head += f"\n📞 {esc(shop['phone'])}"

    kb_cat = InlineKeyboardBuilder()
    if cats:
        for c in cats:
            kb_cat.row(InlineKeyboardButton(text=f"📁 {c['name']}", callback_data=f"cat:{shop['id']}:{c['id']}"))
        kb_cat.row(InlineKeyboardButton(text="📋 Barcha mahsulotlar", callback_data=f"cat:{shop['id']}:all"))

    if not products:
        await message.answer(head + "\n\nHozircha mahsulot yo'q.", reply_markup=kb_cat.as_markup() if cats else None)
        return

    await message.answer(head + "\n\nTanlang va «➕ Savatga» ni bosing 👇", reply_markup=kb_cat.as_markup() if cats else None)
    for p in products[:30]:
        text, kb = product_card_customer(p)
        if p["photo"]:
            await message.answer_photo(photo=p["photo"], caption=text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
    
    kb_cart = InlineKeyboardBuilder()
    kb_cart.row(InlineKeyboardButton(text="🛒 Savatchani ochish", callback_data="cart"))
    await message.answer("Tanlab bo'lgach savatchani oching:", reply_markup=kb_cart.as_markup())


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
    kb.row(
        InlineKeyboardButton(text="✏️ Do'kon nomi", callback_data="s_name"),
    )
    await message.answer(text, reply_markup=kb.as_markup(), link_preview_options=NOPREVIEW)


def owned_product(uid: int, pid: int):
    p = db.get_product(pid)
    if p is None:
        return None
    shop = db.get_shop(p["shop_id"])
    if shop is None or shop["owner_id"] != uid:
        return None
    return p


# ======================= HOLATLAR (FSM STATES) =======================

class Reg(StatesGroup):
    name = State()
    phone = State()

class AddP(StatesGroup):
    name = State()
    price = State()
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
    user_id = State()
    text = State()


# ======================= ADMIN PANEL & SOZLAMALAR =======================

def admin_kb():
    paid = db.get_setting("is_paid", "0") == "1"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="💰 Narxni belgilash", callback_data="adm_set_price"))
    kb.row(InlineKeyboardButton(text="💳 Karta raqam va F.I.SH", callback_data="adm_set_card"))
    kb.row(InlineKeyboardButton(text=f"🔒 To'lov: {'🔴 OCHIK (PULLIK)' if paid else '🟢 BEPUL'}", callback_data="adm_toggle_paid"))
    kb.row(InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"))
    kb.row(InlineKeyboardButton(text="📢 Xabar tarqatish", callback_data="adm_broadcast"))
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    price = db.get_setting("shop_price", "0")
    card = db.get_setting("card_number", "Kiritilmagan")
    owner = db.get_setting("card_owner", "Kiritilmagan")
    text = (
        "⚙️ <b>Super Admin Panel</b>\n\n"
        f"💰 Do'kon ochish narxi: <b>{money(price)}</b>\n"
        f"💳 Karta raqam: <b>{esc(card)}</b>\n"
        f"👤 F.I.SH: <b>{esc(owner)}</b>"
    )
    await message.answer(text, reply_markup=admin_kb())


@router.callback_query(F.data == "adm_toggle_paid")
async def adm_toggle_paid(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    cur = db.get_setting("is_paid", "0")
    new_val = "1" if cur == "0" else "0"
    db.set_setting("is_paid", new_val)
    await cb.answer("To'lov rejimi o'zgartirildi!")
    await safe_edit(cb.message, cb.message.text, admin_kb())


@router.callback_query(F.data == "adm_set_price")
async def adm_set_price(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminSetPrice.price)
    await cb.message.answer("Do'kon ochish narxini kiriting (faqat raqam, masalan: 15000):", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AdminSetPrice.price, F.text)
async def adm_save_price(message: Message, state: FSMContext):
    p = parse_price(message.text)
    if p is None:
        await message.answer("Narxni to'g'ri kiriting:")
        return
    db.set_setting("shop_price", p)
    await state.clear()
    await message.answer(f"✅ Do'kon ochish narxi {money(p)} deb belgilandi.", reply_markup=main_kb(message.from_user.id))


@router.callback_query(F.data == "adm_set_card")
async def adm_set_card(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminSetCard.card)
    await cb.message.answer("💳 Karta raqamini kiriting (masalan: 8600 0000 0000 0000):", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AdminSetCard.card, F.text)
async def adm_save_card(message: Message, state: FSMContext):
    await state.update_data(card=message.text.strip())
    await state.set_state(AdminSetCard.owner)
    await message.answer("👤 Karta egasining F.I.SH ni kiriting (masalan: Aliyev Vali):")


@router.message(AdminSetCard.owner, F.text)
async def adm_save_owner(message: Message, state: FSMContext):
    data = await state.get_data()
    db.set_setting("card_number", data["card"])
    db.set_setting("card_owner", message.text.strip())
    await state.clear()
    await message.answer("✅ Karta rekvizitlari muvaffaqiyatli saqlandi!", reply_markup=main_kb(message.from_user.id))


@router.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    t = db.totals()
    await cb.message.answer(
        f"📊 <b>Bot Statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: {t['users']}\n"
        f"🏪 Do'konlar: {t['shops']}\n"
        f"🍽 Mahsulotlar: {t['products']}\n"
        f"📦 Buyurtmalar: {t['orders']}\n"
        f"💰 Umumiy aylanma: {money(t['sum'])}"
    )
    await cb.answer()


@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminBroadcast.text)
    await cb.message.answer("Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AdminBroadcast.text, F.text)
async def adm_send_broadcast(message: Message, state: FSMContext):
    users = db.all_users()
    txt = message.text
    await state.clear()
    await message.answer(f"📢 Xabar {len(users)} kishiga yuborilmoqda...")
    count = 0
    for u in users:
        try:
            await message.bot.send_message(u["id"], txt)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ Xabar {count} ta foydalanuvchiga muvaffaqiyatli yetkazildi.")


# ======================= SHIKOYAT VA TAKLIFLAR =======================

@router.message(F.text == "✍️ Shikoyat va taklif")
async def fb_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FeedbackState.text)
    await message.answer(
        "Fikr, shikoyat yoki taklifingiz bo'lsa yozib qoldiring.\n"
        "Xabaringiz to'g'ridan-to'g'ri admin'ga yetkaziladi:",
        reply_markup=cancel_kb()
    )


@router.message(FeedbackState.text, F.text)
async def fb_save(message: Message, state: FSMContext):
    uid = message.from_user.id
    name = message.from_user.full_name
    txt = message.text.strip()
    await state.clear()
    
    fid = db.add_feedback(uid, name, txt)
    await message.answer("Rahmat! Xabaringiz admin'ga yetkazildi.", reply_markup=main_kb(uid))
    
    if ADMIN_ID:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✍️ Javob yozish", callback_data=f"reply_fb:{uid}"))
        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"📩 <b>Yangi shikoyat/taklif!</b>\n\n"
                f"👤 Kimdan: {esc(name)} (ID: <code>{uid}</code>)\n"
                f"💬 Matn:\n<i>{esc(txt)}</i>",
                reply_markup=kb.as_markup()
            )
        except Exception as e:
            log.error("Adminga feedback bormadi: %s", e)


@router.callback_query(F.data.startswith("reply_fb:"))
async def reply_fb_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    uid = int(cb.data.split(":")[1])
    await state.set_state(AdminReply.text)
    await state.update_data(target_uid=uid)
    await cb.message.answer(f"Foydalanuvchiga (ID: {uid}) javob xabaringizni yozing:", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AdminReply.text, F.text)
async def reply_fb_send(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data["target_uid"]
    txt = message.text.strip()
    await state.clear()
    try:
        await message.bot.send_message(uid, f"👨‍💻 <b>Admin javobi:</b>\n\n{esc(txt)}")
        await message.answer("✅ Javobingiz foydalanuvchiga yuborildi.", reply_markup=main_kb(message.from_user.id))
    except Exception as e:
        await message.answer(f"❌ Xabar yuborishda xatolik: {e}")


# ======================= UMUMIY BUYRUQLAR =======================

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
        "Bu bot orqali do'konlardan oson buyurtma berishingiz yoki o'z do'koningizni ochishingiz mumkin.",
        reply_markup=main_kb(uid),
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Sizning ID: <code>{message.from_user.id}</code>")


@router.message(F.text == CANCEL)
async def do_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb(message.from_user.id))


@router.message(F.text == "ℹ️ Yordam")
@router.message(Command("help"))
async def do_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Mijoz uchun:</b> Havolani oching → mahsulotni tanlang → savatchaga o'ting va buyurtma bering.\n\n"
        "<b>Sotuvchi uchun:</b> «🏬 Do'kon ochish» ni bosing, mahsulotlarni qo'shing va havolani ulashing.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.message(F.text == "🍽 Menyu")
async def do_menu(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_user_shop(message.from_user.id)
    if not shop:
        await message.answer("Avval sotuvchining havolasini oching.", reply_markup=main_kb(message.from_user.id))
        return
    await send_catalog(message, shop)


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
        lines.append(f"#{o['id']} · {esc(o['shop_name'])} · {money(o['total'])}\n   {STATUS.get(o['status'], o['status'], o['status'])}")
    await message.answer("📦 <b>So'nggi buyurtmalaringiz</b>\n\n" + "\n\n".join(lines))


# ======================= SOTUVCHI: RO'YXATDAN O'TISH =======================

@router.message(F.text == "🏬 Do'kon ochish")
async def reg_start(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        await send_panel(message, shop)
        return

    is_paid = db.get_setting("is_paid", "0") == "1"
    if is_paid:
        price = db.get_setting("shop_price", "0")
        card = db.get_setting("card_number", "8600...")
        owner = db.get_setting("card_owner", "Admin")
        text = (
            f"🏬 <b>Do'kon ochish narxi atigi {money(price)}.</b>\n"
            f"To'lov qilib o'z do'koningizni ochishingiz mumkin.\n\n"
            f"<b>To'lov qilish uchun:</b>\n"
            f"💳 Karta raqam: <code>{esc(card)}</code>\n"
            f"👤 F.I.SH: <b>{esc(owner)}</b>\n\n"
            f"To'lovni amalga oshirgach, chekni va do'kon nomini adminga yuboring!"
        )
        await message.answer(text, reply_markup=main_kb(message.from_user.id))
        return

    await state.set_state(Reg.name)
    await message.answer("🏬 Do'kon ochamiz!\n\nDo'koningiz nomi nima?", reply_markup=cancel_kb())


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 60:
        await message.answer("Nom 2 dan 60 gacha belgi bo'lsin:")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    await message.answer(
        "📞 Telefon raqamingizni yuboring:",
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
    await message.answer(f"🎉 Do'koningiz ochildi!\n\n🔗 Havola: {shop_link(shop)}", reply_markup=main_kb(uid), link_preview_options=NOPREVIEW)


@router.message(F.text == "🏪 Mening do'konim")
@router.message(Command("panel"))
async def my_shop(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if not shop:
        await message.answer("Sizda hali do'kon yo'q.")
        return
    await send_panel(message, shop)


# ======================= MAHSULOT VA KATEGORIYALAR =======================

@router.callback_query(F.data == "c_add")
async def c_add(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return
    await state.set_state(AddCat.name)
    await cb.message.answer("Kategoriya nomi (masalan: Ichimliklar):", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AddCat.name, F.text)
async def c_save(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        db.add_category(shop["id"], message.text.strip())
    await state.clear()
    await message.answer("✅ Kategoriya qo'shildi.", reply_markup=main_kb(message.from_user.id))


@router.callback_query(F.data == "p_add")
async def p_add(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        return
    await state.set_state(AddP.name)
    await cb.message.answer("Mahsulot nomi?", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AddP.name, F.text)
async def addp_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddP.price)
    await message.answer("💰 Narxi necha so'm?")


@router.message(AddP.price, F.text)
async def addp_price(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if price is None:
        await message.answer("Narxni faqat raqamlar bilan yozing:")
        return
    await state.update_data(price=price)
    await state.set_state(AddP.photo)
    await message.answer("📷 Rasmini yuboring yoki o'tkazib yuboring:", reply_markup=reply_kb([[SKIP], [CANCEL]]))


async def finish_add(message: Message, state: FSMContext, photo):
    data = await state.get_data()
    shop = db.get_shop_by_owner(message.from_user.id)
    await state.clear()
    if shop:
        db.add_product(shop["id"], data["name"], data["price"], photo)
    await message.answer(f"✅ «{esc(data['name'])}» qo'shildi.", reply_markup=main_kb(message.from_user.id))


@router.message(AddP.photo, F.photo)
async def addp_photo(message: Message, state: FSMContext):
    await finish_add(message, state, message.photo[-1].file_id)


@router.message(AddP.photo, F.text == SKIP)
async def addp_skip(message: Message, state: FSMContext):
    await finish_add(message, state, None)


@router.callback_query(F.data == "p_list")
async def p_list(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)
    if not shop:
        return
    products = db.list_products(shop["id"])
    await cb.answer()
    if not products:
        await cb.message.answer("Hali mahsulot yo'q.")
        return
    for p in products[:20]:
        text, kb = product_card_seller(p)
        if p["photo"]:
            await cb.message.answer_photo(photo=p["photo"], caption=text, reply_markup=kb)
        else:
            await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pt:"))
async def p_toggle(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if p:
        db.set_available(p["id"], not p["available"])
        p = db.get_product(p["id"])
        text, kb = product_card_seller(p)
        await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("pd:"))
async def p_delete(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if p:
        db.delete_product(p["id"])
        await safe_edit(cb.message, f"🗑 «{esc(p['name'])}» o'chirildi.", None)
    await cb.answer()


@router.callback_query(F.data == "s_name")
async def s_name(cb: CallbackQuery, state: FSMContext):
    if db.get_shop_by_owner(cb.from_user.id):
        await state.set_state(Rename.name)
        await cb.message.answer("Do'konning yangi nomi:", reply_markup=cancel_kb())
    await cb.answer()


@router.message(Rename.name, F.text)
async def s_name_set(message: Message, state: FSMContext):
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        db.rename_shop(shop["id"], message.text.strip())
    await state.clear()
    await message.answer("✅ Nom o'zgartirildi.", reply_markup=main_kb(message.from_user.id))


# ======================= BUYURTMALAR VA SAVATCHA =======================

@router.callback_query(F.data == "o_list")
async def o_list(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)
    await cb.answer()
    if not shop:
        return
    orders = db.active_orders(shop["id"])
    if not orders:
        await cb.message.answer("Faol buyurtmalar yo'q.")
        return
    for o in reversed(orders):
        await cb.message.answer(order_text(o, db.order_items(o["id"]), True), reply_markup=status_kb(o))


@router.callback_query(F.data.startswith("st:"))
async def order_status(cb: CallbackQuery):
    _, oid, new = cb.data.split(":")
    o = db.get_order(int(oid))
    if o:
        db.set_status(o["id"], new)
        o = db.get_order(o["id"])
        await safe_edit(cb.message, order_text(o, db.order_items(o["id"]), True), status_kb(o))
    await cb.answer()


@router.callback_query(F.data.startswith("add:"))
async def cart_add(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = db.get_product(pid)
    if p and p["available"]:
        db.upsert_user(cb.from_user.id, cb.from_user.full_name)
        qty, cleared = db.cart_add(cb.from_user.id, pid)
        msg = f"✅ Savatda: {qty} ta"
        if cleared:
            msg += "\n(Boshqa do'kon mahsulotlari tozalandi)"
        await cb.answer(msg, show_alert=cleared)


@router.callback_query(F.data == "cart")
async def cart_open(cb: CallbackQuery):
    text, kb = cart_view(cb.from_user.id)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("inc:"))
async def cart_inc(cb: CallbackQuery):
    db.cart_change(cb.from_user.id, int(cb.data.split(":")[1]), +1)
    text, kb = cart_view(cb.from_user.id)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("dec:"))
async def cart_dec(cb: CallbackQuery):
    db.cart_change(cb.from_user.id, int(cb.data.split(":")[1]), -1)
    text, kb = cart_view(cb.from_user.id)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "clear")
async def cart_clear(cb: CallbackQuery):
    db.cart_clear(cb.from_user.id)
    text, kb = cart_view(cb.from_user.id)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery, state: FSMContext):
    items = [i for i in db.cart_items(cb.from_user.id) if i["available"]]
    if not items:
        await cb.answer("Savatcha bo'sh!", show_alert=True)
        return
    await state.set_state(Order.phone)
    await cb.message.answer(
        "📱 Telefon raqamingizni yuboring:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Raqamimni yuborish", request_contact=True)], [KeyboardButton(text=CANCEL)]],
            resize_keyboard=True,
        ),
    )
    await cb.answer()


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
    await message.answer("📍 Manzilingizni yozing:", reply_markup=cancel_kb())


@router.message(Order.address, F.text)
async def order_address(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = message.from_user.id
    oid = db.create_order(uid, message.from_user.full_name, data["phone"], message.text.strip())
    await state.clear()
    if oid:
        o = db.get_order(oid)
        shop = db.get_shop(o["shop_id"])
        await message.answer(f"✅ Buyurtmangiz #{oid} qabul qilindi!", reply_markup=main_kb(uid))
        try:
            await message.bot.send_message(shop["owner_id"], "🆕 <b>Yangi buyurtma!</b>\n\n" + order_text(o, db.order_items(oid), True), reply_markup=status_kb(o))
        except Exception as e:
            log.error("Sotuvchiga xabar bormadi: %s", e)


# ======================= RENDER SERVER =======================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=run_server, daemon=True).start()

    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN topilmadi!")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.delete_webhook(drop_pending_updates+True) if False else None
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="savat", description="Savatcha"),
        BotCommand(command="panel", description="Mening do'konim"),
        BotCommand(command="admin", description="Admin Panel"),
        BotCommand(command="help", description="Yordam"),
    ])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
