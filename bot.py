"""Kichik sotuvchilar uchun buyurtma boti (aiogram 3 + SQLite).

Sotuvchi botda do'kon ochadi, mahsulot qo'shadi va mijozlarga havola beradi.
Mijoz havola orqali menyuni ko'radi, savatcha to'ldiradi va buyurtma beradi.
Buyurtma sotuvchiga keladi, sotuvchi holatini tugmalar bilan o'zgartiradi.
"""
import asyncio
import html
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# Hosting'da faqat /data papkasi saqlanib qoladi, shuning uchun baza o'sha yerga yoziladi
_default_db = "/data/shop.db" if os.path.isdir("/data") else "shop.db"
db = DB(os.environ.get("DB_PATH", _default_db))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
BOT_USERNAME = ""
TZ = timezone(timedelta(hours=5))  # O'zbekiston vaqti

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


# ======================= yordamchi =======================

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
    rows = [["🍽 Menyu", "🛒 Savatcha"], ["📦 Buyurtmalarim"]]
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
    lines = [f"{i}. {esc(it['name'])} — {it['qty']} × {money(it['price'])}" for i, it in enumerate(items, 1)]
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


async def send_catalog(message: Message, shop):
    products = db.list_products(shop["id"], only_available=True)
    head = f"🏪 <b>{esc(shop['name'])}</b>"
    if shop["phone"]:
        head += f"\n📞 {esc(shop['phone'])}"
    if not products:
        await message.answer(head + "\n\nHozircha mahsulot yo'q.")
        return
    await message.answer(head + "\n\nTanlang va «➕ Savatga» ni bosing 👇")
    for p in products[:30]:
        text, kb = product_card_customer(p)
        if p["photo"]:
            await message.answer_photo(photo=p["photo"], caption=text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🛒 Savatchani ochish", callback_data="cart"))
    await message.answer("Tanlab bo'lgach savatchani oching:", reply_markup=kb.as_markup())


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
        InlineKeyboardButton(text="📋 Mahsulotlar", callback_data="p_list"),
    )
    kb.row(
        InlineKeyboardButton(text="📦 Faol buyurtmalar", callback_data="o_list"),
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


# ======================= holatlar =======================

class Reg(StatesGroup):
    name = State()
    phone = State()


class AddP(StatesGroup):
    name = State()
    price = State()
    photo = State()


class EditPrice(StatesGroup):
    price = State()


class Rename(StatesGroup):
    name = State()


class Order(StatesGroup):
    phone = State()
    address = State()


# ======================= umumiy buyruqlar =======================

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


@router.message(Command("stat"))
async def cmd_stat(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    t = db.totals()
    await message.answer(
        f"📊 Foydalanuvchilar: {t['users']}\n🏪 Do'konlar: {t['shops']}\n"
        f"🍽 Mahsulotlar: {t['products']}\n📦 Buyurtmalar: {t['orders']}\n💰 Aylanma: {money(t['sum'])}"
    )


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
        "<b>Sotuvchi uchun:</b> «🏬 Do'kon ochish» → mahsulotlarni qo'shing → havolani mijozlarga yuboring "
        "(Telegram, Instagram, kanal). Buyurtmalar shu yerga keladi, holatini tugmalar bilan o'zgartirasiz.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.message(F.text == "🍽 Menyu")
async def do_menu(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_user_shop(message.from_user.id)
    if not shop:
        await message.answer(
            "Avval sotuvchining havolasini oching (u sizga yuborgan).",
            reply_markup=main_kb(message.from_user.id),
        )
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
        lines.append(
            f"#{o['id']} · {esc(o['shop_name'])} · {money(o['total'])}\n   {STATUS.get(o['status'], o['status'])}"
        )
    await message.answer("📦 <b>So'nggi buyurtmalaringiz</b>\n\n" + "\n\n".join(lines))


# ======================= sotuvchi: ro'yxatdan o'tish =======================

@router.message(F.text == "🏬 Do'kon ochish")
async def reg_start(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if shop:
        await send_panel(message, shop)
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
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Birinchi mahsulotni qo'shish", callback_data="p_add"))
    await message.answer(
        f"🎉 Do'koningiz ochildi!\n\n🔗 Mijozlar uchun havola:\n{shop_link(shop)}\n\n"
        "Uni Telegram, Instagram yoki kanalingizga joylang. Avval mahsulotlarni qo'shing 👇",
        reply_markup=main_kb(uid),
        link_preview_options=NOPREVIEW,
    )
    await message.answer("Boshlaymiz:", reply_markup=kb.as_markup())


@router.message(F.text == "🏪 Mening do'konim")
@router.message(Command("panel"))
async def my_shop(message: Message, state: FSMContext):
    await state.clear()
    shop = db.get_shop_by_owner(message.from_user.id)
    if not shop:
        await message.answer("Sizda hali do'kon yo'q. «🏬 Do'kon ochish» ni bosing.")
        return
    await send_panel(message, shop)


# ======================= sotuvchi: mahsulotlar =======================

@router.callback_query(F.data == "p_add")
async def p_add(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        await cb.answer("Avval do'kon oching", show_alert=True)
        return
    await state.set_state(AddP.name)
    await cb.message.answer("Mahsulot nomi? (masalan: «Patir»)", reply_markup=cancel_kb())
    await cb.answer()


@router.message(AddP.name, F.text)
async def addp_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 1 <= len(name) <= 80:
        await message.answer("Nom 1 dan 80 gacha belgi bo'lsin:")
        return
    await state.update_data(name=name)
    await state.set_state(AddP.price)
    await message.answer("💰 Narxi necha so'm? (faqat raqam, masalan: 5000)")


@router.message(AddP.price, F.text)
async def addp_price(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if price is None:
        await message.answer("Narxni raqam bilan yozing, masalan: 5000")
        return
    await state.update_data(price=price)
    await state.set_state(AddP.photo)
    await message.answer(
        "📷 Mahsulot rasmini yuboring (rasm bo'lsa ko'proq sotiladi) yoki o'tkazib yuboring:",
        reply_markup=reply_kb([[SKIP], [CANCEL]]),
    )


async def finish_add(message: Message, state: FSMContext, photo):
    data = await state.get_data()
    shop = db.get_shop_by_owner(message.from_user.id)
    await state.clear()
    if not shop:
        return
    db.add_product(shop["id"], data["name"], data["price"], photo)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Yana qo'shish", callback_data="p_add"),
        InlineKeyboardButton(text="📋 Mahsulotlar", callback_data="p_list"),
    )
    await message.answer(
        f"✅ «{esc(data['name'])}» — {money(data['price'])} qo'shildi.",
        reply_markup=main_kb(message.from_user.id),
    )
    await message.answer("Davom etamizmi?", reply_markup=kb.as_markup())


@router.message(AddP.photo, F.photo)
async def addp_photo(message: Message, state: FSMContext):
    await finish_add(message, state, message.photo[-1].file_id)


@router.message(AddP.photo, F.text == SKIP)
async def addp_skip(message: Message, state: FSMContext):
    await finish_add(message, state, None)


@router.message(AddP.photo)
async def addp_photo_bad(message: Message):
    await message.answer("Rasm yuboring yoki «⏭ O'tkazib yuborish» ni bosing.")


@router.callback_query(F.data == "p_list")
async def p_list(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)
    if not shop:
        await cb.answer()
        return
    products = db.list_products(shop["id"])
    await cb.answer()
    if not products:
        await cb.message.answer("Hali mahsulot yo'q. «➕ Mahsulot qo'shish» ni bosing (/panel).")
        return
    for p in products[:20]:
        text, kb = product_card_seller(p)
        if p["photo"]:
            await cb.message.answer_photo(photo=p["photo"], caption=text, reply_markup=kb)
        else:
            await cb.message.answer(text, reply_markup=kb)
    if len(products) > 20:
        await cb.message.answer(f"Faqat dastlabki 20 tasi ko'rsatildi (jami {len(products)} ta).")


@router.callback_query(F.data.startswith("pt:"))
async def p_toggle(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if not p:
        await cb.answer("Topilmadi", show_alert=True)
        return
    db.set_available(p["id"], not p["available"])
    p = db.get_product(p["id"])
    text, kb = product_card_seller(p)
    await safe_edit(cb.message, text, kb)
    await cb.answer("Yangilandi")


@router.callback_query(F.data.startswith("pp:"))
async def p_price(cb: CallbackQuery, state: FSMContext):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if not p:
        await cb.answer("Topilmadi", show_alert=True)
        return
    await state.set_state(EditPrice.price)
    await state.update_data(pid=p["id"])
    await cb.message.answer(
        f"«{esc(p['name'])}» uchun yangi narx (so'm):", reply_markup=cancel_kb()
    )
    await cb.answer()


@router.message(EditPrice.price, F.text)
async def p_price_set(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if price is None:
        await message.answer("Narxni raqam bilan yozing, masalan: 5000")
        return
    data = await state.get_data()
    p = owned_product(message.from_user.id, data.get("pid", 0))
    await state.clear()
    if not p:
        await message.answer("Mahsulot topilmadi.", reply_markup=main_kb(message.from_user.id))
        return
    db.set_price(p["id"], price)
    await message.answer(
        f"✅ Yangi narx: {money(price)}. «📋 Mahsulotlar» (/panel) da ko'rasiz.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.callback_query(F.data.startswith("pd:"))
async def p_delete_ask(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if not p:
        await cb.answer("Topilmadi", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🗑 Ha, o'chirish", callback_data=f"py:{p['id']}"),
        InlineKeyboardButton(text="↩️ Yo'q", callback_data=f"pn:{p['id']}"),
    )
    await safe_edit(cb.message, f"«{esc(p['name'])}» o'chirilsinmi?", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("py:"))
async def p_delete_yes(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if not p:
        await cb.answer("Topilmadi", show_alert=True)
        return
    db.delete_product(p["id"])
    await safe_edit(cb.message, f"🗑 «{esc(p['name'])}» o'chirildi.", None)
    await cb.answer()


@router.callback_query(F.data.startswith("pn:"))
async def p_delete_no(cb: CallbackQuery):
    p = owned_product(cb.from_user.id, int(cb.data.split(":")[1]))
    if not p:
        await cb.answer()
        return
    text, kb = product_card_seller(p)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "s_name")
async def s_name(cb: CallbackQuery, state: FSMContext):
    if not db.get_shop_by_owner(cb.from_user.id):
        await cb.answer()
        return
    await state.set_state(Rename.name)
    await cb.message.answer("Do'konning yangi nomi:", reply_markup=cancel_kb())
    await cb.answer()


@router.message(Rename.name, F.text)
async def s_name_set(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 60:
        await message.answer("Nom 2 dan 60 gacha belgi bo'lsin:")
        return
    shop = db.get_shop_by_owner(message.from_user.id)
    await state.clear()
    if shop:
        db.rename_shop(shop["id"], name)
    await message.answer("✅ Nom o'zgartirildi.", reply_markup=main_kb(message.from_user.id))


# ======================= sotuvchi: buyurtmalar =======================

@router.callback_query(F.data == "o_list")
async def o_list(cb: CallbackQuery):
    shop = db.get_shop_by_owner(cb.from_user.id)
    await cb.answer()
    if not shop:
        return
    orders = db.active_orders(shop["id"])
    if not orders:
        await cb.message.answer("Faol buyurtma yo'q 👌")
        return
    for o in reversed(orders):
        await cb.message.answer(
            order_text(o, db.order_items(o["id"]), True), reply_markup=status_kb(o)
        )


@router.callback_query(F.data.startswith("st:"))
async def order_status(cb: CallbackQuery):
    _, oid, new = cb.data.split(":")
    o = db.get_order(int(oid))
    if not o:
        await cb.answer("Topilmadi", show_alert=True)
        return
    shop = db.get_shop(o["shop_id"])
    if not shop or shop["owner_id"] != cb.from_user.id:
        await cb.answer("Bu sizning buyurtmangiz emas", show_alert=True)
        return
    if new not in NEXT.get(o["status"], []):
        await cb.answer("Bu holat allaqachon o'zgargan", show_alert=True)
        o = db.get_order(o["id"])
        await safe_edit(cb.message, order_text(o, db.order_items(o["id"]), True), status_kb(o))
        return
    db.set_status(o["id"], new)
    o = db.get_order(o["id"])
    await safe_edit(cb.message, order_text(o, db.order_items(o["id"]), True), status_kb(o))
    await cb.answer(STATUS[new])

    notes = {
        "accepted": f"✅ Buyurtmangiz #{o['id']} qabul qilindi. Tayyorlanmoqda.",
        "ready": f"🍽 Buyurtmangiz #{o['id']} tayyor!",
        "delivered": f"🚚 Buyurtmangiz #{o['id']} yetkazildi. Xaridingiz uchun rahmat! 🙏",
        "canceled": f"❌ Buyurtma #{o['id']} bekor qilindi. Savol bo'lsa: {shop['phone']}",
    }
    try:
        await cb.bot.send_message(o["customer_id"], f"🏪 {esc(shop['name'])}\n{notes[new]}")
    except Exception as e:  # mijoz botni to'xtatgan bo'lishi mumkin
        log.warning("mijozga xabar bormadi: %s", e)


# ======================= mijoz: savatcha =======================

@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("add:"))
async def cart_add(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = db.get_product(pid)
    if not p or not p["available"]:
        await cb.answer("Bu mahsulot hozir yo'q", show_alert=True)
        return
    db.upsert_user(cb.from_user.id, cb.from_user.full_name)
    qty, cleared = db.cart_add(cb.from_user.id, pid)
    msg = f"✅ Savatda: {qty} ta"
    if cleared:
        msg += "\n(boshqa do'kon mahsulotlari savatdan olib tashlandi)"
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
    await cb.answer("Savatcha tozalandi")


# ======================= mijoz: buyurtma berish =======================

@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery, state: FSMContext):
    items = [i for i in db.cart_items(cb.from_user.id) if i["available"]]
    if not items:
        await cb.answer("Savatchada mavjud mahsulot yo'q", show_alert=True)
        return
    await state.set_state(Order.phone)
    await cb.message.answer(
        "📱 Telefon raqamingizni yuboring (sotuvchi siz bilan bog'lanishi uchun):",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Raqamimni yuborish", request_contact=True)],
                [KeyboardButton(text=CANCEL)],
            ],
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
    await message.answer(
        "📍 Manzilingiz va qachonga kerakligini yozing.\n"
        "Masalan: «Navoiy ko'chasi 12-uy, ertaga soat 10:00»",
        reply_markup=cancel_kb(),
    )


@router.message(Order.address, F.text)
async def order_address(message: Message, state: FSMContext):
    address = message.text.strip()
    if len(address) < 3:
        await message.answer("Manzilni to'liqroq yozing:")
        return
    data = await state.get_data()
    uid = message.from_user.id
    oid = db.create_order(uid, message.from_user.full_name, data["phone"], address)
    await state.clear()
    if oid is None:
        await message.answer(
            "Savatchangiz bo'sh yoki mahsulotlar tugagan.", reply_markup=main_kb(uid)
        )
        return
    o = db.get_order(oid)
    shop = db.get_shop(o["shop_id"])
    items = db.order_items(oid)
    await message.answer(
        f"✅ Buyurtmangiz #{oid} qabul qilindi!\n"
        f"Sotuvchi tez orada siz bilan bog'lanadi: {esc(shop['phone'])}\n"
        "Holati o'zgarganda shu yerga xabar keladi.",
        reply_markup=main_kb(uid),
    )
    try:
        await message.bot.send_message(
            shop["owner_id"],
            "🆕 <b>Yangi buyurtma!</b>\n\n" + order_text(o, items, True),
            reply_markup=status_kb(o),
        )
    except Exception as e:
        log.error("sotuvchiga xabar bormadi: %s", e)
        await message.answer(
            "⚠️ Sotuvchiga xabar yetkazib bo'lmadi. Iltimos, unga to'g'ridan-to'g'ri qo'ng'iroq qiling: "
            + esc(shop["phone"])
        )


def start_keepalive():
    """Render kabi hostinglar uchun: oddiy veb-sahifa. Tashqi "uyg'otuvchi" (UptimeRobot) shu
    sahifaga har 5 daqiqada so'rov yuborib, botni uxlab qolishdan saqlaydi."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def _ok(self, with_body):
            data = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if with_body:
                self.wfile.write(data)

        def do_GET(self):
            self._ok(True)

        def do_HEAD(self):
            self._ok(False)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Keep-alive sahifasi: port %s", port)


# ======================= ishga tushirish =======================

async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_keepalive()
    token = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Token topilmadi. BOT_TOKEN (yoki TELEGRAM_BOT_TOKEN) ni kiriting.")
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
