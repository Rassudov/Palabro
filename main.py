import os
import logging
import sqlite3
import pandas as pd
import datetime
import random

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler,
)

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Константы состояний для обучения ---
WAIT_FOR_SHOW, WAIT_FOR_RATING = range(2)

# --- Параметры БД ---
DB_NAME = "words.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Таблица со словами (слово и перевод)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE NOT NULL,
            translation TEXT NOT NULL
        )
    """)
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            words_per_day INTEGER DEFAULT 5
        )
    """)
    # Таблица индивидуального прогресса по словам для каждого пользователя
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            word_id INTEGER NOT NULL,
            repetition INTEGER DEFAULT 0,
            interval INTEGER DEFAULT 0,
            EF REAL DEFAULT 2.5,
            next_review TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, word_id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Функция для получения текущего слова (либо due, либо новое, если лимит не исчерпан) ---
def get_due_or_new_word(telegram_id: int):
    now = datetime.datetime.utcnow()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Ищем слово, которое уже пора повторить (next_review <= now)
    cursor.execute("""
        SELECT up.id, up.word_id, w.word, w.translation, up.repetition, up.interval, up.EF, up.next_review
        FROM user_progress up
        JOIN words w ON up.word_id = w.id
        WHERE up.telegram_id = ? AND datetime(up.next_review) <= datetime(?)
        ORDER BY datetime(up.next_review) ASC
        LIMIT 1
    """, (telegram_id, now))
    row = cursor.fetchone()
    if row:
        conn.close()
        return {
            'progress_id': row[0],
            'word_id': row[1],
            'word': row[2],
            'translation': row[3],
            'repetition': row[4],
            'interval': row[5],
            'EF': row[6],
            'next_review': row[7]
        }
    # Если нет due-слов, проверяем возможность добавления нового слова
    cursor.execute("SELECT words_per_day FROM users WHERE telegram_id = ?", (telegram_id,))
    res = cursor.fetchone()
    daily_limit = res[0] if res else 5

    today_str = now.strftime('%Y-%m-%d')
    cursor.execute("""
        SELECT COUNT(*) FROM user_progress
        WHERE telegram_id = ? AND repetition = 0 AND date(added_date) = ?
    """, (telegram_id, today_str))
    count_new = cursor.fetchone()[0]
    if count_new < daily_limit:
        # Выбираем слово, которого ещё нет в прогрессе пользователя
        cursor.execute("""
            SELECT id FROM words
            WHERE id NOT IN (
                SELECT word_id FROM user_progress WHERE telegram_id = ?
            )
        """, (telegram_id,))
        available = cursor.fetchall()
        if available:
            word_id = random.choice(available)[0]
            cursor.execute("SELECT word, translation FROM words WHERE id = ?", (word_id,))
            word_details = cursor.fetchone()
            # Добавляем новое слово в прогресс
            cursor.execute("""
                INSERT INTO user_progress (telegram_id, word_id, next_review)
                VALUES (?, ?, ?)
            """, (telegram_id, word_id, now))
            conn.commit()
            # Считываем добавленную запись
            progress_id = cursor.lastrowid
            conn.close()
            return {
                'progress_id': progress_id,
                'word_id': word_id,
                'word': word_details[0],
                'translation': word_details[1],
                'repetition': 0,
                'interval': 0,
                'EF': 2.5,
                'next_review': now
            }
        else:
            conn.close()
            return None
    else:
        conn.close()
        return None

