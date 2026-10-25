# Настройки: рекомендуется задать их через переменные окружения на хостинге.
import asyncio
import logging
import os
import re
import sqlite3
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/ИМЯ_ПУБЛИЧНОГО_КАНАЛА")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
except ValueError:
    ADMIN_ID = 123456789

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
DB_FILE = os.getenv("DB_FILE", "bot_data.db")


class AddButton(StatesGroup):
    title = State()
    parent = State()
    kind = State()
    content = State()


def channel_chat_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if (parsed.netloc or "").lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise ValueError("CHANNEL_URL должна выглядеть как https://t.me/имя_канала")
    path = parsed.path.strip("/")
    if not path or path.startswith("+") or path.startswith("joinchat/"):
        raise ValueError("Используй публичную ссылку канала вида https://t.me/имя_канала")
    username = path.split("/")[0]
    if username.lower() in {"c", "share", "addstickers", "proxy"}:
        raise ValueError("Укажи публичную ссылку канала вида https://t.me/имя_канала")
    return "@" + username


try:
    CHANNEL_ID = channel_chat_id(CHANNEL_URL)
except ValueError as e:
    raise SystemExit(f"Ошибка настройки: {e}")


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='buttons'"
        ).fetchone()
        if not existing:
            conn.execute("""CREATE TABLE buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('submenu', 'text', 'link', 'photo')),
                content TEXT NOT NULL DEFAULT '',
                photo_file_id TEXT,
                parent_id INTEGER REFERENCES buttons(id) ON DELETE CASCADE
            )""")
            return

        # Старый вариант базы ограничивал kind значениями text/link.
        # Пересоздаём таблицу, чтобы новые типы кнопок работали и старые данные сохранились.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(buttons)")}
        schema_sql = (existing["sql"] or "").lower()
        if "submenu" not in schema_sql or "photo_file_id" not in cols or "parent_id" not in cols:
            old_cols = cols
            conn.execute("""CREATE TABLE buttons_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('submenu', 'text', 'link', 'photo')),
                content TEXT NOT NULL DEFAULT '',
                photo_file_id TEXT,
                parent_id INTEGER REFERENCES buttons_new(id) ON DELETE CASCADE
            )""")
            photo_expr = "photo_file_id" if "photo_file_id" in old_cols else "NULL"
            parent_expr = "parent_id" if "parent_id" in old_cols else "NULL"
            conn.execute(f"""INSERT INTO buttons_new(id,title,kind,content,photo_file_id,parent_id)
                SELECT id,title,kind,content,{photo_expr},{parent_expr} FROM buttons""")
            conn.execute("DROP TABLE buttons")
            conn.execute("ALTER TABLE buttons_new RENAME TO buttons")


def sub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="sub_check")]
    ])


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить кнопку/раздел", callback_data="admin:add")],
        [InlineKeyboardButton(text="🗑 Удалить кнопку", callback_data="admin:delete")],
        [InlineKeyboardButton(text="👀 Посмотреть меню", callback_data="admin:preview")],
        [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="admin:close")]
    ])


def get_children(parent_id=None):
    with db() as conn:
        if parent_id is None:
            return conn.execute(
                "SELECT id,title,kind,content,photo_file_id,parent_id FROM buttons WHERE parent_id IS NULL ORDER BY id"
            ).fetchall()
        return conn.execute(
            "SELECT id,title,kind,content,photo_file_id,parent_id FROM buttons WHERE parent_id=? ORDER BY id",
            (parent_id,)
        ).fetchall()


def get_button(button_id):
    with db() as conn:
        return conn.execute(
            "SELECT id,title,kind,content,photo_file_id,parent_id FROM buttons WHERE id=?",
            (button_id,)
        ).fetchone()


def menu_keyboard(parent_id=None, include_back=False):
    items = get_children(parent_id)
    rows = []
    for item in items:
        # Цвет и изображение внутри самой кнопки Telegram Bot API не поддерживает.
        rows.append([InlineKeyboardButton(text=item["title"], callback_data=f"menu:{item['id']}")])
    if include_back:
        parent = get_button(parent_id) if parent_id else None
        grandparent = parent["parent_id"] if parent else None
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"menu:back:{grandparent if grandparent is not None else 'root'}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        logging.exception("Ошибка проверки подписки. Проверь ссылку канала и права бота.")
        return False


