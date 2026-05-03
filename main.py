import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiocryptopay import AioCryptoPay, Networks

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = 'ВАШ_ТЕЛЕГРАМ_ТОКЕН'
CRYPTO_TOKEN = 'ВАШ_КРИПТОБОТ_ТОКЕН'
ADMIN_ID = 0  # ВАШ ID (ЧИСЛОМ)
COMMISSION = 0.05  # 5%

# Настройка логирования
logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
# Используем тестовую сеть (testnet), если токен тестовый. Для основы смени на mainnet.
crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)

# --- СОСТОЯНИЯ (FSM) ---
class SellAccount(StatesGroup):
    details = State()
    price = State()
    photo = State()
    credentials = State()

class AddFree(StatesGroup):
    waiting_list = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect("market.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, 
            balance REAL DEFAULT 0, 
            last_free_gift TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            info TEXT,
            price REAL,
            photo_id TEXT,
            creds TEXT,
            status TEXT DEFAULT 'active')""")
        await db.execute("""CREATE TABLE IF NOT EXISTS free_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creds TEXT)""")
        await db.commit()

# --- ОБЩИЕ ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    async with aiosqlite.connect("market.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (message.from_user.id,))
        await db.commit()
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить аккаунт", callback_data="buy_menu")
    kb.button(text="💰 Продать аккаунт", callback_data="sell_start")
    kb.button(text="🎁 Бесплатный (раз в сутки)", callback_data="get_free")
    if message.from_user.id == ADMIN_ID:
        kb.button(text="🔑 Админ: Добавить раздачу", callback_data="admin_add_free")
    kb.adjust(1)
    await message.answer(f"Привет, {message.from_user.first_name}! Это маркет аккаунтов Black Russia.", reply_markup=kb.as_markup())

# --- ЛОГИКА ПРОДАЖИ (FSM) ---
@dp.callback_query(F.data == "sell_start")
async def sell_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Опишите аккаунт (Сервер, уровень, донат, имущество):")
    await state.set_state(SellAccount.details)

@dp.message(SellAccount.details)
async def sell_details(message: types.Message, state: FSMContext):
    await state.update_data(details=message.text)
    await message.answer("Введите желаемую цену в USDT:")
    await state.set_state(SellAccount.price)

@dp.message(SellAccount.price)
async def sell_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("Отправьте один скриншот аккаунта:")
        await state.set_state(SellAccount.photo)
    except ValueError:
        await message.answer("Ошибка! Введите число (например: 10.5)")

@dp.message(SellAccount.photo, F.photo)
async def sell_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await message.answer("Введите данные в формате Логин:Пароль (их увидит только покупатель):")
    await state.set_state(SellAccount.credentials)

@dp.message(SellAccount.credentials)
async def sell_final(message: types.Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect("market.db") as db:
        cur = await db.execute(
            "INSERT INTO accounts (seller_id, info, price, photo_id, creds, status) VALUES (?, ?, ?, ?, ?, ?)",
            (message.from_user.id, data['details'], data['price'], data['photo_id'], message.text, 'moderation')
        )
        acc_id = cur.lastrowid
        await db.commit()

    await message.answer("✅ Отправлено на модерацию. Ожидайте уведомления!")
    
    # К админу
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"mod_yes_{acc_id}")
    kb.button(text="❌ Отклонить", callback_data=f"mod_no_{acc_id}")
    await bot.send_photo(
        ADMIN_ID, photo=data['photo_id'],
        caption=f"⚙️ МОДЕРАЦИЯ #{acc_id}\n\nИнфо: {data['details']}\nЦена: {data['price']} USDT\nДанные: {message.text}",
        reply_markup=kb.as_markup()
    )
    await state.clear()

# --- МОДЕРАЦИЯ ---
@dp.callback_query(F.data.startswith("mod_"))
async def moderation_process(callback: types.CallbackQuery):
    action, decision, acc_id = callback.data.split("_")
    async with aiosqlite.connect("market.db") as db:
        if decision == "yes":
            await db.execute("UPDATE accounts SET status = 'active' WHERE id = ?", (acc_id,))
            await callback.message.edit_caption(caption="✅ Одобрено")
        else:
            await db.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
            await callback.message.edit_caption(caption="❌ Отклонено/Удалено")
        await db.commit()

# --- ПОКУПКА ---
@dp.callback_query(F.data == "buy_menu")
async def buy_menu(callback: types.CallbackQuery):
    async with aiosqlite.connect("market.db") as db:
        async with db.execute("SELECT id, price, info FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()
    
    if not accounts:
        return await callback.answer("На данный момент нет активных товаров.", show_alert=True)
    
    for acc in accounts:
        kb = InlineKeyboardBuilder()
        kb.button(text=f"💳 Купить за {acc[1]} USDT", callback_data=f"pay_{acc[0]}")
        await callback.message.answer(f"📦 Товар #{acc[0]}\n{acc[2]}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("pay_"))
async def create_payment(callback: types.CallbackQuery):
    acc_id = callback.data.split("_")[1]
    async with aiosqlite.connect("market.db") as db:
        async with db.execute("SELECT price FROM accounts WHERE id = ?", (acc_id,)) as cursor:
            res = await cursor.fetchone()
    
    invoice = await crypto.create_invoice(asset='USDT', amount=res[0])
    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить в CryptoBot", url=invoice.pay_url)
    kb.button(text="Проверить оплату", callback_data=f"check_{invoice.invoice_id}_{acc_id}")
    await callback.message.answer(f"Счет на {res[0]} USDT создан.", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("check_"))
async def check_payment(callback: types.CallbackQuery):
    _, inv_id, acc_id = callback.data.split("_")
    invoices = await crypto.get_invoices(invoice_ids=inv_id)
    
    if invoices and invoices.status == 'paid':
        async with aiosqlite.connect("market.db") as db:
            async with db.execute("SELECT seller_id, price, creds FROM accounts WHERE id = ?", (acc_id,)) as cursor:
                acc_data = await cursor.fetchone()
            
            # 1. Выдаем товар
            await callback.message.answer(f"✅ Оплата прошла! Ваши данные:\n`{acc_data[2]}`", parse_mode="Markdown")
            
            # 2. Выплата продавцу (5% комиссия)
            pay_amount = acc_data[1] * (1 - COMMISSION)
            check = await crypto.create_check(asset='USDT', amount=pay_amount)
            await bot.send_message(acc_data[0], f"💰 Ваш товар #{acc_id} куплен! Получите выплату: {check.bot_check_url}")
            
            # 3. Удаляем из базы
            await db.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
            await db.commit()
    else:
        await callback.answer("❌ Оплата не найдена.", show_alert=True)

# --- БЕСПЛАТНАЯ РАЗДАЧА ---
@dp.callback_query(F.data == "get_free")
async def get_free(callback: types.CallbackQuery):
    async with aiosqlite.connect("market.db") as db:
        async with db.execute("SELECT last_free_gift FROM users WHERE id = ?", (callback.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            if row[0]:
                if datetime.now() < datetime.fromisoformat(row[0]) + timedelta(days=1):
                    return await callback.answer("❌ Возвращайтесь через 24 часа!", show_alert=True)
        
        async with db.execute("SELECT id, creds FROM free_accounts LIMIT 1") as cursor:
            gift = await cursor.fetchone()
            if not gift:
                return await callback.answer("Аккаунты закончились!", show_alert=True)
            
            await callback.message.answer(f"🎁 Твой аккаунт:\n`{gift[1]}`", parse_mode="Markdown")
            await db.execute("UPDATE users SET last_free_gift = ? WHERE id = ?", (datetime.now().isoformat(), callback.from_user.id))
            await db.execute("DELETE FROM free_accounts WHERE id = ?", (gift[0],))
            await db.commit()

# --- АДМИН-ДОБАВЛЕНИЕ РАЗДАЧИ ---
@dp.callback_query(F.data == "admin_add_free")
async def admin_free_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришли список аккаунтов (строка = аккаунт):")
    await state.set_state(AddFree.waiting_list)

@dp.message(AddFree.waiting_list)
async def admin_free_save(message: types.Message, state: FSMContext):
    lines = message.text.split('\n')
    async with aiosqlite.connect("market.db") as db:
        for line in lines:
            if line.strip():
                await db.execute("INSERT INTO free_accounts (creds) VALUES (?)", (line.strip(),))
        await db.commit()
    await message.answer(f"Добавлено {len(lines)} шт.")
    await state.clear()

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
