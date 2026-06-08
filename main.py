import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from itertools import groupby
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# 1. НАЛАШТУВАННЯ ТА ЛОГІКА БОТА
# ==========================================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = "8717177282:AAEQ045JKyIO56Gc7pM9oSmQe1NwposQPEY"
DB_NAME = "planner.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
USER_ID_STORAGE = None


def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Все задачи")
    builder.button(text="🗑️ Удалить задачу")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


# ==========================================
# 2. МОДУЛЬ БАЗИ ДАНИХ (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_text TEXT,
            target_date TEXT,
            is_important INTEGER DEFAULT 0,
            task_type TEXT DEFAULT 'regular'
        )
    ''')
    conn.commit()
    conn.close()


def add_task(user_id, task_text, target_date, is_important=0, task_type='regular'):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO tasks (user_id, task_text, target_date, is_important, task_type)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, task_text, target_date, is_important, task_type))
    conn.commit()
    conn.close()


def get_all_actual_tasks(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_date_str = datetime.now().strftime('%Y-%m-%d')
    cursor.execute('''
        SELECT id, task_text, target_date, is_important, task_type 
        FROM tasks 
        WHERE user_id = ? AND target_date >= ?
        ORDER BY target_date ASC
    ''', (user_id, now_date_str))
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_task_by_id(user_id, task_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    changes = conn.total_changes
    conn.commit()
    conn.close()
    return changes > 0


def get_morning_summary(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    today_date = datetime.now().strftime('%Y-%m-%d')
    next_week_date = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')

    cursor.execute(
        "SELECT task_text, target_date FROM tasks WHERE user_id = ? AND target_date LIKE ? AND task_type = 'regular'",
        (user_id, f"{today_date}%"))
    today_tasks = cursor.fetchall()

    cursor.execute(
        "SELECT task_text, target_date FROM tasks WHERE user_id = ? AND target_date > ? AND target_date <= ? AND task_type = 'regular'",
        (user_id, f"{today_date} 23:59:59", f"{next_week_date} 23:59:59"))
    week_tasks = cursor.fetchall()

    cursor.execute(
        "SELECT task_text, target_date FROM tasks WHERE user_id = ? AND task_type = 'deadline' AND target_date >= ?",
        (user_id, today_date))
    deadlines = cursor.fetchall()

    cursor.execute(
        "SELECT task_text, target_date FROM tasks WHERE user_id = ? AND is_important = 1 AND target_date >= ?",
        (user_id, today_date))
    important_tasks = cursor.fetchall()

    conn.close()
    return today_tasks, week_tasks, deadlines, important_tasks


# ==========================================
# 3. МОДУЛЬ ПАРСЕРА ТЕКСТУ
# ==========================================
MONTHS_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}
WEEKDAYS_MAP = {
    "понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3, "пятницу": 4, "субботу": 5, "воскресенье": 6,
    "среда": 2, "пятница": 4, "суббота": 5
}


def parse_user_text(text: str):
    text_lower = text.lower()
    current_now = datetime.now()

    is_important = 0
    if "важно" in text_lower or "срочно" in text_lower or "!" in text:
        is_important = 1
        text = re.sub(r'(?i)важно|срочно|!', '', text).strip()

    task_type = 'regular'
    if "до " in text_lower:
        task_type = 'deadline'

    # Парсинг дати
    date_str = None
    if "послезавтра" in text_lower:
        date_str = (current_now + timedelta(days=2)).strftime('%Y-%m-%d')
        text = re.sub(r'(?i)\bпослезавтра\b', '', text)
    elif "завтра" in text_lower:
        date_str = (current_now + timedelta(days=1)).strftime('%Y-%m-%d')
        text = re.sub(r'(?i)\bзавтра\b', '', text)
    elif "сегодня" in text_lower:
        date_str = current_now.strftime('%Y-%m-%d')
        text = re.sub(r'(?i)\bсегодня\b', '', text)

    if not date_str:
        for day_name, day_num in WEEKDAYS_MAP.items():
            if day_name in text_lower:
                days_ahead = day_num - current_now.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                date_str = (current_now + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
                text = re.sub(rf'(?i)\b(?:в|во)?\s*{day_name}\b', '', text)
                break

    if not date_str:
        date_match = re.search(
            r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)',
            text_lower)
        if date_match:
            day = int(date_match.group(1))
            month = MONTHS_MAP[date_match.group(2)]
            year = current_now.year
            if month < current_now.month or (month == current_now.month and day < current_now.day):
                year += 1
            date_str = f"{year}-{month:02d}-{day:02d}"
            text = text.replace(date_match.group(0), "")

    if not date_str:
        date_str = current_now.strftime('%Y-%m-%d')

    # Парсинг часу (підтримує "11 40", "11:40", "11.40", "16")
    time_str = "08:00:00"
    time_with_minutes = re.search(r'(?:на|в)\s+(\d{1,2})[:.\s](\d{2})', text_lower)
    if time_with_minutes:
        hours = int(time_with_minutes.group(1))
        minutes = int(time_with_minutes.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            time_str = f"{hours:02d}:{minutes:02d}:00"
            text = text.replace(time_with_minutes.group(0), "")
    else:
        time_just_hours = re.search(r'(?:на|в)\s+(\d{1,2})\b', text_lower)
        if time_just_hours:
            hours = int(time_just_hours.group(1))
            if 0 <= hours <= 23:
                time_str = f"{hours:02d}:00:00"
                text = text.replace(time_just_hours.group(0), "")

    target_datetime = f"{date_str} {time_str}"
    clean_text = re.sub(r'(?i)\bдо\b', '', text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()

    if not clean_text:
        clean_text = "Новая задача"

    return clean_text, target_datetime, is_important, task_type


# ==========================================
# 4. ОБРОБНИКИ ПОВІДОМЛЕНЬ ТЕЛЕГРАМ
# ==========================================
async def send_morning_reminder():
    global USER_ID_STORAGE
    if not USER_ID_STORAGE:
        return
    today_t, week_t, dead_t, imp_t = get_morning_summary(USER_ID_STORAGE)
    if not today_t and not week_t and not dead_t and not imp_t:
        return
    text = "☀️ **Доброе утро! Твой план на сегодня и ближайшее время:**\n\n"
    if today_t:
        text += "📅 **Планы на СЕГОДНЯ:**\n"
        for t, d in today_t:
            text += f" • {t} (в {d.split()[1][:5]})\n"
        text += "\n"
    if imp_t:
        text += "🔥 **ВАЖНЫЕ задачи:**\n"
        for t, d in imp_t:
            text += f" • {t} — до {d}\n"
        text += "\n"
    if dead_t:
        text += "⌛ **Текущие ДЕДЛАЙНЫ:**\n"
        for t, d in dead_t:
            text += f" • {t} (до: {d.split()[0]})\n"
        text += "\n"
    if week_t:
        text += "🗓️ **Планы на НЕДЕЛЮ:**\n"
        for t, d in week_t:
            text += f" • {t} ({d.split()[0]})\n"
    await bot.send_message(USER_ID_STORAGE, text, parse_mode="Markdown")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    global USER_ID_STORAGE
    USER_ID_STORAGE = message.from_user.id
    await message.answer("Привет! Я твой умный планер.", reply_markup=get_main_menu())


@dp.message(lambda msg: msg.text == "📋 Все задачи")
async def show_all_tasks(message: types.Message):
    global USER_ID_STORAGE
    USER_ID_STORAGE = message.from_user.id
    tasks = get_all_actual_tasks(message.from_user.id)
    if not tasks:
        await message.answer("У тебя пока нет активных задач!")
        return

    text = "📋 Твои актуальные задачи:\n\n"

    def get_date_key(task_row):
        return task_row[2].split()[0]

    for date_day, group in groupby(tasks, key=get_date_key):
        text += f"─────── 📅 {date_day} ───────\n"
        for task in group:
            t_id, t_text, t_date, is_imp, t_type = task
            imp_tag = "🔥 " if is_imp else ""
            type_tag = "⌛ [Дедлайн] " if t_type == 'deadline' else "• "
            time_part = t_date.split()[1][:5]
            text += f"{type_tag}{imp_tag}{t_text} (в {time_part}) \n└ Удалить: /del_{t_id}\n"
        text += "\n"
    await message.answer(text)


@dp.message(lambda msg: msg.text and msg.text.startswith("/del_"))
async def handle_delete_command(message: types.Message):
    try:
        task_id = int(message.text.split("_")[1])
        success = delete_task_by_id(message.from_user.id, task_id)
        if success:
            await message.answer(f"✅ Задача #{task_id} успешно удалена!", reply_markup=get_main_menu())
        else:
            await message.answer("⚠️ Задача не найдена.", reply_markup=get_main_menu())
    except (IndexError, ValueError):
        await message.answer("Неверный формат команды.")


@dp.message(lambda msg: msg.text == "🗑️ Удалить задачу")
async def delete_instruction(message: types.Message):
    await message.answer("Нажми кнопку **📋 Все задачи** и кликни по ссылке `/del_номер` возле задания.")


@dp.message()
async def handle_new_task(message: types.Message):
    global USER_ID_STORAGE
    USER_ID_STORAGE = message.from_user.id

    if message.text.startswith("/del"):
        await message.answer("Используй ссылки с нижним подчеркиванием: /del_1")
        return

    task_text, target_date, is_important, task_type = parse_user_text(message.text)
    add_task(message.from_user.id, task_text, target_date, is_important, task_type)

    importance_str = "🔥 Важное! " if is_important else ""
    type_str = "⌛ Дедлайн" if task_type == 'deadline' else "📅 Обычное"
    await message.answer(
        f"Задание записано!\n📝 Текст: {task_text}\n📅 Время: {target_date}\nТип: {type_str} | {importance_str}",
        reply_markup=get_main_menu())


# ==========================================
# 5. ГОЛОВНИЙ ЗАПУСК
# ==========================================
async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(send_morning_reminder, "cron", hour=8, minute=0)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())