async def show_menu(message: Message, bot: Bot):
    if not await subscribed(bot, message.from_user.id):
        await message.answer(
            "Привет! Для доступа подпишись на канал, затем нажми «Я подписался».",
            reply_markup=sub_keyboard()
        )
        return
    await message.answer("Подписка подтверждена! Выбери раздел:", reply_markup=menu_keyboard())
    if message.from_user.id == ADMIN_ID:
        await message.answer("Для управления кнопками отправь /admin")


@dp.message(CommandStart())
async def start(message: Message, bot: Bot):
    await show_menu(message, bot)


@dp.callback_query(F.data == "sub_check")
async def check_sub(callback: CallbackQuery, bot: Bot):
    if await subscribed(bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена!")
        await callback.message.answer("Отлично! Вот меню:", reply_markup=menu_keyboard())
        if callback.from_user.id == ADMIN_ID:
            await callback.message.answer("Для управления кнопками отправь /admin")
    else:
        await callback.answer("Подписка не найдена. Подпишись и попробуй ещё раз.", show_alert=True)


@dp.message(Command("admin"))
async def admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    await state.clear()
    await message.answer("⚙️ Панель управления:", reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith("menu:back:"))
async def menu_back(callback: CallbackQuery, bot: Bot):
    if not await subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        return
    target = callback.data.rsplit(":", 1)[1]
    if target == "root":
        await callback.message.answer("Главное меню:", reply_markup=menu_keyboard())
    else:
        parent = get_button(int(target))
        if parent:
            await callback.message.answer(f"Раздел: {parent['title']}", reply_markup=menu_keyboard(parent["id"], True))
        else:
            await callback.message.answer("Главное меню:", reply_markup=menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("menu:"))
async def user_button(callback: CallbackQuery, bot: Bot):
    if not await subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        await callback.message.answer("Подпишись, чтобы продолжить:", reply_markup=sub_keyboard())
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Кнопка не найдена.", show_alert=True)
        return
    item = get_button(button_id)
    if not item:
        await callback.answer("Кнопка уже удалена.", show_alert=True)
        return
    children = get_children(button_id)
    child_keyboard = menu_keyboard(button_id, True) if children else None
    await callback.answer()
    if item["kind"] == "link":
        # Для старых ссылочных кнопок и ссылок, добавленных в новой версии.
        await callback.message.answer(f"{item['title']}\n{item['content']}", reply_markup=child_keyboard)
    elif item["kind"] == "photo":
        if item["photo_file_id"]:
            await callback.message.answer_photo(
                item["photo_file_id"],
                caption=item["content"] or item["title"],
                reply_markup=child_keyboard
            )
        else:
            await callback.message.answer("У этой кнопки пока нет фотографии.", reply_markup=child_keyboard)
    elif item["kind"] == "submenu":
        await callback.message.answer(f"📂 {item['title']}", reply_markup=menu_keyboard(button_id, True))
    else:
        await callback.message.answer(item["content"], reply_markup=child_keyboard)


@dp.callback_query(F.data == "admin:add")
async def add_button_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddButton.title)
    await callback.message.answer("Напиши название кнопки (до 60 символов).")
    await callback.answer()


@dp.message(AddButton.title)
async def add_button_title(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    title = message.text.strip() if message.text else ""
    if not title or len(title) > 60:
        await message.answer("Название должно быть текстом до 60 символов. Попробуй снова.")
        return
    await state.update_data(title=title)
    await state.set_state(AddButton.parent)
    rows = [[InlineKeyboardButton(text="🏠 В главное меню", callback_data="parent:root")]]
    with db() as conn:
        parents = conn.execute(
            "SELECT id,title,kind FROM buttons WHERE kind IN ('submenu','photo','text') ORDER BY id"
        ).fetchall()
    for item in parents:
        rows.append([InlineKeyboardButton(text=f"↳ {item['title']}"[:60], callback_data=f"parent:{item['id']}")])
    await message.answer(
        "Куда добавить кнопку? Выбери главное меню или один из существующих разделов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(AddButton.parent, F.data.startswith("parent:"))
async def add_button_parent(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    raw = callback.data.split(":", 1)[1]
    parent_id = None if raw == "root" else int(raw)
    if parent_id is not None and not get_button(parent_id):
        await callback.answer("Раздел не найден.", show_alert=True)
        return
    await state.update_data(parent_id=parent_id)
    await state.set_state(AddButton.kind)
    await callback.message.answer("Выбери тип кнопки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Раздел с вложенными кнопками", callback_data="kind:submenu")],
        [InlineKeyboardButton(text="💬 Текстовый ответ", callback_data="kind:text")],
        [InlineKeyboardButton(text="🔗 Ссылка", callback_data="kind:link")],
        [InlineKeyboardButton(text="🖼 Фото с подписью и вложенными кнопками", callback_data="kind:photo")]
    ]))
    await callback.answer()


