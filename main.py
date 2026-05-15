import asyncio
import logging
import sys
import os
from datetime import datetime

import asyncpg
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError

# =========================
# CONFIG
# =========================
API_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

logger.info(f"DB = {DATABASE_URL}")

if not API_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found in Secrets")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL not found in Secrets")

ADMINS = {6814524171, 7764122495, 8010864043, 8246405204}

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# DB POOL
# =========================
pool: asyncpg.Pool = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                nickname TEXT,
                balance FLOAT DEFAULT 0,
                bank FLOAT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                username TEXT PRIMARY KEY,
                user_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                type TEXT NOT NULL,
                from_user BIGINT,
                to_user BIGINT,
                amount FLOAT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

    logger.info("✅ Database initialized")


async def log_transaction(conn, type_: str, from_user, to_user, amount: float):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await conn.execute(
        "INSERT INTO transactions (type, from_user, to_user, amount, created_at) VALUES ($1, $2, $3, $4, $5)",
        type_, from_user, to_user, amount, now
    )
    logger.info(f"💾 [{type_}] from={from_user} to={to_user} amount={amount:.2f}$ at={now}")


# =========================
# UTILS
# =========================
async def ensure_user(uid: int, username: str = None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid
        )
        if username:
            await conn.execute(
                "INSERT INTO usernames (username, user_id) VALUES ($1, $2) ON CONFLICT (username) DO UPDATE SET user_id = $2",
                username.lower(), uid
            )


async def save_user(message: Message):
    if not message or not message.from_user:
        return
    await ensure_user(message.from_user.id, message.from_user.username)


async def get_user_id(identifier: str):
    if identifier.isdigit():
        return int(identifier)

    clean = identifier.lower().replace("@", "")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM usernames WHERE username = $1", clean
        )

    return row["user_id"] if row else None


# =========================
# BOT
# =========================
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    await save_user(message)
    logger.info(f"👋 /start — user={message.from_user.id}")
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "🤖 Бот готов к работе.\n"
        "📖 Используй /help чтобы увидеть все команды."
    )


@dp.message(Command("profile"))
async def profile(message: Message):
    await save_user(message)
    uid = message.from_user.id
    logger.info(f"👤 /profile — user={uid}")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT nickname, balance, bank FROM users WHERE user_id = $1", uid
        )

    nick = row["nickname"] if row and row["nickname"] else "No name"
    bal = row["balance"] if row else 0
    bank = row["bank"] if row else 0

    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"📛 Никнейм: <b>{nick}</b>\n"
        f"💰 Баланс: <b>{bal:.2f}$</b>\n"
        f"🏦 Банк: <b>{bank:.2f}$</b>"
    )


@dp.message(Command("nick"))
async def nick(message: Message):
    await save_user(message)
    uid = message.from_user.id

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /nick <b>имя</b>")

    new_nick = parts[1].strip()

    if not new_nick:
        return await message.answer("⚠️ Никнейм не может быть пустым")
    if len(new_nick) > 32:
        return await message.answer("⚠️ Никнейм слишком длинный (макс. 32 символа)")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET nickname = $1 WHERE user_id = $2", new_nick, uid
        )

    logger.info(f"✏️ /nick — user={uid} new_nick={new_nick}")
    await message.answer(f"✅ Никнейм обновлён: <b>{new_nick}</b>")


@dp.message(Command("add"))
async def add(message: Message):
    await save_user(message)

    if message.from_user.id not in ADMINS:
        logger.warning(f"🚫 /add — unauthorized user={message.from_user.id}")
        return

    parts = message.text.split()
    reply = message.reply_to_message

    if reply and reply.from_user:
        if len(parts) < 2:
            return await message.answer("⚠️ Использование (реплай): /add 100")
        target = reply.from_user.id
        amount_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.answer("⚠️ Использование: /add @user 100\nИли ответь на сообщение игрока: /add 100")
        target = await get_user_id(parts[1])
        if target is None:
            return await message.answer("❌ Пользователь не найден")
        amount_str = parts[2]

    await ensure_user(target)

    try:
        amount = float(amount_str)
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target
        )
        await log_transaction(conn, "ADD", from_user=message.from_user.id, to_user=target, amount=amount)

    await message.answer(f"✅ Начислено <b>+{amount:.2f}$</b> пользователю <code>{target}</code>")


