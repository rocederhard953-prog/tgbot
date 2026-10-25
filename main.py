import asyncio
from contextlib import contextmanager
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, FSInputFile
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
DB_FILE = os.getenv("DB_FILE", "bot_data.db")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()


class AddButton(StatesGroup):
    title = State()
    parent = State()
    kind = State()
    content = State()


class EditSetting(StatesGroup):
    welcome = State()
    menu_photo = State()


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class Support(StatesGroup):
    message = State()


class SupportReply(StatesGroup):
    message = State()


class EditButton(StatesGroup):
    choose = State()
    title = State()
    content = State()


def channel_chat_id(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise ValueError("CHANNEL_URL должна быть публичной ссылкой https://t.me/имя_канала")
    path = parsed.path.strip("/")
    if not path or path.startswith("+") or path.startswith("joinchat/"):
        raise ValueError("CHANNEL_URL должна быть публичной ссылкой https://t.me/имя_канала")
    return "@" + path.split("/")[0]


CHANNEL_ID = None
if CHANNEL_URL:
    try:
        CHANNEL_ID = channel_chat_id(CHANNEL_URL)
    except ValueError as exc:
        raise SystemExit(str(exc))


@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            started_at TEXT NOT NULL, last_seen TEXT NOT NULL,
            referrer_id INTEGER, blocked INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS buttons(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('submenu','text','link','photo')),
            content TEXT NOT NULL DEFAULT '',
            photo_file_id TEXT,
            parent_id INTEGER REFERENCES buttons(id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS scheduled_broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, send_at TEXT NOT NULL,
            text TEXT NOT NULL, photo_file_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_by INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            action TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS referrals(
            user_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        # Migrate older buttons table without losing its data.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(buttons)")}
        for name, definition in [
            ("position", "INTEGER NOT NULL DEFAULT 0"),
            ("clicks", "INTEGER NOT NULL DEFAULT 0"),
            ("photo_file_id", "TEXT"),
            ("parent_id", "INTEGER REFERENCES buttons(id) ON DELETE CASCADE"),
        ]:
            if name not in cols:
                try:
                    c.execute(f"ALTER TABLE buttons ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError:
                    pass
        # Old CHECK constraints may still allow only text/link. Rebuild if needed.
        schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='buttons'").fetchone()
        if schema and ("submenu" not in (schema["sql"] or "").lower() or "photo" not in (schema["sql"] or "").lower()):
            oldcols = {r["name"] for r in c.execute("PRAGMA table_info(buttons)")}
            c.execute("""CREATE TABLE buttons_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('submenu','text','link','photo')),
                content TEXT NOT NULL DEFAULT '', photo_file_id TEXT,
                parent_id INTEGER REFERENCES buttons_new(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0)""")
            names = ["id","title","kind","content"]
            expr = ["id","title","kind","content"]
            for col in ("photo_file_id","parent_id","position","clicks"):
                names.append(col); expr.append(col if col in oldcols else ("NULL" if col in ("photo_file_id","parent_id") else "0"))
            c.execute(f"INSERT INTO buttons_new({','.join(names)}) SELECT {','.join(expr)} FROM buttons")
            c.execute("DROP TABLE buttons")
            c.execute("ALTER TABLE buttons_new RENAME TO buttons")
        c.execute("CREATE INDEX IF NOT EXISTS idx_buttons_parent ON buttons(parent_id,position,id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_started ON users(started_at)")


def setting(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def audit(action):
    with db() as c:
        c.execute("INSERT INTO audit_log(admin_id,action,created_at) VALUES(?,?,?)",
                  (ADMIN_ID, action, datetime.now().isoformat(timespec="seconds")))


def is_admin(user_id):
    return user_id == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Фото меню", callback_data="admin:photo"),
         InlineKeyboardButton(text="✏️ Приветствие", callback_data="admin:welcome")],
        [InlineKeyboardButton(text="🔘 Управление кнопками", callback_data="admin:buttons"),
         InlineKeyboardButton(text="👀 Предпросмотр", callback_data="admin:preview")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🗓 Запланированные рассылки", callback_data="admin:scheduled")],
        [InlineKeyboardButton(text="💾 Резервная копия", callback_data="admin:backup"),
         InlineKeyboardButton(text="📜 Журнал", callback_data="admin:audit")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")]
    ])


def buttons_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить кнопку/раздел", callback_data="admin:add")],
        [InlineKeyboardButton(text="✏️ Изменить кнопку", callback_data="admin:edit")],
        [InlineKeyboardButton(text="↕️ Порядок кнопок", callback_data="admin:order")],
        [InlineKeyboardButton(text="🗑 Удалить кнопку", callback_data="admin:delete")],
        [InlineKeyboardButton(text="⬅️ В панель", callback_data="admin:home")]
    ])


def get_children(parent_id=None):
    with db() as c:
        if parent_id is None:
            return c.execute("SELECT * FROM buttons WHERE parent_id IS NULL ORDER BY position,id").fetchall()
        return c.execute("SELECT * FROM buttons WHERE parent_id=? ORDER BY position,id", (parent_id,)).fetchall()


def get_button(button_id):
    with db() as c:
        return c.execute("SELECT * FROM buttons WHERE id=?", (button_id,)).fetchone()


def build_menu(parent_id=None, include_back=False):
    rows = []
    for item in get_children(parent_id):
        if item["kind"] == "link":
            # URL buttons open the destination directly and do not send a bot message.
            rows.append([InlineKeyboardButton(text=item["title"], url=item["content"])])
        else:
            rows.append([InlineKeyboardButton(text=item["title"], callback_data=f"menu:{item['id']}")])
    if parent_id is None:
        rows.append([InlineKeyboardButton(text="✉️ Поддержка", callback_data="support:start"),
                     InlineKeyboardButton(text="🤝 Пригласить", callback_data="referral:show")])
    if include_back:
        parent = get_button(parent_id) if parent_id else None
        grandparent = parent["parent_id"] if parent else None
        target = str(grandparent) if grandparent is not None else "root"
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"menu:back:{target}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def sub_keyboard():
    rows = []
    if CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)])
        rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="sub_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def subscribed(bot, user_id):
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        logging.exception("Не удалось проверить подписку. Проверь права бота в канале.")
        return False