# --- Функция обновления прогресса с использованием алгоритма SM-2 ---
def update_progress(progress, grade: int):
    """
    progress: dict с ключами: repetition, interval, EF, progress_id, word_id, etc.
    grade: оценка (0-5)
    """
    now = datetime.datetime.utcnow()
    repetition = progress['repetition']
    interval = progress['interval']
    EF = progress['EF']

    if grade < 3:
        # Если оценка меньше 3 – повторения сбрасываются
        repetition = 0
        interval = 1  # повтор через 1 день
    else:
        if repetition == 0:
            interval = 1
            repetition = 1
        elif repetition == 1:
            interval = 6
            repetition = 2
        else:
            interval = round(interval * EF)
            repetition += 1

    # Обновляем EF (коэффициент легкости)
    EF = EF + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    if EF < 1.3:
        EF = 1.3

    next_review = now + datetime.timedelta(days=interval)
    progress.update({
        'repetition': repetition,
        'interval': interval,
        'EF': EF,
        'next_review': next_review
    })

    # Обновляем запись в БД
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE user_progress
        SET repetition = ?, interval = ?, EF = ?, next_review = ?
        WHERE id = ?
    """, (repetition, interval, EF, next_review, progress['progress_id']))
    conn.commit()
    conn.close()

# --- Обработчик команды /start ---
def start(update: Update, context: CallbackContext) -> None:
    telegram_id = update.message.chat_id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    conn.commit()
    conn.close()
    update.message.reply_text(
        "Привет! Я помогу тебе учить испанские слова.\n"
        "Для загрузки слов используй команду /upload\n"
        "Для начала тренировки отправь команду /train"
    )

# --- Обработчик загрузки Excel-файла ---
def upload_words(update: Update, context: CallbackContext) -> None:
    file = context.bot.get_file(update.message.document.file_id)
    file_path = f"temp_{update.message.chat_id}.xlsx"
    file.download(file_path)
    try:
        df = pd.read_excel(file_path)
    except Exception as e:
        update.message.reply_text("Ошибка при чтении файла. Убедись, что формат корректный.")
        os.remove(file_path)
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for _, row in df.iterrows():
        try:
            cursor.execute(
                "INSERT OR IGNORE INTO words (word, translation) VALUES (?, ?)",
                (row["Слово"], row["Перевод"])
            )
        except Exception as e:
            logger.error(f"Ошибка вставки: {e}")
    conn.commit()
    conn.close()
    os.remove(file_path)
    update.message.reply_text("Слова успешно загружены!")

# --- Начало тренировки (/train) ---
def start_training(update: Update, context: CallbackContext) -> int:
    telegram_id = update.message.chat_id
    progress = get_due_or_new_word(telegram_id)
    if not progress:
        update.message.reply_text("Нет слов для повторения или изучения на данный момент.")
        return ConversationHandler.END
    context.user_data['current_progress'] = progress

    # Отправляем слово (без перевода) и предлагаем показать ответ
    keyboard = [
        [InlineKeyboardButton("Показать ответ", callback_data="show_answer")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(f"Слово: {progress['word']}", reply_markup=reply_markup)
    return WAIT_FOR_SHOW

# --- Обработка нажатия кнопки "Показать ответ" ---
def show_answer(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    progress = context.user_data.get('current_progress')
    if not progress:
        query.edit_message_text("Ошибка: нет текущего слова.")
        return ConversationHandler.END

    # Показываем перевод
    text = f"Перевод: {progress['translation']}\n\nОцени, насколько хорошо ты запомнил слово (0-5):"
    # Кнопки для оценки
    keyboard = [
        [InlineKeyboardButton(str(i), callback_data=f"rate:{i}") for i in range(6)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text(text, reply_markup=reply_markup)
    return WAIT_FOR_RATING

# --- Обработка оценки пользователя ---
def receive_rating(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    data = query.data
    try:
        grade = int(data.split(":")[1])
    except (IndexError, ValueError):
        query.edit_message_text("Некорректная оценка.")
        return ConversationHandler.END

    progress = context.user_data.get('current_progress')
    if not progress:
        query.edit_message_text("Ошибка: нет данных о текущем слове.")
        return ConversationHandler.END

    # Обновляем данные по карточке с использованием SM-2
    update_progress(progress, grade)
    query.edit_message_text(f"Оценка {grade} сохранена. Готовимся к следующему слову...")
    
    # Ждем пару секунд перед переходом к следующему слову
    context.job_queue.run_once(lambda ctx: send_next_word(update, context), when=2)
    return WAIT_FOR_SHOW

def send_next_word(update: Update, context: CallbackContext):
    telegram_id = update.effective_chat.id
    progress = get_due_or_new_word(telegram_id)
    if not progress:
        context.bot.send_message(chat_id=telegram_id, text="На данный момент нет слов для повторения.")
        return ConversationHandler.END
    context.user_data['current_progress'] = progress
    keyboard = [
        [InlineKeyboardButton("Показать ответ", callback_data="show_answer")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    context.bot.send_message(chat_id=telegram_id, text=f"Слово: {progress['word']}", reply_markup=reply_markup)

# --- Обработчик команды /cancel для завершения тренировки ---
def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("Тренировка завершена.")
    return ConversationHandler.END

# --- Инициализация Telegram-бота ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
updater = Updater(TOKEN, use_context=True)
dispatcher = updater.dispatcher

# Обработчики команд
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(Filters.document.mime_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), upload_words))

# ConversationHandler для режима тренировки
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("train", start_training)],
    states={
        WAIT_FOR_SHOW: [
            CallbackQueryHandler(show_answer, pattern="^show_answer$")
        ],
        WAIT_FOR_RATING: [
            CallbackQueryHandler(receive_rating, pattern="^rate:")
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)
dispatcher.add_handler(conv_handler)

# --- Запуск бота ---
if __name__ == "__main__":
    updater.start_polling()
    updater.idle()