@dp.message(Command("take"))
async def take(message: Message):
    await save_user(message)

    if message.from_user.id not in ADMINS:
        logger.warning(f"🚫 /take — unauthorized user={message.from_user.id}")
        return

    parts = message.text.split()
    reply = message.reply_to_message

    if reply and reply.from_user:
        if len(parts) < 2:
            return await message.answer("⚠️ Использование (реплай): /take 100")
        target = reply.from_user.id
        amount_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.answer("⚠️ Использование: /take @user 100\nИли ответь на сообщение игрока: /take 100")
        target = await get_user_id(parts[1])
        if target is None:
            return await message.answer("❌ Пользователь не найден")
        amount_str = parts[2]

    await ensure_user(target)

    try:
        amount = float(amount_str)
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", target)
        bal = row["balance"] if row else 0

        if bal < amount:
            return await message.answer("❌ У пользователя недостаточно средств")

        await conn.execute(
            "UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, target
        )
        await log_transaction(conn, "TAKE", from_user=target, to_user=None, amount=amount)

    await message.answer(f"✅ Снято <b>-{amount:.2f}$</b> у пользователя <code>{target}</code>")


@dp.message(Command("withdraw"))
async def withdraw(message: Message):
    await save_user(message)
    uid = message.from_user.id

    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /withdraw 100")

    try:
        amount = float(parts[1])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", uid)
        bal = row["balance"] if row else 0

        if bal < amount:
            return await message.answer("❌ Недостаточно средств на балансе")

        await conn.execute(
            "UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, uid
        )
        await log_transaction(conn, "WITHDRAW", from_user=uid, to_user=None, amount=amount)

    await message.answer(f"💸 Вывод выполнен: <b>-{amount:.2f}$</b>")

    bot: Bot = dp.get("bot")
    try:
        nick_str = message.from_user.username or str(uid)
        for admin_id in ADMINS:
            if bot:
                await bot.send_message(
                    admin_id,
                    f"🏧 <b>Запрос на вывод</b>\n"
                    f"👤 Пользователь: @{nick_str} (<code>{uid}</code>)\n"
                    f"💵 Сумма: <b>{amount:.2f}$</b>"
                )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось уведомить админа о выводе: {e}")


@dp.message(Command("pay"))
async def pay(message: Message):
    await save_user(message)
    sender = message.from_user.id

    parts = message.text.split()
    reply = message.reply_to_message

    if reply and reply.from_user:
        if len(parts) < 2:
            return await message.answer("⚠️ Использование (реплай): /pay 100")
        target = reply.from_user.id
        amount_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.answer("⚠️ Использование: /pay @user 100\nИли ответь на сообщение игрока: /pay 100")
        target = await get_user_id(parts[1])
        if target is None:
            return await message.answer("❌ Пользователь не найден")
        amount_str = parts[2]

    await ensure_user(target)

    try:
        amount = float(amount_str)
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    if sender == target:
        return await message.answer("⚠️ Нельзя переводить самому себе")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", sender)
        bal = row["balance"] if row else 0

        if bal < amount:
            return await message.answer("❌ Недостаточно средств на балансе")

        try:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, sender
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target
                )
                await log_transaction(conn, "PAY", from_user=sender, to_user=target, amount=amount)
        except Exception as e:
            logger.error(f"❌ [PAY] Transaction failed: {e}")
            return await message.answer("❌ Ошибка транзакции, попробуй ещё раз")

    await message.answer(f"✅ Перевод выполнен: <b>{amount:.2f}$</b> → <code>{target}</code>")