def remember_user(user, referrer_id=None):
    now = datetime.now().isoformat(timespec="seconds")
    with db() as c:
        old = c.execute("SELECT user_id,referrer_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        c.execute("""INSERT INTO users(user_id,username,first_name,started_at,last_seen,referrer_id,blocked)
                     VALUES(?,?,?,?,?,?,0)
                     ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                     first_name=excluded.first_name,last_seen=excluded.last_seen""",
                  (user.id, user.username, user.first_name or "", now, now, referrer_id))
        if not old and referrer_id and referrer_id != user.id:
            c.execute("INSERT OR IGNORE INTO referrals(user_id,referrer_id,created_at) VALUES(?,?,?)",
                      (user.id, referrer_id, now))


async def show_home(target, bot, user_id):
    welcome = setting("welcome_text", "Добро пожаловать! Выберите нужный раздел.")
    photo_id = setting("menu_photo_file_id", "")
    if not await subscribed(bot, user_id):
        text = "Для доступа к меню подпишитесь на канал, затем нажмите «Я подписался»."
        kb = sub_keyboard()
        if isinstance(target, Message):
            await target.answer(text, reply_markup=kb)
        else:
            await target.message.answer(text, reply_markup=kb)
        return
    kb = build_menu()
    if isinstance(target, Message):
        if photo_id:
            await target.answer_photo(photo_id, caption=welcome, reply_markup=kb)
        else:
            await target.answer(welcome, reply_markup=kb)
    else:
        msg = target.message
        try:
            if photo_id and msg.photo:
                await msg.edit_media(InputMediaPhoto(media=photo_id, caption=welcome), reply_markup=kb)
            elif photo_id:
                await msg.delete()
                await bot.send_photo(target.from_user.id, photo_id, caption=welcome, reply_markup=kb)
            elif msg.text:
                await msg.edit_text(welcome, reply_markup=kb)
            else:
                await msg.delete()
                await bot.send_message(target.from_user.id, welcome, reply_markup=kb)
        except Exception:
            try:
                await msg.delete()
            except Exception:
                pass
            if photo_id:
                await bot.send_photo(target.from_user.id, photo_id, caption=welcome, reply_markup=kb)
            else:
                await bot.send_message(target.from_user.id, welcome, reply_markup=kb)


@dp.message(CommandStart())
async def start(message: Message, bot: Bot):
    payload = (message.text or "").split(maxsplit=1)
    referrer = None
    if len(payload) > 1 and payload[1].startswith("ref_"):
        try:
            referrer = int(payload[1][4:])
        except ValueError:
            pass
    remember_user(message.from_user, referrer)
    await show_home(message, bot, message.from_user.id)
    if is_admin(message.from_user.id):
        await message.answer("Админ-панель: /admin")


@dp.message(Command("admin"))
async def admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await state.clear()
    await message.answer("⚙️ **Панель управления**\nВыберите действие:", reply_markup=admin_kb())


@dp.callback_query(F.data == "sub_check")
async def check_sub(callback: CallbackQuery, bot: Bot):
    if await subscribed(bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена!")
        await show_home(callback, bot, callback.from_user.id)
    else:
        await callback.answer("Подписка не найдена. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("menu:back:"))
async def menu_back(callback: CallbackQuery, bot: Bot):
    if not await subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишитесь на канал.", show_alert=True)
        return
    target = callback.data.rsplit(":", 1)[1]
    if target == "root":
        await show_home(callback, bot, callback.from_user.id)
    else:
        parent = get_button(int(target))
        if parent:
            await render_button_menu(callback, parent, bot)
        else:
            await show_home(callback, bot, callback.from_user.id)
    await callback.answer()


async def render_button_menu(callback, item, bot):
    children = get_children(item["id"])
    kb = build_menu(item["id"], True)
    text = (item["content"] if item["kind"] == "submenu" and item["content"] else f"📂 {item['title']}")
    photo = item["photo_file_id"] if item["kind"] == "photo" else None
    try:
        if photo:
            if callback.message.photo:
                await callback.message.edit_media(InputMediaPhoto(media=photo, caption=item["content"] or item["title"]), reply_markup=kb)
            else:
                await callback.message.delete()
                await bot.send_photo(callback.from_user.id, photo, caption=item["content"] or item["title"], reply_markup=kb)
        elif callback.message.text:
            await callback.message.edit_text(text, reply_markup=kb)
        else:
            await callback.message.delete()
            await bot.send_message(callback.from_user.id, text, reply_markup=kb)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        if photo:
            await bot.send_photo(callback.from_user.id, photo, caption=item["content"] or item["title"], reply_markup=kb)
        else:
            await bot.send_message(callback.from_user.id, text, reply_markup=kb)


@dp.callback_query(F.data.startswith("menu:"))
async def user_button(callback: CallbackQuery, bot: Bot):
    if not await subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишитесь на канал.", show_alert=True)
        return
    try:
        item_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        return await callback.answer("Кнопка не найдена.", show_alert=True)
    item = get_button(item_id)
    if not item:
        return await callback.answer("Кнопка уже удалена.", show_alert=True)
    with db() as c:
        c.execute("UPDATE buttons SET clicks=clicks+1 WHERE id=?", (item_id,))
    await callback.answer()
    if item["kind"] in {"submenu", "photo"} or get_children(item_id):
        await render_button_menu(callback, item, bot)
    elif item["kind"] == "text":
        try:
            await callback.message.edit_text(item["content"], reply_markup=build_menu(item["parent_id"], True))
        except Exception:
            await callback.message.answer(item["content"], reply_markup=build_menu(item["parent_id"], True))
    else:
        await callback.message.answer(item["content"], reply_markup=build_menu(item["parent_id"], True))



@dp.callback_query(F.data == "support:start")
async def support_start(cb: CallbackQuery, state: FSMContext):
    if not await subscribed(cb.bot, cb.from_user.id):
        return await cb.answer("Сначала подпишитесь на канал.", show_alert=True)
    await state.set_state(Support.message)
    await cb.message.answer("✉️ Напишите сообщение для поддержки. Для отмены отправьте /cancel.")
    await cb.answer()


@dp.message(Support.message)
async def support_send(msg: Message, state: FSMContext, bot: Bot):
    if not msg.text and not msg.photo and not msg.document:
        return await msg.answer("Отправьте текст, фото или документ.")
    user = msg.from_user
    caption = f"✉️ Обращение в поддержку\\nID: {user.id}\\nИмя: {user.full_name}\\nUsername: @{user.username or 'нет'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"support:reply:{user.id}")]
    ])
    try:
        await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
        if msg.photo:
            await bot.send_photo(ADMIN_ID, msg.photo[-1].file_id, caption=msg.caption or "")
        elif msg.document:
            await bot.send_document(ADMIN_ID, msg.document.file_id, caption=msg.caption or "")
        else:
            await bot.send_message(ADMIN_ID, msg.text)
    except Exception:
        logging.exception("Не удалось передать обращение администратору")
        return await msg.answer("Не удалось отправить обращение. Попробуйте позже.")
    await state.clear()
    await msg.answer("✅ Сообщение передано поддержке.")


@dp.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа.", show_alert=True)
    try:
        uid = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("Некорректный пользователь.", show_alert=True)
    await state.set_state(SupportReply.message)
    await state.update_data(reply_user_id=uid)
    await cb.message.answer(f"Введите ответ для пользователя {uid}.")
    await cb.answer()


@dp.message(SupportReply.message)
async def support_reply_send(msg: Message, state: FSMContext, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    data = await state.get_data()
    if not msg.text and not msg.photo and not msg.document:
        return await msg.answer("Отправьте текст, фото или документ.")
    try:
        uid = data["reply_user_id"]
        if msg.photo:
            await bot.send_photo(uid, msg.photo[-1].file_id, caption=msg.caption or "")
        elif msg.document:
            await bot.send_document(uid, msg.document.file_id, caption=msg.caption or "")
        else:
            await bot.send_message(uid, f"💬 Ответ поддержки:\\n{msg.text}")
        await msg.answer("✅ Ответ отправлен.", reply_markup=admin_kb())
    except Exception:
        await msg.answer("Не удалось доставить ответ: пользователь мог заблокировать бота.")
    await state.clear()


@dp.callback_query(F.data == "referral:show")
async def referral_show(cb: CallbackQuery, bot: Bot):
    if not await subscribed(bot, cb.from_user.id):
        return await cb.answer("Сначала подпишитесь на канал.", show_alert=True)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM referrals WHERE referrer_id=?", (cb.from_user.id,)).fetchone()["n"]
    await cb.message.answer(f"🤝 Ваша ссылка для приглашений:\\n{link}\\n\\nРегистраций по ссылке: {count}")
    await cb.answer()


@dp.callback_query(F.data == "admin:home")
async def admin_home(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    await cb.message.answer("⚙️ Панель управления:", reply_markup=admin_kb()); await cb.answer()


@dp.callback_query(F.data == "admin:buttons")
async def admin_buttons(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    await cb.message.answer("🔘 Управление меню:", reply_markup=buttons_kb()); await cb.answer()


@dp.callback_query(F.data == "admin:welcome")
async def welcome_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    await state.set_state(EditSetting.welcome)
    await cb.message.answer("Отправьте новый текст приветствия (до 900 символов).")
    await cb.answer()


@dp.message(EditSetting.welcome)
async def welcome_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    text = (msg.text or "").strip()
    if not text or len(text) > 900:
        return await msg.answer("Нужен текст до 900 символов.")
    set_setting("welcome_text", text); audit("Изменено приветствие"); await state.clear()
    await msg.answer("✅ Приветствие сохранено.", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin:photo")
async def photo_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    await state.set_state(EditSetting.menu_photo)
    await cb.message.answer("Отправьте фотографию для главного меню. Чтобы убрать фото, отправьте /remove_photo.")
    await cb.answer()


@dp.message(EditSetting.menu_photo)
async def photo_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    if msg.text == "/remove_photo":
        set_setting("menu_photo_file_id", "")
    elif msg.photo:
        set_setting("menu_photo_file_id", msg.photo[-1].file_id)
    else:
        return await msg.answer("Отправьте фотографию или /remove_photo.")
    audit("Изменено фото главного меню"); await state.clear()
    await msg.answer("✅ Настройка фото сохранена.", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin:add")
async def add_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    await state.clear(); await state.set_state(AddButton.title)
    await cb.message.answer("Введите название кнопки (до 55 символов).")
    await cb.answer()


@dp.message(AddButton.title)
async def add_title(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    title = (msg.text or "").strip()
    if not title or len(title) > 55: return await msg.answer("Название должно быть до 55 символов.")
    await state.update_data(title=title); await state.set_state(AddButton.parent)
    rows = [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="parent:root")]]
    with db() as c:
        parents = c.execute("SELECT id,title FROM buttons WHERE kind IN ('submenu','photo') ORDER BY position,id").fetchall()
    rows += [[InlineKeyboardButton(text=("📂 "+r["title"])[:60], callback_data=f"parent:{r['id']}")] for r in parents]
    await msg.answer("Куда добавить кнопку?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(AddButton.parent, F.data.startswith("parent:"))
async def add_parent(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    raw = cb.data.split(":",1)[1]; parent = None if raw == "root" else int(raw)
    if parent is not None and not get_button(parent): return await cb.answer("Раздел не найден.", show_alert=True)
    await state.update_data(parent_id=parent); await state.set_state(AddButton.kind)
    await cb.message.answer("Выберите тип:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Раздел", callback_data="kind:submenu")],
        [InlineKeyboardButton(text="💬 Текст", callback_data="kind:text")],
        [InlineKeyboardButton(text="🔗 Прямая URL-ссылка", callback_data="kind:link")],
        [InlineKeyboardButton(text="🖼 Фото с подписью", callback_data="kind:photo")]
    ])); await cb.answer()


@dp.callback_query(AddButton.kind, F.data.startswith("kind:"))
async def add_kind(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    kind = cb.data.split(":",1)[1]
    if kind not in {"submenu","text","link","photo"}: return await cb.answer("Неизвестный тип.")
    await state.update_data(kind=kind); await state.set_state(AddButton.content)
    prompts = {"submenu":"Введите описание раздела или - без описания.",
               "text":"Отправьте текст ответа.",
               "link":"Отправьте полный URL (https://... или http://...). Кнопка будет открывать его напрямую.",
               "photo":"Отправьте фото; подпись необязательна."}
    await cb.message.answer(prompts[kind]); await cb.answer()


def valid_url(text):
    try:
        u = urlparse(text.strip())
        return u.scheme in {"http","https"} and bool(u.netloc) and " " not in text
    except Exception:
        return False


@dp.message(AddButton.content)
async def add_content(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data = await state.get_data(); kind = data["kind"]; photo = None
    if kind == "photo":
        if not msg.photo: return await msg.answer("Отправьте именно фотографию.")
        photo = msg.photo[-1].file_id; content = (msg.caption or "").strip()
    else:
        content = (msg.text or "").strip()
        if kind == "submenu" and content == "-": content = ""
        if kind == "link" and not valid_url(content): return await msg.answer("Некорректный URL. Используйте https://... или http://...")
        if kind in {"text","link"} and not content: return await msg.answer("Содержимое не должно быть пустым.")
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),-1)+1 AS p FROM buttons WHERE parent_id IS ?", (data["parent_id"],)).fetchone()["p"]
        c.execute("INSERT INTO buttons(title,kind,content,photo_file_id,parent_id,position) VALUES(?,?,?,?,?,?)",
                  (data["title"],kind,content,photo,data["parent_id"],pos))
    audit(f"Добавлена кнопка: {data['title']} ({kind})"); await state.clear()
    await msg.answer("✅ Кнопка добавлена.", reply_markup=buttons_kb())


@dp.callback_query(F.data == "admin:delete")
async def delete_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    with db() as c: items = c.execute("SELECT id,title FROM buttons ORDER BY position,id").fetchall()
    if not items: await cb.message.answer("Кнопок нет.")
    else:
        rows = [[InlineKeyboardButton(text=f"🗑 {r['title']}"[:60], callback_data=f"delete:{r['id']}")] for r in items]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:buttons")])
        await cb.message.answer("Удалить кнопку? Все вложенные кнопки раздела также удалятся.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("delete:"))
async def delete_button(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    try: bid = int(cb.data.split(":")[1])
    except ValueError: return await cb.answer("Неверный ID.", show_alert=True)
    item = get_button(bid)
    if not item: return await cb.answer("Кнопка уже удалена.", show_alert=True)
    with db() as c: c.execute("DELETE FROM buttons WHERE id=?", (bid,))
    audit(f"Удалена кнопка: {item['title']}")
    await cb.answer("Удалено"); await cb.message.answer("✅ Кнопка удалена.", reply_markup=buttons_kb())


@dp.callback_query(F.data == "admin:edit")
async def edit_list(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    with db() as c: items = c.execute("SELECT id,title FROM buttons ORDER BY position,id").fetchall()
    if not items: await cb.message.answer("Кнопок пока нет.")
    else:
        await state.set_state(EditButton.choose)
        rows = [[InlineKeyboardButton(text=r["title"][:55], callback_data=f"edit:{r['id']}")] for r in items]
        await cb.message.answer("Выберите кнопку для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(EditButton.choose, F.data.startswith("edit:"))
async def edit_choose(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    bid = int(cb.data.split(":")[1]); item = get_button(bid)
    if not item: return await cb.answer("Кнопка не найдена.", show_alert=True)
    await state.update_data(button_id=bid)
    await cb.message.answer(f"Выбрана: {item['title']}\nЧто изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Название", callback_data="editfield:title")],
            [InlineKeyboardButton(text="Содержимое / URL", callback_data="editfield:content")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:buttons")]
        ])); await cb.answer()


@dp.callback_query(F.data.startswith("editfield:"))
async def edit_field(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    field = cb.data.split(":")[1]
    if field not in {"title","content"}: return await cb.answer()
    await state.update_data(field=field); await state.set_state(EditButton.title if field=="title" else EditButton.content)
    await cb.message.answer("Введите новое название:" if field=="title" else "Введите новый текст или URL. Для фото/раздела меняется подпись/описание.")
    await cb.answer()


@dp.message(EditButton.title)
async def edit_title_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data=await state.get_data(); title=(msg.text or "").strip()
    if not title or len(title)>55: return await msg.answer("Название должно быть до 55 символов.")
    with db() as c: c.execute("UPDATE buttons SET title=? WHERE id=?",(title,data["button_id"]))
    audit("Изменено название кнопки"); await state.clear(); await msg.answer("✅ Сохранено.",reply_markup=buttons_kb())


@dp.message(EditButton.content)
async def edit_content_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data=await state.get_data(); item=get_button(data["button_id"]); content=(msg.text or "").strip()
    if not item: await state.clear(); return await msg.answer("Кнопка не найдена.")
    if item["kind"]=="link" and not valid_url(content): return await msg.answer("Введите корректный URL http:// или https://.")
    if not content: return await msg.answer("Содержимое не должно быть пустым.")
    with db() as c: c.execute("UPDATE buttons SET content=? WHERE id=?",(content,data["button_id"]))
    audit("Изменено содержимое кнопки"); await state.clear(); await msg.answer("✅ Сохранено.",reply_markup=buttons_kb())


@dp.callback_query(F.data == "admin:order")
async def order_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    with db() as c: items=c.execute("SELECT id,title,parent_id FROM buttons ORDER BY COALESCE(parent_id,0),position,id").fetchall()
    if not items: await cb.message.answer("Кнопок нет.")
    else:
        rows=[]
        for r in items:
            rows.append([InlineKeyboardButton(text=f"⬆️ {r['title']}"[:55],callback_data=f"move:{r['id']}:-1"),
                         InlineKeyboardButton(text="⬇️",callback_data=f"move:{r['id']}:1")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="admin:buttons")])
        await cb.message.answer("Переместите кнопку вверх/вниз в пределах её раздела:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("move:"))
async def move_button(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    try: _,raw,direction=cb.data.split(":"); bid=int(raw); delta=int(direction)
    except ValueError: return await cb.answer("Некорректная команда.",show_alert=True)
    item=get_button(bid)
    if not item: return await cb.answer("Не найдено.",show_alert=True)
    with db() as c:
        siblings=c.execute("SELECT id,position FROM buttons WHERE parent_id IS ? ORDER BY position,id",(item["parent_id"],)).fetchall()
        ids=[x["id"] for x in siblings]; idx=ids.index(bid); newidx=max(0,min(len(ids)-1,idx+delta))
        ids[idx],ids[newidx]=ids[newidx],ids[idx]
        for pos, ident in enumerate(ids): c.execute("UPDATE buttons SET position=? WHERE id=?",(pos,ident))
    await cb.answer("Порядок обновлён")


@dp.callback_query(F.data == "admin:preview")
async def preview(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    text=setting("welcome_text","Добро пожаловать! Выберите нужный раздел.")
    photo=setting("menu_photo_file_id","")
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=build_menu())
    else: await cb.message.answer(text,reply_markup=build_menu())
    await cb.answer()


@dp.callback_query(F.data == "admin:stats")
async def stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.", show_alert=True)
    with db() as c:
        total=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        today=c.execute("SELECT COUNT(*) n FROM users WHERE substr(started_at,1,10)=?",(datetime.now().date().isoformat(),)).fetchone()["n"]
        btns=c.execute("SELECT title,clicks FROM buttons ORDER BY clicks DESC LIMIT 10").fetchall()
        refs=c.execute("SELECT COUNT(*) n FROM referrals").fetchone()["n"]
        scheduled=c.execute("SELECT COUNT(*) n FROM scheduled_broadcasts WHERE status='pending'").fetchone()["n"]
    detail="\n".join(f"• {r['title']}: {r['clicks']} нажатий" for r in btns) or "Пока нет кнопок."
    await cb.message.answer(f"📊 Статистика\nПользователей: {total}\nНовые сегодня: {today}\nПриглашено по реферальным ссылкам: {refs}\nЗапланировано рассылок: {scheduled}\n\nПопулярность кнопок\n{detail}\n\nПримечание: прямые URL-кнопки открываются без сообщения от бота; Telegram не передаёт боту событие нажатия таких кнопок.")
    await cb.answer()


async def send_broadcast(bot, text, photo_id=None):
    with db() as c: users=c.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    sent=failed=0
    for row in users:
        uid=row["user_id"]
        try:
            if photo_id: await bot.send_photo(uid,photo_id,caption=text or None)
            else: await bot.send_message(uid,text)
            sent+=1
        except Exception as exc:
            failed+=1
            if "blocked" in str(exc).lower() or "chat not found" in str(exc).lower():
                with db() as c: c.execute("UPDATE users SET blocked=1 WHERE user_id=?",(uid,))
        await asyncio.sleep(0.05)
    return sent,failed


@dp.callback_query(F.data == "admin:broadcast")
async def broadcast_start(cb: CallbackQuery,state:FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    await state.set_state(Broadcast.content)
    await cb.message.answer("Отправьте текст рассылки или фото с подписью. Для отмены /cancel.")
    await cb.answer()


@dp.message(Command("cancel"))
async def cancel(msg:Message,state:FSMContext):
    await state.clear(); await msg.answer("Действие отменено.")


@dp.message(Broadcast.content)
async def broadcast_content(msg:Message,state:FSMContext):
    if not is_admin(msg.from_user.id): return
    text=msg.text or msg.caption or ""
    photo=msg.photo[-1].file_id if msg.photo else None
    if not text and not photo: return await msg.answer("Отправьте текст или фото с подписью.")
    await state.update_data(text=text,photo=photo); await state.set_state(Broadcast.confirm)
    await msg.answer("Предпросмотр рассылки:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить всем",callback_data="broadcast:send")],
        [InlineKeyboardButton(text="❌ Отмена",callback_data="broadcast:cancel")]
    ]))
    if photo: await msg.answer_photo(photo,caption=text or None)
    else: await msg.answer(text)


@dp.callback_query(Broadcast.confirm,F.data=="broadcast:send")
async def broadcast_send(cb:CallbackQuery,state:FSMContext,bot:Bot):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    data=await state.get_data(); await state.clear(); await cb.answer("Рассылка запущена.")
    await cb.message.answer("Рассылка выполняется. По завершении придёт отчёт.")
    sent,failed=await send_broadcast(bot,data.get("text",""),data.get("photo"))
    audit(f"Рассылка: доставлено {sent}, ошибок {failed}")
    await bot.send_message(ADMIN_ID,f"📢 Рассылка завершена.\nДоставлено: {sent}\nОшибок: {failed}")


@dp.callback_query(Broadcast.confirm,F.data=="broadcast:cancel")
async def broadcast_cancel(cb:CallbackQuery,state:FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    await state.clear(); await cb.message.answer("Рассылка отменена.",reply_markup=admin_kb()); await cb.answer()


@dp.callback_query(F.data=="admin:scheduled")
async def scheduled_menu(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    with db() as c: rows=c.execute("SELECT * FROM scheduled_broadcasts WHERE status='pending' ORDER BY send_at LIMIT 20").fetchall()
    if not rows: await cb.message.answer("Нет запланированных рассылок.")
    else:
        for r in rows:
            await cb.message.answer(f"#{r['id']} · {r['send_at']}\n{r['text'][:250]}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Отменить",callback_data=f"schedule:cancel:{r['id']}")]
            ]))
    await cb.message.answer("Чтобы создать рассылку, нажмите «Запланировать» ниже.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 Запланировать рассылку",callback_data="schedule:new")],
        [InlineKeyboardButton(text="⬅️ В панель",callback_data="admin:home")]
    ])); await cb.answer()


class ScheduleState(StatesGroup):
    when = State()
    content = State()


@dp.callback_query(F.data=="schedule:new")
async def schedule_new(cb:CallbackQuery,state:FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    await state.set_state(ScheduleState.when)
    await cb.message.answer("Введите дату и время отправки в формате YYYY-MM-DD HH:MM (время сервера).")
    await cb.answer()


@dp.message(ScheduleState.when)
async def schedule_when(msg:Message,state:FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        when=datetime.strptime((msg.text or "").strip(),"%Y-%m-%d %H:%M")
        if when <= datetime.now(): raise ValueError()
    except ValueError:
        return await msg.answer("Неверная дата или время в прошлом. Формат: YYYY-MM-DD HH:MM.")
    await state.update_data(send_at=when.isoformat(timespec="minutes")); await state.set_state(ScheduleState.content)
    await msg.answer("Теперь отправьте текст или фото с подписью для запланированной рассылки.")


@dp.message(ScheduleState.content)
async def schedule_content(msg:Message,state:FSMContext):
    if not is_admin(msg.from_user.id): return
    text=msg.text or msg.caption or ""; photo=msg.photo[-1].file_id if msg.photo else None
    if not text and not photo: return await msg.answer("Отправьте текст или фото с подписью.")
    data=await state.get_data()
    with db() as c:
        c.execute("INSERT INTO scheduled_broadcasts(send_at,text,photo_file_id,created_by) VALUES(?,?,?,?)",
                  (data["send_at"],text,photo,ADMIN_ID))
    audit("Создана запланированная рассылка"); await state.clear()
    await msg.answer(f"✅ Рассылка запланирована на {data['send_at']} (время сервера).",reply_markup=admin_kb())


@dp.callback_query(F.data.startswith("schedule:cancel:"))
async def schedule_cancel(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    try: sid=int(cb.data.rsplit(":",1)[1])
    except ValueError: return await cb.answer("Неверный ID.",show_alert=True)
    with db() as c: c.execute("UPDATE scheduled_broadcasts SET status='cancelled' WHERE id=? AND status='pending'",(sid,))
    audit(f"Отменена запланированная рассылка #{sid}"); await cb.answer("Отменено"); await cb.message.answer("Готово.")


@dp.callback_query(F.data == "admin:backup")
async def backup(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    try:
        with db() as c:
            dest=sqlite3.connect("bot_data_backup.db")
            c.backup(dest); dest.close()
        await cb.message.answer_document(FSInputFile("bot_data_backup.db"),caption="Резервная копия базы данных. Храните файл в безопасном месте.")
        audit("Создана резервная копия")
    except Exception:
        logging.exception("Backup failed"); await cb.message.answer("Не удалось создать резервную копию.")
    await cb.answer()


@dp.callback_query(F.data == "admin:audit")
async def audit_view(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    with db() as c: rows=c.execute("SELECT action,created_at FROM audit_log ORDER BY id DESC LIMIT 15").fetchall()
    text="📜 Последние действия\n"+"\n".join(f"• {r['created_at']} — {r['action']}" for r in rows)
    await cb.message.answer(text if rows else "Журнал пока пуст."); await cb.answer()


@dp.callback_query(F.data == "admin:close")
async def admin_close(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    await cb.message.answer("Панель закрыта. Открыть снова: /admin"); await cb.answer()


async def scheduled_worker(bot:Bot):
    while True:
        try:
            now=datetime.now().isoformat(timespec="minutes")
            with db() as c:
                rows=c.execute("SELECT * FROM scheduled_broadcasts WHERE status='pending' AND send_at<=? ORDER BY send_at LIMIT 5",(now,)).fetchall()
                for row in rows:
                    c.execute("UPDATE scheduled_broadcasts SET status='sending' WHERE id=?",(row["id"],))
            for row in rows:
                sent,failed=await send_broadcast(bot,row["text"],row["photo_file_id"])
                with db() as c: c.execute("UPDATE scheduled_broadcasts SET status='sent' WHERE id=?",(row["id"],))
                await bot.send_message(ADMIN_ID,f"🗓 Запланированная рассылка #{row['id']} завершена.\nДоставлено: {sent}\nОшибок: {failed}")
        except Exception:
            logging.exception("Ошибка планировщика рассылок")
        await asyncio.sleep(20)


@dp.callback_query(F.data == "admin:help")
async def admin_help(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа.",show_alert=True)
    await cb.message.answer("Справка: /admin — панель, /cancel — отмена текущего действия. Ссылки добавляются как прямые URL-кнопки."); await cb.answer()


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Запустите НАСТРОИТЬ_И_ЗАПУСТИТЬ.bat и укажите токен.")
    if not ADMIN_ID:
        raise SystemExit("Не задан ADMIN_ID. Укажите свой числовой Telegram ID.")
    init_db()
    bot=Bot(BOT_TOKEN)
    worker=None
    try:
        me=await bot.get_me()
        logging.info("Бот запущен: @%s",me.username)
        worker=asyncio.create_task(scheduled_worker(bot))
        await dp.start_polling(bot)
    finally:
        if worker:
            worker.cancel()
            try: await worker
            except asyncio.CancelledError: pass
        await bot.session.close()


if __name__=="__main__":
    asyncio.run(main())
