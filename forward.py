import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_ID = 33621079
API_HASH = "5378ac906c789310f63f3c60f2063b6e"
BOT_TOKEN = "8472836665:AAGqmM0rVEbnWA_xjYdjmYh2wd6ytgHNRBk"
PHONE = "79832378779"

user_app = Client(
    "user_session",
    phone_number=PHONE,
    api_id=API_ID,
    api_hash=API_HASH,
    lang_code="ru",
    # --- Новые параметры ---
    max_concurrent_transmissions=3,
    sleep_threshold=30
)

CLIENT_BOT_ID = 1711822710

main = True

if main:
    WORK_GROUP = -1003702747405
    THREAD_NOW_ID = 2
    THREAD_ORDER_ID = None
    THREAD_DELIVERY_ID = 74
else:
    WORK_GROUP = -1003646541060
    THREAD_NOW_ID = 3087
    THREAD_ORDER_ID = 1

@user_app.on_message(filters.private & filters.bot)
async def forwarder(client, message):
    if message.from_user.id != CLIENT_BOT_ID:
        return  # Игнорируем сообщения от других ботов

    try:

        await message.forward(
            chat_id=WORK_GROUP
        )
        logging.info(f"📨 Сообщение от бота {CLIENT_BOT_ID} переслано в группу {WORK_GROUP}")
    except Exception as e:
        logging.error(f"❌ Не удалось переслать сообщение: {e}")

logging.info("🔁 Запущен мониторинг сообщений от клиентского бота...")

# --- Запуск бота ---
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )

    logging.info("🔄 Запуск бота...")

    logging.info("🚀 Бот успешно запущен и готов к работе.")
    user_app.run()