@dp.message(Command("top"))
async def top(message: Message):
    await save_user(message)
    logger.info(f"🏆 /top — user={message.from_user.id}")

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT nickname, balance
            FROM users
            WHERE balance > 0
            ORDER BY balance DESC
            LIMIT 10
        """)

    if not rows:
        return await message.answer("❌ Список пуст")

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>ТОП ИГРОКОВ</b>\n\n"

    for i, r in enumerate(rows, 1):
        nick = r["nickname"] or "No name"
        bal = r["balance"]
        medal = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{medal} {nick} — <b>{bal:.2f}$</b>\n"

    await message.answer(text)


@dp.message(Command("history"))
async def history(message: Message):
    await save_user(message)
    uid = message.from_user.id

    if uid not in ADMINS:
        logger.warning(f"🚫 /history — unauthorized user={uid}")
        return

    if message.chat.type != "private":
        return await message.answer("🔒 Эта команда доступна только в личке с ботом")

    parts = message.text.split()
    page = 1
    if len(parts) > 1 and parts[1].isdigit():
        page = max(1, int(parts[1]))

    limit = 20
    offset = (page - 1) * limit

    logger.info(f"📋 /history — admin={uid} page={page}")

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM transactions")
        rows = await conn.fetch("""
            SELECT type, from_user, to_user, amount, created_at
            FROM transactions
            ORDER BY id DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)

    if not rows:
        return await message.answer("📭 История транзакций пуста")

    total_pages = (total + limit - 1) // limit

    icons = {
        "PAY": "💸", "ADD": "➕", "TAKE": "➖",
        "WITHDRAW": "🏧", "DEPOSIT": "🏦", "BANKWITHDRAW": "🏦",
    }

    text = f"📋 <b>Транзакции — страница {page}/{total_pages}</b>\n\n"
    for r in rows:
        type_ = r["type"]
        from_u = r["from_user"]
        to_u = r["to_user"]
        amount = r["amount"]
        created_at = r["created_at"]
        icon = icons.get(type_, "🔄")
        to_str = f"→ <code>{to_u}</code>" if to_u else ""
        text += f"{icon} <b>{type_}</b> | <code>{from_u}</code> {to_str} | <b>{amount:.2f}$</b> | {created_at}\n"

    if total_pages > 1 and page < total_pages:
        text += f"\n📌 Следующая страница: /history {page + 1}"

    await message.answer(text)


@dp.message(Command("deposit"))
async def deposit(message: Message):
    await save_user(message)
    uid = message.from_user.id

    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /deposit 100")

    try:
        amount = float(parts[1])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", uid)
        bal = row["balance"] if row else 0

        if bal < amount:
            return await message.answer("❌ Недостаточно средств на балансе")

        await conn.execute(
            "UPDATE users SET balance = balance - $1, bank = bank + $1 WHERE user_id = $2",
            amount, uid
        )
        await log_transaction(conn, "DEPOSIT", from_user=uid, to_user=None, amount=amount)

    logger.info(f"🏦 /deposit — user={uid} amount={amount}")
    await message.answer(f"🏦 Положено в банк: <b>+{amount:.2f}$</b>")


@dp.message(Command("bankwithdraw"))
async def bankwithdraw(message: Message):
    await save_user(message)
    uid = message.from_user.id

    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /bankwithdraw 100")

    try:
        amount = float(parts[1])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT bank FROM users WHERE user_id = $1", uid)
        bank = row["bank"] if row else 0

        if bank < amount:
            return await message.answer("❌ Недостаточно средств в банке")

        await conn.execute(
            "UPDATE users SET bank = bank - $1, balance = balance + $1 WHERE user_id = $2",
            amount, uid
        )
        await log_transaction(conn, "BANKWITHDRAW", from_user=uid, to_user=None, amount=amount)

    logger.info(f"🏦 /bankwithdraw — user={uid} amount={amount}")
    await message.answer(f"💰 Снято из банка: <b>+{amount:.2f}$</b>")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    logger.info(f"❓ /help — user={message.from_user.id}")
    await message.answer(
        "📖 <b>Список команд</b>\n\n"
        "👤 /profile — твой профиль\n"
        "✏️ /nick &lt;имя&gt; — сменить никнейм\n"
        "💰 /deposit &lt;сумма&gt; — положить в банк\n"
        "🏦 /bankwithdraw &lt;сумма&gt; — снять из банка\n"
        "💸 /withdraw &lt;сумма&gt; — вывести деньги\n"
        "💳 /pay @user &lt;сумма&gt; — перевести деньги\n"
        "🏆 /top — топ игроков\n\n"
        "🔐 <b>Только для админов (в личке):</b>\n"
        "➕ /add @user &lt;сумма&gt;\n"
        "➖ /take @user &lt;сумма&gt;\n"
        "📋 /history [страница] — все транзакции\n"
        "🔍 /checkprofile @user — профиль игрока"
    )