@dp.callback_query(AddButton.kind, F.data.in_({"kind:submenu", "kind:text", "kind:link", "kind:photo"}))
async def add_button_kind(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = callback.data.split(":")[1]
    await state.update_data(kind=kind)
    await state.set_state(AddButton.content)
    prompts = {
        "submenu": "Напиши короткое описание раздела (или отправь тире - если описание не нужно).",
        "text": "Отправь текст, который будет показан при нажатии.",
        "link": "Отправь полную ссылку, начинающуюся с https:// или http://.",
        "photo": "Отправь фотографию одним сообщением. По желанию добавь подпись к фото."
    }
    await callback.message.answer(prompts[kind])
    await callback.answer()


@dp.message(AddButton.content)
async def add_button_content(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    kind = data["kind"]
    photo_file_id = None
    if kind == "photo":
        if not message.photo:
            await message.answer("Пожалуйста, отправь именно фотографию одним сообщением, можно с подписью.")
            return
        photo_file_id = message.photo[-1].file_id
        content = (message.caption or "").strip()
    else:
        content = message.text.strip() if message.text else ""
        if kind == "submenu" and content == "-":
            content = ""
        if kind in ("text", "link") and not content:
            await message.answer("Отправь текст или ссылку.")
            return
        if kind == "link" and not re.match(r"^https?://\S+$", content):
            await message.answer("Ссылка должна начинаться с https:// или http://. Попробуй снова.")
            return
    with db() as conn:
        conn.execute(
            "INSERT INTO buttons(title,kind,content,photo_file_id,parent_id) VALUES(?,?,?,?,?)",
            (data["title"], kind, content, photo_file_id, data["parent_id"])
        )
    await state.clear()
    await message.answer("✅ Кнопка добавлена. Если это раздел или фото, можно добавлять в него вложенные кнопки.")
    await message.answer("Панель управления:", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin:delete")
async def delete_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    with db() as conn:
        items = conn.execute("SELECT id,title FROM buttons ORDER BY id").fetchall()
    if not items:
        await callback.message.answer("Кнопок пока нет.")
    else:
        rows = [[InlineKeyboardButton(text=f"🗑 {x['title']}"[:60], callback_data=f"delete:{x['id']}")] for x in items]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")])
        await callback.message.answer("Какую кнопку удалить? Вложенные кнопки внутри раздела также будут удалены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("delete:"))
async def delete_button(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный номер.", show_alert=True)
        return
    with db() as conn:
        # SQLite cascade работает благодаря PRAGMA foreign_keys в db().
        conn.execute("DELETE FROM buttons WHERE id=?", (button_id,))
    await callback.answer("Удалено")
    await callback.message.answer("✅ Кнопка удалена.", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin:preview")
async def preview(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("Так выглядит главное меню:", reply_markup=menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("⚙️ Панель управления:", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin:close")
async def admin_close(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("Панель закрыта. Открыть снова: /admin")
    await callback.answer()


async def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER" or not BOT_TOKEN.strip():
        raise SystemExit("Задай BOT_TOKEN в переменных окружения или в начале main.py.")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID == 123456789:
        raise SystemExit("Задай ADMIN_ID своим числовым Telegram ID.")
    init_db()
    bot = Bot(BOT_TOKEN)
    try:
        me = await bot.get_me()
        logging.info("Бот запущен: @%s; канал: %s", me.username, CHANNEL_ID)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