@dp.message(Command("checkprofile"))
async def checkprofile(message: Message):
    await save_user(message)
    uid = message.from_user.id

    if uid not in ADMINS:
        logger.warning(f"🚫 /checkprofile — unauthorized user={uid}")
        return

    parts = message.text.split()
    reply = message.reply_to_message

    if reply and reply.from_user:
        target = reply.from_user.id
        if target == uid:
            return await message.answer("⚠️ Это твоё собственное сообщение. Укажи другого пользователя.")
    else:
        if len(parts) < 2:
            return await message.answer("⚠️ Использование: /checkprofile @user\nИли ответь на сообщение игрока: /checkprofile")
        target = await get_user_id(parts[1])
        if target is None:
            return await message.answer("❌ Пользователь не найден")
        if target == uid:
            return await message.answer("⚠️ Это твой собственный профиль. Используй /profile.")

    await ensure_user(target)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT nickname, balance, bank FROM users WHERE user_id = $1", target
        )
        tx_rows = await conn.fetch("""
            SELECT type, from_user, to_user, amount, created_at
            FROM transactions
            WHERE from_user = $1 OR to_user = $1
            ORDER BY id DESC
            LIMIT 5
        """, target)

    nick = row["nickname"] if row and row["nickname"] else "No name"
    bal = row["balance"] if row else 0
    bank = row["bank"] if row else 0

    icons = {"PAY": "💸", "ADD": "➕", "TAKE": "➖", "WITHDRAW": "🏧", "DEPOSIT": "🏦", "BANKWITHDRAW": "🏦"}

    text = (
        f"👤 <b>Профиль пользователя</b>\n\n"
        f"🆔 ID: <code>{target}</code>\n"
        f"📛 Никнейм: <b>{nick}</b>\n"
        f"💰 Баланс: <b>{bal:.2f}$</b>\n"
        f"🏦 Банк: <b>{bank:.2f}$</b>\n"
    )

    if tx_rows:
        text += "\n📋 <b>Последние транзакции:</b>\n"
        for r in tx_rows:
            type_ = r["type"]
            from_u = r["from_user"]
            to_u = r["to_user"]
            amount = r["amount"]
            created_at = r["created_at"]
            icon = icons.get(type_, "🔄")
            to_str = f"→ <code>{to_u}</code>" if to_u else ""
            text += f"{icon} <b>{type_}</b> | <code>{from_u}</code> {to_str} | <b>{amount:.2f}$</b> | {created_at}\n"
    else:
        text += "\n📭 Транзакций пока нет."

    logger.info(f"🔍 /checkprofile — admin={uid} target={target}")
    await message.answer(text)


@dp.message(Command("resetallbalances_x7k2m"))
async def reset_all_balances(message: Message):
    await save_user(message)
    uid = message.from_user.id

    if uid not in ADMINS:
        return

    if message.chat.type != "private":
        return

    parts = message.text.split()

    if len(parts) < 2 or parts[1] != "CONFIRM":
        return await message.answer(
            "⚠️ Для подтверждения сброса ВСЕХ балансов введи:\n"
            "<code>/resetallbalances_x7k2m CONFIRM</code>"
        )

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE balance > 0 OR bank > 0"
        )
        await conn.execute("UPDATE users SET balance = 0, bank = 0")
        await log_transaction(conn, "RESET_ALL", from_user=uid, to_user=None, amount=0)

    logger.warning(f"🔴 RESET ALL BALANCES — admin={uid}, affected={count} users")
    await message.answer(
        f"✅ Все балансы обнулены.\n"
        f"👥 Затронуто пользователей: <b>{count}</b>"
    )


@dp.message(F.from_user)
async def track(message: Message):
    await save_user(message)


# =========================
# WEB SERVER
# =========================
async def handle(request):
    return web.Response(text="Bot running")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", 7860)
    await site.start()

    logger.info("🌐 Web server started on port 7860")


# =========================
# MAIN
# =========================
async def main():
    await init_db()

    bot = Bot(
        token=API_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp["bot"] = bot

    await run_web_server()
    logger.info("🤖 Bot started")

    while True:
        try:
            await dp.start_polling(bot)
        except TelegramNetworkError as e:
            logger.error(f"🌐 Telegram network error: {e}")
            await asyncio.sleep(5)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Bot stopped")
            break
        except Exception as e:
            logger.error(f"💥 Unexpected error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
