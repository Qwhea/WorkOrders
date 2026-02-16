import ast
import asyncio
import logging
import re
import socket
import subprocess
import textwrap
from datetime import datetime, timezone
from textwrap import dedent

from pyrogram import Client, filters
import pandas as pd
import json
import os
import requests

import tempfile
import win32print
import win32api

import time

from datetime import timedelta
from fuzzywuzzy import fuzz

import speech_recognition as sr
from pydub import AudioSegment
import tempfile
import os
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType


USER_PHONE_MAP = {}  # {user_id: "79991234567"}


# --- Конфигурация ---
API_ID = 33621079
API_HASH = "5378ac906c789310f63f3c60f2063b6e"
BOT_TOKEN = "8472836665:AAGqmM0rVEbnWA_xjYdjmYh2wd6ytgHNRBk"
PHONE = "79832378779"


OPENROUTER_API_KEY = "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"
OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324"

main = False

if main:
    WORK_GROUP = -1003702747405
    THREAD_NOW_ID = 2
    THREAD_ORDER_ID = None
    THREAD_DELIVERY_ID = 74
else:
    WORK_GROUP = -1003646541060
    THREAD_NOW_ID = 3087
    THREAD_ORDER_ID = None
    THREAD_DELIVERY_ID = 4462


ACTIVE_ORDERS_JSON = "active_orders.json"  # ← новое
FUTURE_ORDERS_JSON = "future_orders.json"  # ← новое
PENDING_ORDERS_JSON = "pending_orders.json"
MENU_XLSX = "menu.xlsx"
ADDRESS_XLSX = "adress.xlsx"
PRINTER_NAME = "80C"
ADMIN_MESSAGES = []  # Список message_id, отправленных в /admin

awaiting_edit_from_message = None  # Будет содержать order_id

MAX_QUANTITY = 29  # Максимальное количество, которое можно указать


bot_app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

PHONE_JSON = "user_phones.json"

def find_menu_item_fuzzy(name: str):
    """
    Находит блюдо из меню по частичному или нечёткому совпадению.
    """
    name = name.strip().lower()

    # 1. Точное совпадение
    for item in MENU_ITEMS:
        if item["name"].lower() == name:
            return item

    # 2. Частичное вхождение
    for item in MENU_ITEMS:
        if name in item["name"].lower():
            return item

    # 3. Fuzzy-поиск
    best_match = None
    best_ratio = 0
    for item in MENU_ITEMS:
        ratio = fuzz.ratio(name, item["name"].lower())
        if ratio > best_ratio and ratio >= 85:
            best_ratio = ratio
            best_match = item

    if best_match:
        logging.info(f"🔍 Fuzzy-совпадение: '{name}' → '{best_match['name']}' (схожесть: {best_ratio})")
        return best_match

    return None

def load_user_phones():
    """Загружает привязки user_id → phone из файла."""
    global USER_PHONE_MAP
    if os.path.exists(PHONE_JSON):
        try:
            with open(PHONE_JSON, "r", encoding="utf-8") as f:
                USER_PHONE_MAP = json.load(f)
                # Преобразуем ключи в int
                USER_PHONE_MAP = {int(k): v for k, v in USER_PHONE_MAP.items()}
            logging.info(f"✅ Загружено {len(USER_PHONE_MAP)} привязок телефонов")
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки {PHONE_JSON}: {e}")
    else:
        USER_PHONE_MAP = {}

def save_user_phones():
    """Сохраняет привязки в файл."""
    try:
        with open(PHONE_JSON, "w", encoding="utf-8") as f:
            json.dump(USER_PHONE_MAP, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения {PHONE_JSON}: {e}")

def voice_to_text(file_path: str) -> str:
    """
    Конвертирует голосовое сообщение в текст.
    :param file_path: путь к .ogg файлу
    :return: распознанный текст или пустая строка
    """
    try:
        # Конвертация OGG → WAV
        audio = AudioSegment.from_file(file_path)
        wav_path = file_path.replace(".ogg", ".wav")
        audio.export(wav_path, format="wav")

        # Распознавание речи
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")

        # Удаляем временный WAV
        if os.path.exists(wav_path):
            os.remove(wav_path)

        return text.strip()

    except sr.UnknownValueError:
        print("❌ Google не смог распознать аудио")
        return ""
    except sr.RequestError as e:
        print(f"❌ Ошибка сервиса распознавания: {e}")
        return ""
    except Exception as e:
        print(f"❌ Ошибка при обработке аудио: {e}")
        return ""

async def animate_waiting(message):
    """
    Анимирует сообщение: "Обработка." → "Обработка.." → "Обработка..."
    """
    dots = 0
    try:
        while True:
            dots = (dots % 3) + 1
            await message.edit_text(f"⏳ Обработка{'.' * dots}")
            await asyncio.sleep(0.7)
    except asyncio.CancelledError:
        pass

async def update_message_to_order_check(order_id, message):
    """
    Редактирует существующее сообщение (например, "Обработка") на чек заказа.
    Используется для анимации ожидания → чек.
    """
    state = ORDER_STATE.get(order_id)
    if not state:
        logging.warning(f"❌ Не найдено состояние для order_id={order_id}")
        return

    delivery_zone = state.get("delivery_zone")
    delivery_cost = state.get("delivery_price", 0)
    delivery_date = state.get("delivery_date")

    total = calculate_total(state["items"], delivery_price=delivery_cost)
    status_emoji = "⏳"
    order_text = (
            f"{status_emoji} <b>Заказ</b>\n"
            f"📞 Телефон: {state['phone'] or 'не указан'}\n"
            f"⏰ Время: {state['time'] or 'не указано'}\n"
            + (f"📅 Дата: {delivery_date}\n" if delivery_date else "")
            + f"🏠 Адрес: {state['address'] or 'не указан'}\n"
              f"📍 Зона: {delivery_zone if delivery_zone else 'Не определена'}\n"
              f"🚚 Доставка: {delivery_cost} ₽\n\n"
              f"🍣 Блюда:\n" + "\n".join([
        f"• {it['qty']}x {it['name']} — {it['qty'] * it.get('source_price', 0)} ₽"
        + (f"\n  ⚠️ {it['comment'].capitalize()}" if it['comment'] else "")
        for it in state["items"]
    ]) +
            f"\n\n💰 <b>Итого: {total} ₽</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Отправить заказ", callback_data=f"send_to_group:{order_id}")],
        [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order:{order_id}")]
    ])

    try:
        await message.edit_text(order_text, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"❌ Ошибка при редактировании сообщения в чек: {e}")

@bot_app.on_message(filters.private & filters.voice)
async def handle_voice_message(client, message):
    user_id = message.from_user.id
    try:
        os.makedirs("downloads", exist_ok=True)
        voice_file = f"downloads/voice_{user_id}.ogg"

        logging.info(f"📥 Начинаем скачивание голосового сообщения от {user_id}...")
        await client.download_media(message, file_name=voice_file)
        logging.info(f"✅ Голосовое сообщение сохранено: {voice_file}")

        for _ in range(5):
            if os.path.exists(voice_file) and os.path.getsize(voice_file) > 100:
                break
            time.sleep(0.5)
        else:
            if not os.path.exists(voice_file):
                await message.reply("❌ Не удалось скачать голосовое сообщение.")
                return
            if os.path.getsize(voice_file) <= 100:
                await message.reply("❌ Голосовое сообщение слишком короткое или пустое.")
                os.remove(voice_file)
                return

        try:
            audio = AudioSegment.from_file(voice_file)
            wav_path = voice_file.replace(".ogg", ".wav")
            audio.export(wav_path, format="wav")
            logging.info(f"🔊 Конвертировано в WAV: {wav_path}")
        except Exception as e:
            logging.error(f"❌ Ошибка конвертации OGG → WAV: {e}")
            await message.reply("❌ Не удалось обработать аудиофайл.")
            return

        recognizer = sr.Recognizer()
        try:
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
                text = recognizer.recognize_google(audio_data, language="ru-RU")
                logging.info(f"🗣️ Распознано: {text}")
        except sr.UnknownValueError:
            await message.reply("❌ Не удалось распознать речь.")
            return
        except sr.RequestError as e:
            logging.error(f"❌ Ошибка сервиса Google: {e}")
            await message.reply("⚠️ Сервис распознавания временно недоступен.")
            return

        for path in [voice_file, wav_path]:
            if os.path.exists(path):
                os.remove(path)

        phone = USER_PHONE_MAP.get(user_id)
        if not phone:
            await message.reply("📱 Сначала привяжите номер через /start")
            return

        # === 🕐 ПОКАЗЫВАЕМ АНИМАЦИЮ ОЖИДАНИЯ ===
        status_msg = await message.reply("⏳ Обработка может занять несколько секунд...")
        animation_task = asyncio.create_task(animate_waiting(status_msg))

        try:
            ai_result = await parse_order_with_openrouter(text, menu_items=MENU_ITEMS, delivery_zones=DELIVERY_ZONES)
        except Exception as e:
            ai_result = None
            logging.error(f"❌ Ошибка при вызове OpenRouter: {e}")
        finally:
            # Останавливаем анимацию
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        if not ai_result:
            try:
                await status_msg.edit_text("❌ Не удалось распознать заказ. Попробуйте снова.")
            except:
                pass
            return

        logging.info(f"🧠 AI результат: {ai_result}")

        # === Обработка позиций ===
        items = []
        total_menu_price = 0
        unrecognized = []

        for item in ai_result.get("items", []):
            name = item["name"].strip()
            qty = item["qty"]

            matched_item = find_menu_item_fuzzy(name)
            if matched_item:
                source_price = matched_item["price"]
                total_menu_price += source_price * qty
                items.append({
                    "name": matched_item["name"],
                    "qty": qty,
                    "comment": item.get("comment", ""),
                    "source_price": source_price
                })
            else:
                unrecognized.append(name)

        if unrecognized:
            await status_msg.edit_text(f"⚠️ Эти блюда не найдены в меню: {', '.join(unrecognized)}")
            return

        if not items:
            await status_msg.edit_text("❌ Не распознано ни одного блюда из меню.")
            return

        # === Телефон ===
        result_phone = ai_result.get("phone") or phone
        if not result_phone.startswith("+7") and result_phone.startswith("7"):
            result_phone = "+" + result_phone
        elif not result_phone.startswith("+7"):
            result_phone = "+7" + result_phone[-10:]

        # === Адрес ===
        address_input = ai_result.get("address")
        full_address = ""
        if isinstance(address_input, dict):
            street = address_input.get("street", "").strip()
            house = address_input.get("house", "").strip()
            apartment = address_input.get("apartment", "").strip()
            full_address = f"{street} {house}".strip()
            if apartment:
                full_address += f" кв. {apartment}"
        else:
            full_address = str(address_input).strip() if address_input else ""

        is_self_pickup = any(kw in full_address.lower() for kw in ["самовывоз", "лично", "заберу"]) if full_address else False

        # === Зона доставки ===
        matches = []
        delivery_zone = "Самовывоз"
        delivery_price = 0

        if full_address and not is_self_pickup:
            matches = find_delivery_zone_by_address(full_address)
            if matches:
                zone, price, _ = matches[0]
                delivery_zone = zone
                delivery_price = price
            else:
                delivery_zone = "Самовывоз"
                delivery_price = 0
        else:
            full_address = "Самовывоз"

        # === Время и дата ===
        time_guess = ai_result.get("time", "")
        delivery_date = ai_result.get("delivery_date") or parse_delivery_date(time_guess)
        if not delivery_date:
            delivery_date = datetime.now().strftime("%d.%m.%Y")

        # === Сохраняем состояние ===
        order_id = generate_order_id()
        initialize_user_state(order_id)

        saved_order = {
            "id": order_id,
            "items": items,
            "phone": result_phone,
            "address": full_address,
            "time": time_guess,
            "delivery_date": delivery_date,
            "delivery_zone": delivery_zone,
            "delivery_price": delivery_price,
            "comment": ai_result.get("comment", ""),
            "total": total_menu_price + delivery_price,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }

        state = ORDER_STATE[order_id]
        state.update({
            "items": items,
            "phone": result_phone,
            "address": full_address,
            "time": time_guess,
            "delivery_date": delivery_date,
            "delivery_zone": delivery_zone,
            "delivery_price": delivery_price,
            "status": "not_accepted"
        })

        add_pending_order(saved_order)
        logging.info(f"✅ Заказ добавлен в pending: ID={order_id}")

        # === РЕДАКТИРУЕМ СООБЩЕНИЕ НА ЧЕК ===
        await update_message_to_order_check(order_id, status_msg)

    except Exception as e:
        logging.error(f"❌ Критическая ошибка при обработке голоса: {e}", exc_info=True)
        await message.reply("⚠️ Произошла ошибка при обработке голосового сообщения.")

async def parse_order_with_openrouter(text: str, menu_items=None, delivery_zones=None):
    """
    Парсит текст заказа через OpenRouter с актуальным меню и зонами.
    """
    if menu_items is None:
        menu_items = []
    if delivery_zones is None:
        delivery_zones = {}

    # ✅ Используем ПАРАМЕТР, а не глобальную переменную
    menu_lines = "\n".join([
        f"- {item['name']} — {item['price']} ₽"
        for item in menu_items
    ]) if menu_items else "Меню не загружено."


    # Аналогично для зон доставки (если используете delivery_zones)
    zones_text = ""
    if delivery_zones:
        zones_text = "\n".join([
            f"{zone} → {price} ₽"
            for zone, price in delivery_zones.items()
        ])
    else:
        try:
            df = pd.read_excel("adress.xlsx")
            street_col = next((col for col in df.columns if "street" in col.lower()), "street")
            zone_col = next((col for col in df.columns if "zone" in col.lower()), "zone")
            price_col = next((col for col in df.columns if "price" in col.lower()), "price")

            zones_text = "\n".join([
                f"{row[street_col]} → {row[zone_col]} ({int(row[price_col])} ₽)"
                for _, row in df.iterrows() if pd.notna(row[price_col])
            ])
        except Exception as e:
            logging.error(f"❌ Ошибка чтения adress.xlsx: {e}")
            zones_text = "Не загружено"
        menu_lines = "\n".join([f"- {item['name']} — {item['price']} ₽" for item in menu_items])
        zones_text = "\n".join([f"- {zone} → {price} ₽" for zone, price in DELIVERY_ZONES.items()])

    prompt = f"""
Ты — ассистент пиццерии. Проанализируй текст заказа и верни строго JSON.
Сегодня {datetime.now().strftime('%d.%m.%Y')}.

### Меню:
{menu_lines}

### Зоны доставки:
{zones_text}

### Текст заказа:

{text}

Правила:
- Если количество не указано — ставь 1.
- Адрес разбивай: улица, дом, корпус, подъезд, этаж, квартира.
- Телефон нормализуй: +7XXXXXXXXXX.
- Время доставки: если не указано — ближайшее возможное.
- Комментарий — всё, что не попало в другие поля.
- Сумма: суммируй цены из меню.
- Позиции: только те, что есть в меню. Неизвестные — в комментарий.

Верни только JSON:
{{
  "items": [{{"name": "Пицца", "qty": 2, "source_price": 300}}],
  "address": {{"street": "Ленина", "house": "10"}},
  "phone": "+79991234567",
  "time": "13:30",
  "delivery_date": "05.04.2025",
  "comment": "",
  "total": 600
}}
"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": f"{OPENROUTER_MODEL}",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1024
    }
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=15
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()

        logging.info("📥 Ответ от OpenRouter:")
        logging.info(content)

        # 🔁 Заменяем JavaScript-значения на Python-совместимые
        content = re.sub(r'\bnull\b', 'None', content, flags=re.IGNORECASE)
        content = re.sub(r'\btrue\b', 'True', content, flags=re.IGNORECASE)
        content = re.sub(r'\bfalse\b', 'False', content, flags=re.IGNORECASE)

        # Ищем JSON
        json_start = content.find("{")
        json_end = content.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            raise ValueError("JSON не найден в ответе")

        parsed_json = ast.literal_eval(content[json_start:json_end])
        return parsed_json

    except Exception as e:
        logging.error(f"❌ Ошибка при обращении к OpenRouter: {e}")
        return None

def load_pending_orders():
    """Загружает заказы из pending_orders.json"""
    if os.path.exists(PENDING_ORDERS_JSON):
        try:
            with open(PENDING_ORDERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logging.error(f"❌ Ошибка чтения pending_orders.json: {e}")
    return {}

def save_pending_orders(orders):
    """Сохраняет заказы в pending_orders.json"""
    try:
        with open(PENDING_ORDERS_JSON, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"❌ Ошибка записи pending_orders.json: {e}")

def add_pending_order(new_order):
    """Добавляет заказ в pending_orders.json (формат: dict с id как ключом)"""
    if not isinstance(new_order, dict):
        logging.error(f"❌ add_pending_order: ожидался dict, получено {type(new_order)}")
        return

    orders = load_pending_orders()  # Должен вернуть dict
    order_id = str(new_order.get("id"))

    if not order_id:
        order_id = str(int(datetime.now().timestamp()))
        new_order["id"] = order_id

    # Обновляем/добавляем запись
    orders[order_id] = new_order
    save_pending_orders(orders)
    logging.info(f"✅ Добавлен в ожидание: ID={order_id}")

def update_pending_order_in_file(order_id, state):
    """Обновляет заказ в pending_orders.json (формат: dict)"""
    orders = load_pending_orders()
    order_key = str(order_id)

    if order_key not in orders:
        logging.warning(f"❌ Не найден заказ для обновления: {order_id}")
        return

    # Обновляем поля
    orders[order_key].update({
        "items": state["items"],
        "phone": state["phone"],
        "address": state["address"],
        "time": state["time"],
        "delivery_date": state["delivery_date"],
        "delivery_zone": state["delivery_zone"],
        "delivery_price": state["delivery_price"],
        "total": calculate_total(state["items"], state["delivery_price"]),
        "status": "pending"
    })

    save_pending_orders(orders)
    logging.info(f"🔄 Обновлён заказ в pending_orders.json: {order_id}")

def save_active_orders(orders):
    with open(ACTIVE_ORDERS_JSON,"w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

def load_active_orders():
    """Загружает список активных заказов. Если файла нет или он повреждён — возвращает пустой список."""
    if not os.path.exists(ACTIVE_ORDERS_JSON):
        logging.warning(f"⚠️ Файл {ACTIVE_ORDERS_JSON} не найден. Создаётся новый.")
        return []

    try:
        with open(ACTIVE_ORDERS_JSON, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logging.warning(f"⚠️ Файл {ACTIVE_ORDERS_JSON} пуст. Возвращаем пустой список.")
                return []
            return json.loads(content)
    except json.JSONDecodeError as e:
        logging.error(f"❌ Ошибка парсинга {ACTIVE_ORDERS_JSON}: {e}")
        logging.info("🔄 Создаём новый пустой файл...")
        save_active_orders([])
        return []
    except Exception as e:
        logging.error(f"❌ Неожиданная ошибка при загрузке {ACTIVE_ORDERS_JSON}: {e}")
        return []


def add_active_order(new_order):
    """Добавляет один заказ в active_orders.json"""
    if not isinstance(new_order, dict):
        logging.error(f"❌ add_active_order: ожидался dict, получено {type(new_order)}")
        return

    orders = load_active_orders()

    # Генерируем ID, если его нет
    order_id = new_order.get("id") or int(datetime.now().timestamp())
    new_order["id"] = order_id

    orders.append(new_order)

    with open(ACTIVE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    logging.info(f"✅ Добавлен активный заказ: ID={order_id}")


def load_future_orders():
    """Загружает список будущих заказов. Если файла нет или он повреждён — возвращает пустой список."""
    if not os.path.exists(FUTURE_ORDERS_JSON):
        logging.warning(f"⚠️ Файл {FUTURE_ORDERS_JSON} не найден. Создаётся новый.")
        return []

    try:
        with open(FUTURE_ORDERS_JSON, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                logging.warning(f"⚠️ Файл {FUTURE_ORDERS_JSON} пуст. Возвращаем пустой список.")
                return []
            return json.loads(content)
    except json.JSONDecodeError as e:
        logging.error(f"❌ Ошибка парсинга {FUTURE_ORDERS_JSON}: {e}")
        logging.info("🔄 Создаём новый пустой файл...")
        save_future_orders([])
        return []
    except Exception as e:
        logging.error(f"❌ Неожиданная ошибка при загрузке {FUTURE_ORDERS_JSON}: {e}")
        return []

def save_future_orders(orders):
    """Сохраняет список будущих заказов"""
    with open(FUTURE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

def add_future_order(new_order):
    """Добавляет один заказ в future_orders.json"""
    if not isinstance(new_order, dict):
        logging.error(f"❌ add_future_order: ожидался dict, получено {type(new_order)}")
        return

    orders = load_future_orders()

    order_id = new_order.get("id") or int(datetime.now().timestamp())
    new_order["id"] = order_id

    orders.append(new_order)

    with open(FUTURE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    logging.info(f"✅ Добавлен будущий заказ: ID={order_id}, дата={new_order.get('delivery_date')}")

# --- Глобальные переменные ---
MENU_ITEMS = []
MENU_NAMES = []
DELIVERY_ZONES = {}  # { "район": цена }
STREET_NAMES = []    # Список чистых названий улиц из базы
ORDER_STATE = {}  # Храним заказы по уникальному order_id
current_order_id = 0  # Счётчик для генерации ID
CATEGORIES = []


@bot_app.on_message(filters.command("id"))
async def get_thread_id(client, message):
    thread_id = message.reply_to_message_id
    await message.reply(f"🧵 `message_thread_id` = `{thread_id}`")

def generate_order_id():
    global current_order_id
    current_order_id += 1
    return f"order_{int(time.time())}_{current_order_id}"

def load_menu():
    global MENU_ITEMS
    try:
        df = pd.read_excel(MENU_XLSX)

        # Автоопределение нужных столбцов
        name_col = next((col for col in df.columns if "name" in col.lower() or "название" in col.lower()), "name")
        price_col = next((col for col in df.columns if "price" in col.lower() or "цена" in col.lower()), "price")
        category_col = next((col for col in df.columns if "category" in col.lower() or "категория" in col.lower() or "раздел" in col.lower()), "category")

        # Переименовываем столбцы
        df = df.rename(columns={
            name_col: "name",
            price_col: "price",
            category_col: "category"
        })

        # Проверка столбцов
        if "name" not in df.columns:
            raise KeyError("Столбец 'name' не найден в menu.xlsx")
        if "price" not in df.columns:
            raise KeyError("Столбец 'price' не найден в menu.xlsx")
        if "category" not in df.columns:
            raise KeyError("Столбец 'category' (или аналог) не найден в menu.xlsx")

        # Приведение типов
        df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0).astype(int)
        df = df.dropna(subset=["name", "category"])
        df = df[df["name"].astype(str).str.strip() != ""]

        # Преобразуем в список словарей и добавляем id
        MENU_ITEMS = df[["name", "price", "category"]].to_dict('records')
        for idx, item in enumerate(MENU_ITEMS):
            item["id"] = idx  # ✅ Добавляем уникальный числовой ID

        logging.info(f"✅ Меню загружено: {len(MENU_ITEMS)} позиций")

        # Обновляем CATEGORIES
        global CATEGORIES
        CATEGORIES = sorted(list(set(item["category"] for item in MENU_ITEMS)))
        logging.info(f"📋 Категории: {CATEGORIES}")

    except Exception as e:
        logging.error(f"❌ Ошибка загрузки меню: {e}")
        MENU_ITEMS = []
        CATEGORIES = []

def load_delivery_zones():
    """Загружает зоны доставки из adress.xlsx"""
    global DELIVERY_ZONES
    if not os.path.exists(ADDRESS_XLSX):
        logging.warning(f"Файл зон доставки {ADDRESS_XLSX} не найден.")
        return {}

    try:
        df = pd.read_excel(ADDRESS_XLSX)
        street_col = next((col for col in df.columns if "street" in col.lower()), "street")
        zone_col = next((col for col in df.columns if "zone" in col.lower() or "район" in col.lower()), "zone")
        price_col = next((col for col in df.columns if "price" in col.lower() or "цена" in col.lower()), "price")

        zones = {}
        for _, row in df.iterrows():
            zone = str(row[zone_col]).strip()
            price = row[price_col]
            price = int(price) if pd.notna(price) else 0
            zones[zone] = price

        DELIVERY_ZONES = zones
        logging.info(f"✅ Загружено {len(zones)} зон доставки.")
        load_street_names()  # ← Добавлено!
        return zones
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки зон доставки: {e}")
        return {}

def load_street_names():
    """Загружает и нормализует список улиц из adress.xlsx"""
    global STREET_NAMES
    if not os.path.exists(ADDRESS_XLSX):
        logging.warning(f"Файл {ADDRESS_XLSX} не найден.")
        return []

    try:
        df = pd.read_excel(ADDRESS_XLSX)
        street_col = next((col for col in df.columns if "street" in col.lower()), "street")

        streets = [clean_street_name(row[street_col]) for _, row in df.iterrows()]
        STREET_NAMES = list(set(street for street in streets if street))
        logging.info(f"✅ Загружено {len(STREET_NAMES)} уникальных названий улиц: {sorted(STREET_NAMES)[:10]}...")
        return STREET_NAMES
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки улиц: {e}")
        return []

@bot_app.on_message(filters.command("menu"))
async def show_admin_menu(client, message):
    if message.chat.id != WORK_GROUP:
        await message.reply("❌ Эта команда доступна только в рабочей группе.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Активные заказы", callback_data="admin_active_orders"),
         InlineKeyboardButton("📅 Заказы в будущем", callback_data="admin_future_orders")],
        [InlineKeyboardButton("💰 Зарплата", callback_data="admin_salary")],
        [InlineKeyboardButton("✅ Выполненные за сегодня", callback_data="admin_delivered_today")]  # ✅ Новая кнопка
    ])
    await message.reply_text("👨‍💼 <b>Админ-меню</b>\nВыберите действие:", reply_markup=keyboard)

def parse_delivery_date(time_text):
    """
    Парсит строку вроде 'Завтра 18:30' или '09.02 18:30' и возвращает дату.
    Возвращает строку в формате ДД.ММ.ГГГГ или None.
    """
    if not time_text:
        return None

    text = time_text.strip().lower()
    today = datetime.now()

    # Проверка: "завтра"
    if "завтра" in text:
        delivery_date = today + timedelta(days=1)
        return delivery_date.strftime("%d.%m.%Y")

    # Проверка: "сегодня"
    if "сегодня" in text:
        return today.strftime("%d.%m.%Y")

    # Проверка: дата в формате ДД.ММ или Д.ММ
    date_match = re.search(r'\b(\d{1,2})\.(\d{1,2})\b', text)
    if date_match:
        day, month = map(int, date_match.groups())
        year = today.year
        # Если месяц меньше текущего, возможно, это следующий год
        if month < today.month or (month == today.month and day < today.day):
            year += 1
        try:
            delivery_date = datetime(year, month, day)
            return delivery_date.strftime("%d.%m.%Y")
        except ValueError:
            return None

    # Проверка: название месяца
    months = {
        'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
        'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
        'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
    }
    for month_name, month_num in months.items():
        if month_name in text:
            day_match = re.search(r'\b(\d{1,2})\s+' + month_name, text)
            if day_match:
                day = int(day_match.group(1))
                year = today.year
                if month_num < today.month or (month_num == today.month and day < today.day):
                    year += 1
                try:
                    delivery_date = datetime(year, month_num, day)
                    return delivery_date.strftime("%d.%m.%Y")
                except ValueError:
                    pass

    return None  # Не удалось распознать дату


def calculate_total(items, delivery_price=0):
    """
    Считает общую сумму: блюда + доставка.
    """
    items_total = sum(
        it.get("source_price", next((i["price"] for i in MENU_ITEMS if i["name"].lower() == it["name"].lower()), 0)) * it["qty"]
        for it in items
    )
    return items_total + delivery_price  # ✅ Теперь доставка добавляется

def initialize_user_state(order_id):
    ORDER_STATE[order_id] = {
        "items": [],
        "time": None,
        "address": None,
        "phone": None,
        "delivery_zone": None,
        "delivery_price": 0,
        "delivery_matches": [],
        "order_message_id": None,
        "zone_selection_message_id": None,
        "delivery_date": None,
        "last_category": None,
        "temp_cart": [],
        "category_message_id": None,
        "awaiting": None,
        "awaiting_edit_order": False,  # ← новое состояние
        "status": "not_accepted",  # 🆕 Статус: не принят
        "admin_message_ids": []
    }

@bot_app.on_message(filters.command("pending"))
async def show_pending_orders(client, message):
    orders = load_pending_orders()
    if not orders:
        await message.reply("📭 Нет заказов в ожидании подтверждения.")
        return

    text = "<b>⏳ Заказы в ожидании:</b>\n\n"
    for order in orders:
        addr = order.get("address", "—")[:20]
        phone = order.get("phone", "—")
        total = order.get("total", 0)
        order_id = order.get("id", "—")
        text += f"🔹 <code>{order_id}</code> | {addr}... | {phone} | {total}₽\n"

    await message.reply(text)

@bot_app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    full_name = message.from_user.full_name

    # Проверяем, привязан ли номер
    phone = USER_PHONE_MAP.get(user_id)

    if phone:
        # === УЖЕ ПРИВЯЗАН → показываем инлайн-кнопку "Мои заказы" ===
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📦 Мои заказы", callback_data="my_orders")],
                # Опционально: кнопка для смены номера
                # [InlineKeyboardButton("📞 Сменить номер", callback_data="change_phone")]
            ]
        )
        welcome_text = (
            f"👋 Привет, {full_name}!\n\n"
            f"📱 Ваш номер <b>уже привязан</b>: <code>{phone}</code>\n\n"
            "Нажмите кнопку ниже, чтобы посмотреть свои заказы."
        )
    else:
        # === НЕ ПРИВЯЗАН → просим привязать номер ===
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📞 Привязать номер", callback_data="bind_phone")]
            ]
        )
        welcome_text = (
            f"👋 Привет, {full_name}!\n\n"
            "Я — бот для оформления заказов.\n\n"
            "Чтобы делать заказы и просматривать историю — "
            "<b>привяжите свой номер телефона</b>."
        )

    await message.reply_text(welcome_text, reply_markup=keyboard)

@bot_app.on_message(filters.private & filters.text & filters.regex(r"^📞 Привязать номер"))
async def prompt_for_phone(client, message):
    await message.reply_text(
        "📱 Отправьте ваш номер в формате:\n"
        "<code>79991234567</code>"
    )

@bot_app.on_callback_query(filters.regex("my_orders"))
async def my_orders_handler(client, callback):
    user_id = callback.from_user.id
    phone = USER_PHONE_MAP.get(user_id)

    if not phone:
        await callback.answer("❌ Сначала привяжите номер телефона.", show_alert=True)
        return

    # Нормализуем телефон
    normalized_phone = phone.replace("+", "")

    # Загружаем все заказы
    pending_orders = load_pending_orders().values()
    active_orders = load_active_orders()
    future_orders = load_future_orders()

    all_orders = list(pending_orders) + active_orders + future_orders
    user_orders = [
        order for order in all_orders
        if isinstance(order, dict) and order.get("phone", "").replace("+", "") == normalized_phone
    ]

    if not user_orders:
        await callback.answer("📦 У вас пока нет заказов.", show_alert=True)
        return

    # Сортируем: новые — сверху
    user_orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    # Отправляем каждый заказ отдельным сообщением
    for order in user_orders[:5]:  # только последние 5
        status_emoji = "✅" if order["status"] in ["done", "delivered", "accepted"] else "⏳"
        delivery_date = order.get("delivery_date", "—")
        time_str = order.get("time", "По готовности")

        text = (
            f"{status_emoji} <b>Заказ #{order['id']}</b>\n"
            f"📅 <b>Дата:</b> {delivery_date}\n"
            f"⏰ <b>Время:</b> {time_str}\n"
            f"📍 <b>Адрес:</b> {order.get('address', '—')}\n"
            f"💰 <b>Сумма:</b> {order['total']} ₽\n"
        )
        if order.get("comment"):
            text += f"❗<i>{order['comment']}</i>\n"

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👁️ Посмотреть", callback_data=f"view_order:{order['id']}")],
                [InlineKeyboardButton("🔁 Повторить", callback_data=f"repeat_order:{order['id']}")]
            ]
        )

        try:
            await callback.message.reply_text(text, reply_markup=keyboard)
        except Exception as e:
            logging.error(f"❌ Не удалось отправить заказ {order['id']}: {e}")

    # Подтверждаем нажатие
    await callback.answer()

@bot_app.on_callback_query(filters.regex(r"^view_order:"))
async def view_order_handler(client, callback):
    order_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    phone = USER_PHONE_MAP.get(user_id)

    if not phone:
        await callback.answer("❌ Авторизуйтесь через /start", show_alert=True)
        return

    # Поиск заказа
    orders = (
            list(load_pending_orders().values()) +
            load_active_orders() +
            load_future_orders()
    )
    order = next((o for o in orders if str(o.get("id")) == order_id), None)

    if not order:
        await callback.answer("❌ Заказ не найден.")
        return

    # Формируем детали
    items_text = "\n".join(
        [f"• {it['qty']}x {it['name']} — {it.get('source_price', 0) * it['qty']}₽"
         for it in order.get("items", [])]
    )
    address = order.get("address") or "—"
    delivery_cost = order.get("delivery_price", 0)

    text = f"""
📋 <b>Детали заказа #{order_id}</b>

📞 <b>Телефон:</b> <code>{order.get('phone', '—')}</code>
🏠 <b>Адрес:</b> {address}
⏰ <b>Время:</b> {order.get('time', '—')}
📅 <b>Дата доставки:</b> {order.get('delivery_date', '—')}

🍣 <b>Состав:</b>
{items_text}

🚚 <b>Доставка:</b> {delivery_cost} ₽
💰 <b>Итого:</b> <b>{order['total']} ₽</b>
""".strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Повторить", callback_data=f"repeat_order:{order_id}")],
        [InlineKeyboardButton("⬅️ Назад к моим заказам", callback_data="my_orders")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@bot_app.on_callback_query(filters.regex(r"^repeat_order:"))
async def repeat_order_handler(client, callback):
    order_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    phone = USER_PHONE_MAP.get(user_id)

    if not phone:
        await callback.answer("❌ Авторизуйтесь через /start", show_alert=True)
        return

    # Поиск оригинального заказа
    orders = (
            list(load_pending_orders().values()) +
            load_active_orders() +
            load_future_orders()
    )
    original_order = next((o for o in orders if str(o.get("id")) == order_id), None)

    if not original_order:
        await callback.answer("❌ Исходный заказ не найден.")
        return

    # Генерируем новый ID
    new_order_id = generate_order_id()
    initialize_user_state(new_order_id)

    # Создаём копию заказа с обновлёнными полями
    now = datetime.now()
    saved_order = {
        "id": new_order_id,
        "items": original_order["items"],
        "phone": phone,
        "address": original_order.get("address"),
        "time": original_order.get("time"),
        "delivery_date": now.strftime("%d.%m.%Y"),  # На сегодня
        "delivery_zone": original_order.get("delivery_zone"),
        "delivery_price": original_order.get("delivery_price", 0),
        "comment": original_order.get("comment", ""),
        "total": calculate_total(original_order["items"], original_order.get("delivery_price", 0)),
        "status": "pending",
        "created_at": now.isoformat()
    }

    # Сохраняем в pending
    add_pending_order(saved_order)

    # Обновляем состояние
    state = ORDER_STATE[new_order_id]
    state.update({
        "items": original_order["items"],
        "phone": phone,
        "address": original_order.get("address"),
        "time": original_order.get("time"),
        "delivery_date": now.strftime("%d.%m.%Y"),
        "delivery_zone": original_order.get("delivery_zone"),
        "delivery_price": original_order.get("delivery_price", 0),
        "status": "not_accepted"
    })

    # Отправляем чек
    await update_message_to_order_check(new_order_id, callback.message)

    # Уведомление
    await callback.answer("✅ Заказ повторён! Оформите его ниже.")

@bot_app.on_message(filters.private & filters.text & ~filters.command([]))
async def handle_private_text(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    # === ПРОВЕРКА: может, это номер телефона для привязки? ===
    phone_match = re.fullmatch(r"7\d{10}", text)
    if phone_match and user_id not in USER_PHONE_MAP:
        USER_PHONE_MAP[user_id] = "+" + text
        save_user_phones()  # Сохраняем в файл
        await message.reply("✅ Номер телефона успешно привязан!\nТеперь вы можете делать заказы.")
        return

    phone = USER_PHONE_MAP.get(user_id)
    if not phone:
        await message.reply("📱 Сначала привяжите номер через /start")
        return

    logging.info(f"📩 Личка | Получен заказ от {user_id}: '{text[:50]}'")

    # === 🕐 АНИМАЦИЯ ОЖИДАНИЯ ===
    status_msg = await message.reply("⏳ Обработка может занять несколько секунд...")
    animation_task = asyncio.create_task(animate_waiting(status_msg))

    try:
        ai_result = await parse_order_with_openrouter(text, menu_items=MENU_ITEMS, delivery_zones=DELIVERY_ZONES)
    except Exception as e:
        ai_result = None
        logging.error(f"❌ Ошибка при вызове OpenRouter: {e}")
    finally:
        animation_task.cancel()
        try:
            await animation_task
        except asyncio.CancelledError:
            pass

    if not ai_result:
        try:
            await status_msg.edit_text("❌ Не удалось распознать заказ. Попробуйте снова.")
        except:
            pass
        return

    # === Обработка позиций ===
    items = []
    unrecognized = []

    for item in ai_result.get("items", []):
        name = item["name"].strip()
        qty = item["qty"]

        matched_item = find_menu_item_fuzzy(name)
        if matched_item:
            items.append({
                "name": matched_item["name"],
                "qty": qty,
                "comment": item.get("comment", ""),
                "source_price": matched_item["price"]
            })
        else:
            unrecognized.append(name)

    if unrecognized:
        await status_msg.edit_text(f"⚠️ Эти блюда не найдены в меню: {', '.join(unrecognized)}")
        return

    if not items:
        await status_msg.edit_text("❌ Не распознано ни одного блюда из меню.")
        return

    # === Телефон ===
    result_phone = ai_result.get("phone") or phone
    if not result_phone.startswith("+7") and result_phone.startswith("7"):
        result_phone = "+" + result_phone
    elif not result_phone.startswith("+7"):
        result_phone = "+7" + result_phone[-10:]

    # === Адрес ===
    address_input = ai_result.get("address")
    full_address = ""
    if isinstance(address_input, dict):
        street = address_input.get("street", "").strip()
        house = address_input.get("house", "").strip()
        apartment = address_input.get("apartment", "").strip()
        full_address = f"{street} {house}".strip()
        if apartment:
            full_address += f" кв. {apartment}"
    else:
        full_address = str(address_input).strip() if address_input else ""

    is_self_pickup = any(kw in full_address.lower() for kw in ["самовывоз", "лично", "заберу"]) if full_address else False

    # === Зона доставки ===
    matches = []
    delivery_zone = "Самовывоз"
    delivery_price = 0

    if full_address and not is_self_pickup:
        matches = find_delivery_zone_by_address(full_address)
        if matches:
            zone, price, _ = matches[0]
            delivery_zone = zone
            delivery_price = price
        else:
            delivery_zone = "Самовывоз"
            delivery_price = 0
    else:
        full_address = "Самовывоз"

    # === Время и дата ===
    time_guess = ai_result.get("time", "")
    delivery_date = ai_result.get("delivery_date") or parse_delivery_date(time_guess)
    if not delivery_date:
        delivery_date = datetime.now().strftime("%d.%m.%Y")

    # === Сохраняем состояние ===
    order_id = generate_order_id()
    initialize_user_state(order_id)

    saved_order = {
        "id": order_id,
        "items": items,
        "phone": result_phone,
        "address": full_address,
        "time": time_guess,
        "delivery_date": delivery_date,
        "delivery_zone": delivery_zone,
        "delivery_price": delivery_price,
        "comment": ai_result.get("comment", ""),
        "total": calculate_total(items, delivery_price),
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }

    state = ORDER_STATE[order_id]
    state.update({
        "items": items,
        "phone": result_phone,
        "address": full_address,
        "time": time_guess,
        "delivery_date": delivery_date,
        "delivery_zone": delivery_zone,
        "delivery_price": delivery_price,
        "status": "not_accepted"
    })

    add_pending_order(saved_order)
    logging.info(f"✅ Заказ добавлен в pending: ID={order_id}")

    # === РЕДАКТИРУЕМ СООБЩЕНИЕ НА ЧЕК ===
    await update_message_to_order_check(order_id, status_msg)


@bot_app.on_message(filters.text & filters.chat(WORK_GROUP))
async def handle_order(client, message):
    thread_id = message.message_thread_id or (
            message.reply_to_message and message.reply_to_message.message_thread_id
    ) if message.reply_to_message else None
    if THREAD_ORDER_ID and thread_id != THREAD_ORDER_ID:
        return

    text = message.text.strip()
    order_id = None

    global awaiting_edit_from_message

    # === РЕЖИМ РЕДАКТИРОВАНИЯ ===
    if awaiting_edit_from_message:
        order_id = awaiting_edit_from_message
        awaiting_edit_from_message = None

        state = ORDER_STATE.get(order_id)
        if not state:
            logging.warning(f"❌ Ожидалось редактирование order_id={order_id}, но состояние не найдено")
            return

        state["awaiting_edit_order"] = True
        logging.info(f"📩 Режим редактирования активирован для {order_id}")

    else:
        # Поиск по reply_to_message_id
        for oid, state in ORDER_STATE.items():
            if state.get("order_message_id") == message.reply_to_message_id:
                order_id = oid
                break

        if order_id is None:
            order_id = generate_order_id()
            initialize_user_state(order_id)
            logging.info(f"🆕 Создан новый заказ: {order_id}")
        else:
            logging.info(f"🔄 Найден существующий заказ: {order_id}")
            pending_orders = load_pending_orders()
            order_data = next((o for o in pending_orders if str(o.get("id")) == order_id), None)
            if order_data and order_id not in ORDER_STATE:
                initialize_user_state(order_id)
                state = ORDER_STATE[order_id]
                state.update({
                    "items": order_data["items"],
                    "phone": order_data["phone"],
                    "address": order_data["address"],
                    "time": order_data["time"],
                    "delivery_date": order_data["delivery_date"],
                    "delivery_zone": order_data["delivery_zone"],
                    "delivery_price": order_data["delivery_price"],
                    "status": "not_accepted"
                })
                logging.info(f"📥 Состояние восстановлено из pending_orders.json")

    state = ORDER_STATE[order_id]
    logging.info(f"📩 Сообщение от {message.from_user.id}: '{text[:50]}'")

    # === РЕДАКТИРОВАНИЕ ЧЕРЕЗ ИИ ===
    if state.get("awaiting_edit_order"):
        state["awaiting_edit_order"] = False

        ai_result = await parse_order_with_openrouter(text, menu_items=MENU_ITEMS, delivery_zones=DELIVERY_ZONES)
        if not ai_result:
            await message.reply("❌ Не удалось распознать изменения. Проверьте формат.")
            return

        # Обновляем: телефон, время, дата
        if ai_result.get("phone"):
            state["phone"] = ai_result["phone"]
        if ai_result.get("time"):
            state["time"] = ai_result["time"]
            state["delivery_date"] = ai_result.get("delivery_date") or parse_delivery_date(ai_result["time"])

        # Адрес
        address_input = ai_result.get("address")
        if address_input:
            if isinstance(address_input, dict):
                street = address_input.get("street", "")
                house = address_input.get("house", "")
                full_address = f"{street} {house}".strip() if isinstance(address_input, dict) else str(address_input).strip()

                state["address"] = full_address
            else:
                full_address = str(address_input).strip()

            state["address"] = full_address
            state["original_address"] = full_address

            matches = find_delivery_zone_by_address(full_address)
            state["delivery_matches"] = matches

            if not matches:
                state["delivery_zone"] = "Самовывоз"
                state["delivery_price"] = 0
                await message.reply("⚠️ Адрес не найден → Самовывоз")
            elif len(matches) == 1:
                zone, price, _ = matches[0]
                state["delivery_zone"] = zone
                state["delivery_price"] = price
                await message.reply(f"🏠 Адрес и зона обновлены: {zone} (+{price} ₽)")
            else:
                await show_zone_selection(message, matches, order_id)
                return

        # Добавляем блюда
        items = []
        unrecognized = []

        for item in ai_result.get("items", []):
            name = item["name"].strip()
            qty = item["qty"]

            matched_item = find_menu_item_fuzzy(name)
            if matched_item:
                items.append({
                    "name": matched_item["name"],
                    "qty": qty,
                    "comment": item.get("comment", ""),
                    "source_price": matched_item["price"]
                })
            else:
                unrecognized.append(name)

        if unrecognized:
            await message.reply(f"⚠️ Эти блюда не найдены в меню: {', '.join(unrecognized)}")

        existing_items = state["items"]
        for new_item in items:
            existing = next((it for it in existing_items if it["name"] == new_item["name"]), None)
            if existing:
                existing["qty"] += new_item["qty"]
                if new_item["comment"]:
                    existing["comment"] = new_item["comment"]
            else:
                existing_items.append(new_item)

        update_pending_order_in_file(order_id, state)
        await update_order_message(order_id)
        return

    # === ВНЕШНИЙ ЗАКАЗ my2can ИЛИ ЛЮБОЙ ТЕКСТ → ЕДИНЫЙ ИИ-ПАРСИНГ ===
    ai_result = await parse_order_with_openrouter(text, menu_items=MENU_ITEMS, delivery_zones=DELIVERY_ZONES)
    if not ai_result:
        await message.reply("❌ Не удалось распознать заказ. Проверьте текст или отправьте снова.")
        return

    # Извлекаем данные
    items = []
    unrecognized = []

    for item in ai_result.get("items", []):
        name = item["name"].strip()
        qty = item["qty"]

        matched_item = find_menu_item_fuzzy(name)
        if matched_item:
            items.append({
                "name": matched_item["name"],
                "qty": qty,
                "comment": item.get("comment", ""),
                "source_price": matched_item["price"]
            })
        else:
            unrecognized.append(name)

    if unrecognized:
        await message.reply(f"⚠️ Эти блюда не найдены в меню: {', '.join(unrecognized)}")

    if not items:
        await message.reply("❌ Не распознано ни одного блюда.")
        return

    phone = ai_result.get("phone")
    time_guess = ai_result.get("time")
    delivery_date = ai_result.get("delivery_date") or parse_delivery_date(time_guess)
    if not delivery_date:
        delivery_date = datetime.now().strftime("%d.%m.%Y")

    # Адрес
    address_input = ai_result.get("address")
    full_address = ""
    if address_input:
        if isinstance(address_input, dict):
            street = address_input.get("street", "")
            house = address_input.get("house", "")
            full_address = f"{street} {house}".strip()
        else:
            full_address = str(address_input).strip()

    # Самовывоз?
    is_self_pickup = any(kw in full_address.lower() for kw in ["самовывоз", "лично", "заберу"]) if full_address else False

    # Зона доставки
    matches = []
    delivery_zone = "Самовывоз"
    delivery_price = 0

    if full_address and not is_self_pickup:
        matches = find_delivery_zone_by_address(full_address)
        state["delivery_matches"] = matches
        if matches:
            zone, price, _ = matches[0]
            delivery_zone = zone
            delivery_price = price
        else:
            await message.reply("⚠️ Адрес не найден в базе → Самовывоз")
    else:
        full_address = "Самовывоз"

    # Обновляем состояние
    state.update({
        "items": items,
        "phone": phone,
        "address": full_address,
        "time": time_guess,
        "delivery_date": delivery_date,
        "delivery_zone": delivery_zone,
        "delivery_price": delivery_price,
        "status": "not_accepted"
    })

    # Сохраняем в pending_orders.json
    saved_order = {
        "id": order_id,
        "items": items,
        "phone": phone,
        "address": full_address,
        "time": time_guess,
        "delivery_date": delivery_date,
        "delivery_zone": delivery_zone,
        "delivery_price": delivery_price,
        "total": calculate_total(items, delivery_price),
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    add_pending_order(saved_order)

    # Отправляем чек
    await show_editable_order_inline(order_id, message)

async def show_zone_selection(message, matches, order_id):
    """Отправляет кнопки для выбора правильной зоны."""
    keyboard = []
    for i, (zone, price, street_db) in enumerate(matches):
        keyboard.append([InlineKeyboardButton(f"{zone} — {price} ₽ ({street_db})", callback_data=f"select_zone_{i}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await message.reply_text("📍 Найдено несколько зон доставки. Выберите подходящую:", reply_markup=reply_markup)

    # Сохраняем ID сообщения с выбором зоны
    user_id = message.from_user.id
    ORDER_STATE[order_id]["zone_selection_message_id"] = msg.id
    logging.info(f"📌 Сообщение с выбором зоны сохранено: {msg.id}")


async def show_editable_order_inline(order_id, message_or_callback):
    """Отправляет или редактирует сообщение с заказом. Кнопки зависят от контекста (личка / группа)."""
    state = ORDER_STATE.get(order_id)
    if not state:
        pending_orders = load_pending_orders()
        order_data = next((o for o in pending_orders if str(o.get("id")) == order_id), None)
        if not order_data:
            logging.warning(f"❌ Не найдено состояние для order_id={order_id}")
            return
        initialize_user_state(order_id)
        state = ORDER_STATE[order_id]
        state.update({
            "items": order_data["items"],
            "phone": order_data["phone"],
            "address": order_data["address"],
            "time": order_data["time"],
            "delivery_date": order_data["delivery_date"],
            "delivery_zone": order_data["delivery_zone"],
            "delivery_price": order_data["delivery_price"],
            "status": "not_accepted"
        })

    delivery_zone = state.get("delivery_zone")
    delivery_cost = state.get("delivery_price", 0)
    delivery_date = state.get("delivery_date")

    total = calculate_total(state["items"], delivery_price=delivery_cost)
    status_emoji = "⏳"
    order_text = (
            f"{status_emoji} <b>Заказ</b>\n"
            f"📞 Телефон: {state['phone'] or 'не указан'}\n"
            f"⏰ Время: {state['time'] or 'не указано'}\n"
            + (f"📅 Дата: {delivery_date}\n" if delivery_date else "")
            + f"🏠 Адрес: {state['address'] or 'не указан'}\n"
              f"📍 Зона: {delivery_zone if delivery_zone else 'Не определена'}\n"
              f"🚚 Доставка: {delivery_cost} ₽\n\n"
              f"🍣 Блюда:\n" + "\n".join([
        f"• {it['qty']}x {it['name']} — {it['qty'] * it.get('source_price', 0)} ₽"
        + (f"\n  ⚠️ {it['comment'].capitalize()}" if it['comment'] else "")
        for it in state["items"]
    ]) +
            f"\n\n💰 <b>Итого: {total} ₽</b>"
    )

    if isinstance(message_or_callback, CallbackQuery):
        chat_type = message_or_callback.message.chat.type
        from_what = "CallbackQuery"
    else:
        chat_type = message_or_callback.chat.type
        from_what = "Message"

    logging.info(f"🔍 Определение chat_type: {chat_type} (источник: {from_what})")
    logging.info(f"💬 Тип чата: {chat_type}, order_id={order_id}")

    # --- КНОПКИ: разные для лички и группы ---
    if chat_type == ChatType.PRIVATE:
        # 📱 В ЛИЧНЫХ СООБЩЕНИЯХ
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Отправить заказ", callback_data=f"send_to_group:{order_id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order:{order_id}")]
        ])
    else:
        # 🏢 В ГРУППЕ (рабочей)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"confirm_order:{order_id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order:{order_id}")]
        ])

    try:
        if isinstance(message_or_callback, CallbackQuery):
            msg = await message_or_callback.message.edit_text(order_text, reply_markup=keyboard)
        else:
            msg = await message_or_callback.reply_text(order_text, reply_markup=keyboard)
            state["order_message_id"] = msg.id
            ORDER_STATE[order_id]["order_message_id"] = msg.id
            logging.info(f"🔗 Привязан order_id={order_id} к message_id={msg.id}")
    except Exception as e:
        logging.error(f"Ошибка отправки чека: {e}")


def clean_street_name(s):
    """
    Единая функция очистки названия улицы.
    Оставляет только чистое название улицы без типа, номера дома, корпуса, квартиры и т.п.
    """
    if not isinstance(s, str):
        s = str(s)
    s = s.strip().lower()

    # Шаг 1: Заменяем типы улиц на пустоту
    s = re.sub(r'\b(?:ул\.?|улица|проспект|пр\.?|переулок|пер\.|набережная|шоссе|бульвар|аллея|площадь|пл\.?)\b', '', s)

    # Шаг 2: Удаляем все вхождения: дом, д., корпус, корп., кв., стр., уч., участок и т.п.
    s = re.sub(r'\b(?:дом|д\.?|кв\.?|квартира|корпус|корп\.?|строение|стр\.?|участок|уч\.?)\b', '', s)

    # Шаг 3: Удаляем номера: любые цифры, возможно с буквой, после которых нет букв (т.е. не часть слова)
    s = re.sub(r'\b\d+[\w]*\b', '', s)  # удаляет "3", "д.3", "д3", "100а" и т.п.

    # Шаг 4: Убираем лишние символы и пробелы
    s = re.sub(r'[^\w\s]', '', s)  # убираем запятые, точки, дефисы
    s = re.sub(r'\s+', ' ', s).strip()  # множественные пробелы → один

    return s


async def update_order_message(order_id):
    state = ORDER_STATE.get(order_id)
    if not state:
        return

    message_id = state.get("order_message_id")
    if not message_id:
        return

    delivery_zone = state.get("delivery_zone")
    delivery_cost = state.get("delivery_price", 0)
    delivery_date = state.get("delivery_date")

    total = calculate_total(state["items"], delivery_price=delivery_cost)
    status_emoji = "✅" if state.get("status") == "confirmed" else "⏳"
    order_text = (
            f"{status_emoji} <b>Заказ</b>\n"
            f"📞 Телефон: {state['phone'] or 'не указан'}\n"
            f"⏰ Время: {state['time'] or 'не указано'}\n"
            + (f"📅 Дата: {delivery_date}\n" if delivery_date else "")
            + f"🏠 Адрес: {state['address'] or 'не указан'}\n"
              f"📍 Зона: {delivery_zone if delivery_zone else 'Не определена'}\n"
              f"🚚 Доставка: {delivery_cost} ₽\n\n"
              f"🍣 Блюда:\n" + "\n".join([
        f"• {it['qty']}x {it['name']} — {it['qty'] * it.get('source_price', 0)} ₽"
        + (f"\n  ⚠️ {it['comment'].capitalize()}" if it['comment'] else "")
        for it in state["items"]
    ]) +
            f"\n\n💰 <b>Итого: {total} ₽</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖨️ Печать чека", callback_data=f"print:{order_id}")]
    ]) if state.get("status") == "confirmed" else InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Редактировать заказ", callback_data=f"edit_order:{order_id}")],
        [InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"confirm_order:{order_id}")]
    ])

    try:
        await bot_app.edit_message_text(
            chat_id=WORK_GROUP,
            message_id=message_id,
            text=order_text,
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при обновлении чека: {e}")


def check_files():
    for file_path in [ACTIVE_ORDERS_JSON, FUTURE_ORDERS_JSON, PENDING_ORDERS_JSON]:
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=4)
            logging.info(f"✅ Создан пустой файл: {file_path}")

def format_order_details(order):
    """
    Форматирует детали заказа для отображения в Telegram.
    """
    items_text = "\n".join(
        [f"• {item['qty']}x {item['name']} — {item.get('source_price', 0) * item['qty']}₽"
         for item in order.get("items", [])]
    )
    phone = order.get("phone") or "—"
    address = order.get("address") or "—"
    time_str = order.get("time") or "По готовности"
    delivery_date = order.get("delivery_date", "—")
    delivery_cost = order.get("delivery_price", 0)
    total = order.get("total", 0)

    lines = [
        f"📞 <b>Телефон:</b> <phone>{phone}</phone>",
        f"🏠 <b>Адрес:</b> {address.capitalize()}",
        f"⏰ <b>Время:</b> {time_str}",
        f"📅 <b>Дата доставки:</b> <b>{delivery_date}</b>",
        "",
        f"📋 <b>Состав заказа:</b>",
        items_text,
        "",
        f"🚚 <b>Доставка:</b> {delivery_cost}₽",
        f"💰 <b>Итого:</b> <b>{total}₽</b>"
    ]

    return "\n".join(lines)

def move_future_to_active():
    """Перемещает будущие заказы на сегодня в active_orders"""
    today = datetime.now().strftime("%d.%m.%Y")
    future_orders = load_future_orders()
    updated_futures = []

    moved_count = 0
    for order in future_orders:
        if order.get("delivery_date") == today:
            add_active_order(order)
            moved_count += 1
            logging.info(f"🔄 Перемещён в активные: {order['id']}")
        else:
            updated_futures.append(order)

    # Пересохраняем future_orders без сегодняшних
    with open(FUTURE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(updated_futures, f, ensure_ascii=False, indent=4)

    if moved_count:
        logging.info(f"✅ {moved_count} будущих заказов перемещено в активные")

@bot_app.on_callback_query()
async def handle_callback(client, callback):
    data = callback.data
    user_id = callback.from_user.id
    message = callback.message
    global awaiting_edit_from_message
    global ADMIN_MESSAGES

    logging.info(f"📥 Получен callback: '{data}' от {user_id}")

    if data == "admin_active_orders":

        # Удаляем предыдущие сообщения
        for msg_id in ADMIN_MESSAGES:
            try:
                await bot_app.delete_messages(callback.message.chat.id, msg_id)
                logging.info(f"🗑️ Удалено админ-сообщение: {msg_id}")
            except Exception as e:
                logging.warning(f"Не удалось удалить сообщение {msg_id}: {e}")
        ADMIN_MESSAGES.clear()

        active_orders = [
            o for o in load_active_orders()
            if o.get("status") not in ["delivered", "cancelled"]
        ]
        if not active_orders:
            await callback.answer("📭 Нет активных заказов")
            return

        for order in active_orders:
            order_id = order.get("id", "б/н")
            addr = order.get("address")
            if isinstance(addr, dict):
                street = addr.get("street", "").strip()
                house = addr.get("house", "").strip()
                addr = f"{street} {house}".strip()
            elif not addr:
                addr = "Самовывоз"
            else:
                addr = str(addr).strip()

            phone = order.get("phone") or "—"
            total = order.get("total", 0)
            time_order = (order.get("time") or "По готовности").strip()
            phone_last_4 = phone[-4:] if len(phone) >= 4 else "—"

            text = (
                f"📦 <b>Заказ #{order_id}</b>\n"
                f"⏰ <b>Время:</b> {time_order}\n"
                f"🏠 <b>Адрес:</b> {addr}\n"
                f"📞 <b>Тел:</b> ...{phone_last_4}\n"
                f"💰 <b>Сумма:</b> {total} ₽"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("👁️ Посмотреть", callback_data=f"view_active_order_{order_id}")],
                    [
                        InlineKeyboardButton("✅ Готов", callback_data=f"order_ready_{order_id}"),
                        InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_order:{order_id}")
                    ]
                ]
            )
            try:
                sent_msg = await bot_app.send_message(
                    chat_id=callback.message.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    message_thread_id=THREAD_NOW_ID
                )
                ADMIN_MESSAGES.append(sent_msg.id)
            except Exception as e:
                logging.error(f"❌ Ошибка отправки заказа {order_id}: {e}")

        await callback.answer()
        return

    elif data == "admin_future_orders":
        for msg_id in ADMIN_MESSAGES:
            try:
                await bot_app.delete_messages(callback.message.chat.id, msg_id)
                logging.info(f"🗑️ Удалено админ-сообщение: {msg_id}")
            except Exception as e:
                logging.warning(f"Не удалось удалить сообщение {msg_id}: {e}")
        ADMIN_MESSAGES.clear()

        future_orders = load_future_orders()
        if not future_orders:
            await callback.answer("📭 Нет будущих заказов")
            return

        keyboard = []
        today_str = datetime.now().strftime("%d.%m.%Y")
        for order in future_orders:
            delivery_date = order.get("delivery_date")
            if not delivery_date or delivery_date <= today_str:
                continue

            order_id = order.get("id", "б/н")
            addr = (order.get("address") or "Самовывоз")[:15].strip()
            phone = order.get("phone") or "—"
            date_str = delivery_date
            total = order.get("total", 0)
            phone_last_4 = phone[-4:] if len(phone) >= 4 else "—"
            btn_text = f"📅 {date_str} | {addr}... | {phone_last_4} | {total}₽"
            keyboard.append([
                InlineKeyboardButton(btn_text, callback_data=f"view_future_order_{order_id}")
            ])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
        try:
            sent_msg = await message.edit_text("<b>📅 Заказы в будущем</b>\nВыберите заказ:", reply_markup=InlineKeyboardMarkup(keyboard))
            ADMIN_MESSAGES.append(sent_msg.id)
        except Exception as e:
            logging.error(f"❌ Ошибка при редактировании: {e}")

        await callback.answer()
        return

    elif data == "admin_delivered_today":
        orders = load_active_orders()
        today_str = datetime.now().strftime("%d.%m.%Y")

        delivered_today = [
            o for o in orders
            if o.get("status") == "delivered"
               and (
                       o.get("delivery_date") == today_str
                       or o.get("delivery_date") is None  # если не указана — считаем как "сегодня"
               )
        ]

        if not delivered_today:
            await callback.answer("📭 Нет выполненных заказов за сегодня")
            return

        keyboard = []
        for order in delivered_today:
            order_id = order.get("id", "б/н")
            addr = (order.get("address") or "Самовывоз").strip()[:15]
            phone = order.get("phone") or "—"
            total = order.get("total", 0)
            phone_last_4 = phone[-4:] if len(phone) >= 4 else "—"
            btn_text = f"{addr}... | {phone_last_4} | {total}₽"
            keyboard.append([
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f"view_delivered_order_{order_id}"
                )
            ])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")])
        await message.edit_text("<b>✅ Выполненные заказы за сегодня</b>\nВыберите заказ для просмотра:", reply_markup=InlineKeyboardMarkup(keyboard))
        await callback.answer()

    elif data.startswith("view_active_order_"):
        order_id = data.replace("view_active_order_", "")
        order = next((o for o in load_active_orders() if str(o.get("id")) == order_id), None)
        if not order:
            await callback.answer("❌ Заказ не найден")
            return

        text = format_order_details(order)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готов", callback_data=f"order_ready_{order_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_active_orders")]
        ])
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return

    elif data == "admin_salary":
        active_orders = load_active_orders()
        future_orders = load_future_orders()
        today_str = datetime.now().strftime("%d.%m.%Y")
        today_future_orders = [o for o in future_orders if o.get("delivery_date") == today_str]

        # 💰 Все активные заказы (включая "готов", "в пути") + будущие на сегодня
        total_active = sum(o["total"] for o in active_orders)
        total_today_future = sum(o["total"] for o in today_future_orders)
        total_all = total_active + total_today_future

        # 🚚 Доход с доставки
        delivery_income = (
                sum(o.get("delivery_price", 0) for o in active_orders) +
                sum(o.get("delivery_price", 0) for o in today_future_orders)
        )

        # 🍣 Чистый доход с блюд (без учёта доставки)
        food_income = total_all - delivery_income

        # 📊 Статистика
        count_all = len(active_orders) + len(today_future_orders)
        avg_check = food_income // count_all if count_all else 0

        text = dedent(f"""
            <b>💰 Расчёт выручки (зарплата)</b>

            📦 Активные заказы: <b>{total_active:,} ₽</b>
            📅 Будущие заказы: <b>{total_today_future:,} ₽</b>

            🍣 <b>Выручка:</b> <code>{food_income:,} ₽</code>
            🚚 <b>Доставка:</b> <code>{delivery_income:,} ₽</code>
            
            📊 Средний чек (без доставки): <b>{avg_check:,} ₽</b>
            📌 Всего заказов: <b>{count_all}</b>

            💰 Зарплата: <b>{food_income / 8:.2f} ₽</b>
        """).strip()

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]])
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return

    elif data.startswith("send_to_group:"):
        order_id = data.replace("send_to_group:", "")
        state = ORDER_STATE.get(order_id)
        if not state:
            await callback.answer("❌ Заказ не найден.")
            return

        # Формируем текст чека
        delivery_zone = state.get("delivery_zone")
        delivery_cost = state.get("delivery_price", 0)
        delivery_date = state.get("delivery_date")

        total = calculate_total(state["items"], delivery_price=delivery_cost)
        status_emoji = "⏳"
        order_text = (
                f"{status_emoji} <b>Заказ</b>\n"
                f"📞 Телефон: {state['phone'] or 'не указан'}\n"
                f"⏰ Время: {state['time'] or 'не указано'}\n"
                + (f"📅 Дата: {delivery_date}\n" if delivery_date else "")
                + f"🏠 Адрес: {state['address'] or 'не указан'}\n"
                  f"📍 Зона: {delivery_zone if delivery_zone else 'Не определена'}\n"
                  f"🚚 Доставка: {delivery_cost} ₽\n\n"
                  f"🍣 Блюда:\n" + "\n".join([
            f"• {it['qty']}x {it['name']} — {it['qty'] * it.get('source_price', 0)} ₽"
            + (f"\n  ⚠️ {it['comment'].capitalize()}" if it['comment'] else "")
            for it in state["items"]
        ]) +
                f"\n\n💰 <b>Итого: {total} ₽</b>"
        )

        # Кнопки для группы
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"confirm_order:{order_id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order:{order_id}")]
        ])

        try:
            # Отправляем в группу
            sent_message = await bot_app.send_message(
                chat_id=WORK_GROUP,
                text=order_text,
                reply_markup=keyboard,
                message_thread_id=THREAD_ORDER_ID  # если нужно в тред
            )

            # Сохраняем ID сообщения
            state["order_message_id"] = sent_message.id
            ORDER_STATE[order_id]["order_message_id"] = sent_message.id

            # Редактируем оригинальное сообщение в личке
            await callback.message.edit_text(
                callback.message.text.html + "\n\n📤 <b>Заказ отправлен в группу.</b>",
                reply_markup=None
            )

            await callback.answer("✅ Заказ отправлен в группу!")
            logging.info(f"📤 Заказ {order_id} отправлен в группу: message_id={sent_message.id}")
        except Exception as e:
            logging.error(f"❌ Ошибка при отправке в группу: {e}")
            await callback.answer("❌ Не удалось отправить заказ в группу.")
        return

    elif data == "back_to_menu":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Активные заказы", callback_data="admin_active_orders"),
             InlineKeyboardButton("📅 Заказы в будущем", callback_data="admin_future_orders")],
            [InlineKeyboardButton("💰 Зарплата", callback_data="admin_salary")],
            [InlineKeyboardButton("✅ Выполненные за сегодня", callback_data="admin_delivered_today")]  # ✅ Новая кнопка
        ])
        await message.edit_text("👨‍💼 <b>Админ-меню</b>\nВыберите действие:", reply_markup=keyboard)
        await callback.answer()
        return

    elif data.startswith("view_future_order_"):
        order_id = data.replace("view_future_order_", "")
        order = next((o for o in load_future_orders() if str(o.get("id")) == order_id), None)
        if not order:
            await callback.answer("❌ Заказ не найден")
            return

        text = format_order_details(order)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖨️ Печать чека", callback_data=f"print_future_{order_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_future_orders")]
        ])
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return

    elif data.startswith("view_delivered_order_"):
        order_id = data.replace("view_delivered_order_", "")
        orders = load_active_orders()
        order = next((o for o in orders if str(o.get("id")) == order_id and o.get("status") == "delivered"), None)
        if not order:
            await callback.answer("❌ Заказ не найден")
            return

        text = format_order_details(order)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_delivered_today")]
        ])
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return

    elif data.startswith("order_ready_"):
        order_id = data.replace("order_ready_", "")
        orders = load_active_orders()
        target_order = None
        updated = False
        for o in orders:
            if str(o.get("id")) == order_id:
                o["status"] = "ready"
                save_active_orders(orders)
                target_order = o  # ✅ Присваиваем найденный заказ
                updated = True
                break
        if updated:
            await callback.answer("✅ Статус обновлён: готов")
            await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin_active_orders")]
            ]))
        else:
            await callback.answer("❌ Заказ не найден")
            return

        items_text = "\n".join(
            [f"• {item['qty']}x {item['name']}" for item in target_order.get("items", [])]
        )
        phone = target_order.get("phone") or "—"
        address = target_order.get("address") or "Самовывоз"
        time_str = target_order.get("time") or "По готовности"
        delivery_zone = target_order.get("delivery_zone", "—")
        total = target_order.get("total", 0)

        delivery_message = f"""
📦 <b>Заказ готов к выдаче!</b>

📞 <b>Телефон:</b> <phone>{phone}</phone>
🏠 <b>Адрес:</b> {address.capitalize()}
⏰ <b>Время:</b> {time_str}
📍 <b>Район:</b> {delivery_zone}

📋 <b>Состав:</b>
{items_text}

💰 <b>Итого:</b> <b>{total} ₽</b>
        """.strip()

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Выдан", callback_data=f"order_delivered_{order_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_active_orders")]
        ])

        try:
            await bot_app.send_message(
                chat_id=WORK_GROUP,
                reply_to_message_id=THREAD_DELIVERY_ID,
                text=delivery_message,
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"❌ Не удалось отправить сообщение в группу доставки: {e}")


    elif data.startswith("order_delivered_"):
        order_id = data.replace("order_delivered_", "")
        orders = load_active_orders()
        for o in orders:
            if str(o.get("id")) == order_id:
                o["status"] = "delivered"
                save_active_orders(orders)
                break
        save_active_orders(orders)
        await callback.answer("🗑️ Заказ удалён")
        await message.delete()
        return

    elif data.startswith("print_future_"):
        order_id = data.replace("print_future_", "")
        order = next((o for o in load_future_orders() if str(o.get("id")) == order_id), None)
        if not order:
            await callback.answer("❌ Заказ не найден")
            return
        print_receipt_html(order)
        await callback.answer("🖨️ Чек отправлен на печать")
        return


    elif data.startswith("edit_order:"):
        # Ищем order_id по message.id (ID самого чека)
        order_id = None
        for oid, state in ORDER_STATE.items():
            if state.get("order_message_id") == callback.message.id:
                order_id = oid
                break

        if not order_id:
            await callback.answer("❌ Не удалось найти заказ.")
            return

        # ✅ Устанавливаем ожидание СЛЕДУЮЩЕГО сообщения
        global awaiting_edit_from_message
        awaiting_edit_from_message = order_id

        # Меняем текст чека
        try:
            await callback.message.edit_text(
                "✏️ <b>Режим редактирования</b>\n\n"
                "Отправьте текст с изменениями (не обязательно как ответ):\n"
                "- Добавьте/удалите блюда\n"
                "- Обновите адрес, телефон, время",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 Отменить", callback_data="cancel_edit")]
                ])
            )
            logging.info(f"🔄 Ожидание редактирования через следующее сообщение: {order_id}")
        except Exception as e:
            logging.error(f"Ошибка при активации редактирования: {e}")

        await callback.answer()

    elif data.startswith("confirm_order:"):
        order_id = data.replace("confirm_order:", "")
        state = ORDER_STATE.get(order_id)
        if not state:
            await callback.answer("❌ Заказ не найден.")
            return

        # Удаляем из pending_orders
        pending_orders = load_pending_orders()
        if order_id in pending_orders:
            del pending_orders[order_id]
            save_pending_orders(pending_orders)
            logging.info(f"🗑️ Удалён из pending_orders: {order_id}")

        # Определяем дату доставки
        today = datetime.now().strftime("%d.%m.%Y")
        if not state.get("delivery_date"):
            state["delivery_date"] = today
        delivery_date = state["delivery_date"]

        total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))

        saved_order = {
            "id": order_id,
            "items": state["items"],
            "phone": state["phone"],
            "address": state["address"],
            "time": state["time"],
            "delivery_date": delivery_date,
            "delivery_zone": state["delivery_zone"],
            "delivery_price": state["delivery_price"],
            "total": total,
            "status": "accepted",
            "created_at": datetime.now().isoformat()
        }

        # === ✅ ШАГ 1: Добавляем в JSON (активные / будущие) ===
        if delivery_date == today:
            add_active_order(saved_order)
            logging.info(f"📥 Заказ {order_id} добавлен в активные")
        else:
            add_future_order(saved_order)
            logging.info(f"📅 Заказ {order_id} добавлен в будущие")

        # === ✅ ШАГ 2: Формируем текст сообщения для треда ===
        addr = state["address"]
        if isinstance(addr, dict):
            street = addr.get("street", "").strip()
            house = addr.get("house", "").strip()
            addr = f"{street} {house}".strip()
        elif not addr:
            addr = "Самовывоз"

        phone = state["phone"]
        time_order = state["time"] or "По готовности"
        phone_last_4 = phone[-4:] if len(phone) >= 4 else "—"

        text = (
            f"📦 <b>Заказ #{order_id}</b>\n"
            f"⏰ <b>Время:</b> {time_order}\n"
            f"🏠 <b>Адрес:</b> {addr}\n"
            f"📞 <b>Тел:</b> ...{phone_last_4}\n"
            f"💰 <b>Сумма:</b> {total} ₽"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁️ Посмотреть", callback_data=f"view_active_order_{order_id}")],
            [
                InlineKeyboardButton("✅ Готов", callback_data=f"order_ready_{order_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_order:{order_id}")
            ]
        ])

        # === ✅ ШАГ 3: Отправляем в тред активных заказов (THREAD_NOW_ID) ===
        try:
            sent_msg = await bot_app.send_message(
                chat_id=WORK_GROUP,
                text=text,
                reply_markup=keyboard,
                message_thread_id=THREAD_NOW_ID
            )
            logging.info(f"📤 Заказ {order_id} отправлен в тред активных заказов (ID: {sent_msg.id})")
        except Exception as e:
            logging.error(f"❌ Не удалось отправить заказ {order_id} в THREAD_NOW_ID: {e}")

        # === ✅ ШАГ 4: Удаляем из старого треда (THREAD_ORDER_ID) ===
        try:
            await bot_app.delete_messages(WORK_GROUP, state["order_message_id"])
            logging.info(f"🗑️ Удалён из треда заказов: {state['order_message_id']}")
        except Exception as e:
            logging.warning(f"❌ Не удалось удалить сообщение {state['order_message_id']}: {e}")

        # === ✅ ШАГ 5: Обновляем статус в интерфейсе и уведомляем админа ===
        state["status"] = "accepted"
        await update_order_message(order_id)

        await callback.answer("✅ Заказ принят и перенесён в активные")

        # === ✅ Уведомление пользователю (как было) ===
        user_id = None
        for uid, phone in USER_PHONE_MAP.items():
            if phone == state["phone"]:
                user_id = uid
                break

        if user_id:
            try:
                await bot_app.send_message(
                    chat_id=user_id,
                    text="✅ Ваш заказ подтверждён и передан в работу!\n\n"
                         "Мы свяжемся с вами при необходимости 📞"
                )
            except Exception as e:
                logging.error(f"❌ Не удалось отправить уведомление: {e}")

        return

    elif data.startswith("select_zone_"):
        zone_idx = int(data.split("_")[-1])
        logging.info(f"🔍 Обработка выбора зоны: message_id={callback.message.id}, zone_idx={zone_idx}")

        order_id = None
        for oid, state in ORDER_STATE.items():
            if state.get("zone_selection_message_id") == callback.message.id:
                order_id = oid
                break

        if not order_id:
            await callback.answer("❌ Не удалось найти заказ.")
            logging.warning(f"⚠️ Не найден order_id для message_id={callback.message.id}")
            return

        state = ORDER_STATE[order_id]
        matches = state.get("delivery_matches", [])
        if not matches:
            await callback.answer("❌ Нет доступных зон доставки.")
            return

        if 0 <= zone_idx < len(matches):
            zone, price, street_db = matches[zone_idx]
            state["delivery_zone"] = zone
            state["delivery_price"] = price
            # Сохраняем район и цену
            state["delivery_zone"] = zone
            state["delivery_price"] = price

            # Восстанавливаем оригинальный адрес с номером дома
            if state.get("original_address"):
                state["address"] = state["original_address"]
            else:
                state["address"] = f"{street_db}, {state['address'].split()[-1]}"  # попытка восстановить дом

            logging.info(f"📍 Выбрана зона: {zone} → {price} ₽")

            # Удаляем сообщение с выбором
            if state.get("zone_selection_message_id"):
                try:
                    await bot_app.delete_messages(WORK_GROUP, state["zone_selection_message_id"])
                    logging.info(f"🗑️ Удалено сообщение с выбором: {state['zone_selection_message_id']}")
                except Exception as e:
                    logging.error(f"❌ Не удалось удалить сообщение: {e}")
                state["zone_selection_message_id"] = None

            # Сохраняем в pending_orders.json
            update_pending_order_in_file(order_id, state)

            # Обновляем чек
            await update_order_message(order_id)
            await callback.answer(f"✅ Зона выбрана: {zone} (+{price} ₽)")
        else:
            await callback.answer("❌ Неверная зона.")
        return

    # === ОБЩИЙ ПАРСИНГ ДАННЫХ ЧЕРЕЗ ":" (после всех конкретных случаев) ===
    elif ":" in data:
        try:
            action, order_id = data.split(":", 1)
        except ValueError:
            await callback.answer("❌ Ошибка: неверные данные.")
            return

        if action == "cancel_order":
            state = ORDER_STATE.get(order_id)
            pending_orders = load_pending_orders()
            active_orders = load_active_orders()
            future_orders = load_future_orders()

            # Флаг: найден ли заказ
            found = False
            updated = False

            # --- 1. Обновляем в ORDER_STATE ---
            if order_id in ORDER_STATE:
                ORDER_STATE[order_id]["status"] = "cancelled"
                found = True

            # --- 2. Обновляем в pending_orders.json ---
            if str(order_id) in pending_orders:
                pending_orders[str(order_id)]["status"] = "cancelled"
                save_pending_orders(pending_orders)
                found = True

            # --- 3. Обновляем в active_orders.json ---
            for order in active_orders:
                if str(order.get("id")) == str(order_id):
                    order["status"] = "cancelled"
                    save_active_orders(active_orders)
                    found = True
                    updated = True
                    break

            # --- 4. Обновляем в future_orders.json ---
            for order in future_orders:
                if str(order.get("id")) == str(order_id):
                    order["status"] = "cancelled"
                    save_future_orders(future_orders)
                    found = True
                    updated = True
                    break

            # --- Редактируем сообщение ---
            try:
                await callback.message.edit_text(
                    callback.message.text.html + "\n\n🚫 <b>Заказ отменён.</b>",
                    reply_markup=None
                )
            except Exception as e:
                logging.warning(f"Не удалось редактировать сообщение: {e}")

            # --- Ответ ---
            if found:
                if updated:
                    await callback.answer("✅ Заказ отменён")
                    logging.info(f"✅ Заказ отмечен как отменённый: {order_id}")
                else:
                    await callback.answer("✅ Заказ уже был отменён")
            else:
                await callback.answer("❌ Заказ не найден")

            return

        else:
            await callback.answer("❌ Неизвестное действие.")
            return

    elif data == "cancel_edit":
        awaiting_edit_from_message = None  # ✅ Сброс

        order_id = None
        for oid, state in ORDER_STATE.items():
            if state.get("order_message_id") == callback.message.id:
                order_id = oid
                break

        if order_id and order_id in ORDER_STATE:
            ORDER_STATE[order_id]["awaiting_edit_order"] = False

        await update_order_message(order_id)
        await callback.answer("Редактирование отменено")
        return

    elif data == "remove_item":
        order_id = callback.message.id
        state = ORDER_STATE.get(order_id)
        if not state:
            await callback.answer("❌ Сессия истекла")
            return
        if not state.get("items"):
            await callback.answer("В заказе нет блюд.")
            return
        keyboard = []
        for item in state["items"]:
            label = f"{item['name']} (x{item['qty']})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"remove_{item['name']}_{order_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_order")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await callback.message.edit_text("Выберите позицию для удаления:", reply_markup=reply_markup)
            await callback.answer()
        except Exception as e:
            logging.error(f"❌ Ошибка при открытии удаления: {e}")

    elif data.startswith("remove_"):
        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer("❌ Неверный формат")
            return
        item_name = "_".join(parts[1:-1])
        order_id = parts[-1]
        state = ORDER_STATE.get(order_id)
        if not state:
            await callback.answer("❌ Сессия истекла")
            return
        item = next((it for it in state["items"] if it["name"] == item_name), None)
        if not item:
            await callback.answer("Позиция не найдена.")
            return
        if item["qty"] > 1:
            item["qty"] -= 1
            await callback.answer(f"➖ Уменьшено: {item_name} (осталось x{item['qty']})")
        else:
            state["items"].remove(item)
            await callback.answer(f"🗑️ Удалено: {item_name}")
        await update_order_message(order_id)

    elif data == "back_to_order":
        order_id = callback.message.id
        await update_order_message(order_id)
        await callback.answer()

    elif data.startswith("cat_"):
        category = data.replace("cat_", "")
        order_id = callback.message.id
        if order_id not in ORDER_STATE:
            initialize_user_state(order_id)
        ORDER_STATE[order_id]["last_category"] = category
        await show_dishes_by_category(order_id, category)
        await callback.answer()

    elif data.startswith("add_"):
        try:
            item_id = int(data.replace("add_", ""))
        except ValueError:
            await callback.answer("❌ Неверный ID блюда")
            return
        item = next((it for it in MENU_ITEMS if it["id"] == item_id), None)
        if not item:
            await callback.answer("❌ Блюдо не найдено")
            return
        order_id = callback.message.id
        if order_id not in ORDER_STATE:
            initialize_user_state(order_id)
        temp_cart = ORDER_STATE[order_id]["temp_cart"]
        existing = next((it for it in temp_cart if it["name"] == item["name"]), None)
        if existing:
            existing["qty"] += 1
        else:
            temp_cart.append({
                "name": item["name"],
                "qty": 1,
                "comment": "",
                "source_price": item["price"]
            })
        category = ORDER_STATE[order_id].get("last_category")
        if category:
            await show_dishes_by_category(order_id, category)
        await callback.answer()

    elif data == "finish_edit":
        order_id = None
        for oid, state in ORDER_STATE.items():
            if state.get("order_message_id") == callback.message.id:
                order_id = oid
                break

        if not order_id:
            await callback.answer("❌ Заказ не найден")
            return

        state = ORDER_STATE[order_id]
        temp_cart = state.get("temp_cart", [])
        cart = state.setdefault("items", [])

        for new_item in temp_cart:
            existing = next((it for it in cart if it["name"] == new_item["name"]), None)
            if existing:
                existing["qty"] += new_item["qty"]
            else:
                cart.append(new_item.copy())

        state["temp_cart"] = []

        # ✅ Редактируем сообщение обратно на стандартный вид
        try:
            await update_order_message(order_id)
            logging.info(f"✅ Режим добавления завершён, чек восстановлен: {order_id}")
        except Exception as e:
            logging.error(f"❌ Ошибка при восстановлении чека: {e}")

        # ✅ Сохраняем в pending_orders.json
        update_pending_order_in_file(order_id, state)

        await callback.answer("✅ Изменения применены")
        return

    elif data == "edit_zone":
        await show_delivery_zones(message)
        await callback.answer()

    elif data.startswith("zone_"):
        zone = data.replace("zone_", "")
        order_id = callback.message.id
        if order_id in ORDER_STATE:
            ORDER_STATE[order_id]["delivery_zone"] = zone
            await update_order_message(order_id)
            await callback.answer(f"Район выбран: {zone.capitalize()}")

    elif data == "print_receipt":
        order_id = callback.message.id
        state = ORDER_STATE.get(order_id)
        if not state:
            await callback.answer("Нет данных для печати.")
            return
        print_receipt_html(state)
        await callback.answer("🖨️ Чек отправлен на печать!")

    else:
        await callback.answer("❌ Неизвестная команда.")
        logging.warning(f"⚠️ Необработанный callback_data: {data}")

def generate_receipt_text(state):
    """
    Генерирует текст чека как строку.
    Используется для отправки в Telegram.
    """
    order_num = int(datetime.now().timestamp()) % 1000000  # Например: 123456
    total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))
    lines = []
    lines.append("   Магазин \"Орхидея\"")
    lines.append("-" * 22)
    lines.append(f"Заказ №{order_num:06d}")
    now = datetime.now().strftime("%d.%m %H:%M")
    lines.append(f"Время: {now}")
    lines.append("-" * 22)

    if state["phone"]:
        lines.append(f"Тел: {state['phone']}")
    if state["address"]:
        lines.append(f"Адрес: {state['address'].capitalize()}")
    if state["time"]:
        lines.append(f"Время: {state['time']}")
    if state.get("delivery_date"):
        lines.append(f"Дата: {state['delivery_date']}")
    if state["delivery_zone"]:
        lines.append(f"Район: {state['delivery_zone'].capitalize()}")
    lines.append(f"Доставка: {state.get('delivery_price', 0):>6} ₽")

    lines.append("-" * 22)

    for idx, item in enumerate(state["items"], start=1):
        name = item["name"]
        qty = item["qty"]
        comment = item["comment"] if item["comment"] else ""
        price_per_unit = item.get("source_price")
        if price_per_unit is None:
            menu_item = next((i for i in MENU_ITEMS if i["name"] == item["name"]), None)
            price_per_unit = menu_item["price"] if menu_item else 0
        line_total = price_per_unit * item["qty"]

        item_line = f"{idx}. {name}"
        lines.append(item_line)
        lines.append(f"   Кол-во: {qty} шт.")
        lines.append(f"   Цена: {line_total:,}".replace(",", " ") + " ₽")
        if comment:
            lines.append(f"   ⚠️{comment.capitalize()}")

        if idx < len(state["items"]):
            lines.append("-" * 22)

    lines.append("-" * 22)
    total_str = f"{total:,}".replace(",", " ") + " ₽"
    lines.append(f"ИТОГО:     {total_str:>8}")
    lines.append("-" * 22)
    lines.append("Спасибо за заказ!")
    lines.append("Приходите ещё!")

    return "\n".join(lines)

def print_receipt_html(state):
    """
    Генерирует HTML-чек и отправляет на печать через Chrome с флагом --kiosk-printing.
    Требует: Chrome установлен + ваш принтер 80C установлен как принтер по умолчанию.
    """
    import webbrowser
    import os
    import tempfile
    from datetime import datetime

    # Параметры
    MAX_WIDTH = "58mm"
    FONT_SIZE = "15px"
    LINE_HEIGHT = "1.2"

    delivery_cost = state.get("delivery_price", 0)
    total = calculate_total(state["items"], delivery_price=delivery_cost)

    logging.info("Печать чека")

    html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Чек</title>
        <style>
            @page {{
                size: {MAX_WIDTH} auto;
                margin: 2mm;
            }}
            body {{
                width: {MAX_WIDTH};
                font-family: 'sistem_ui';
                font-size: {FONT_SIZE};
                font-weight: 700;           /* Полужирный, но не максимальный */
                line-height: {LINE_HEIGHT};
                margin: 0;
                padding: 4px;
                box-sizing: border-box;
            }}
 
            .center {{ text-align: center; }}
            .right {{ float: right; }}
            .hr {{ border-top: 1px dashed #000; margin: 4px 0; clear: both; }}
            .item {{ margin: 2px 0; }}
            .comment {{ margin-left: 10px; color: #555; font-size: 9px; }}
            .total {{ font-weight: bold; font-size: 11px; margin-top: 6px; }}
            .header {{ font-size: 12px; margin-bottom: 4px; }}
        </style>
        <script>
            // Автопечать через 300 мс после загрузки
            window.addEventListener('load', () => {{
                setTimeout(() => {{
                    window.print();
                }}, 300);
            }});
        </script>
    </head>
    <body>
        <div class="center header"><b>Орхидея</b></div>
        <div class="hr"></div>
    """

    # Генерация номера чека
    order_num = int(datetime.now().timestamp()) % 1000000
    now = datetime.now().strftime("%d.%m %H:%M")
    html += f"""
        <div>Заказ №{order_num:06d}</div>
        <div>Время: {now}</div>
        <div class="hr"></div>
    """

    if state.get("phone"):   html += f"<div>Тел: {state['phone']}</div>"
    if state.get("address"): html += f"<div>Адр: {cut_text(state['address'], 32)}</div>"
    if state.get("time"):    html += f"<div>Время: {state['time']}</div>"
    if state.get("delivery_date"): html += f"<div>Дата: {state['delivery_date']}</div>"
    if state.get("delivery_zone"):
        html += f"<div>Район: {state['delivery_zone'].capitalize()}</div>"
        html += f"<div>Доставка: <span class='right'>{delivery_cost:,} ₽</span></div>".replace(",", " ")

    html += "<div class='hr'></div>"

    for idx, item in enumerate(state["items"], start=1):
        name = item["name"]
        qty = item["qty"]
        comment = item["comment"]
        price_per_unit = item.get("source_price") or next((i["price"] for i in MENU_ITEMS if i["name"] == item["name"]), 0)
        line_total = price_per_unit * qty

        html += f"""
        <div class="item">
            <div>{idx}. {name}</div>
            <div>Кол-во: {qty} шт. <span class='right'>{line_total:,} ₽</span></div>
        </div>
        """.replace(",", "")

        if comment:
            html += f"<div class='comment'>⚠️ {comment.capitalize()}</div>"
        if idx < len(state["items"]):
            html += "<div class='hr'></div>"

    html += "<div class='hr'></div>"
    html += f"<div class='total'>ИТОГО: <span class='right'>{total:,} ₽</span></div>".replace(",", "")
    html += "<div class='hr'></div>"
    html += "<div class='center'>Спасибо!</div>"
    html += "</body></html>"

    # Сохраняем временный HTML
    temp_dir = tempfile.gettempdir()
    html_path = os.path.join(temp_dir, f"receipt_{int(datetime.now().timestamp())}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    logging.info(f"📄 Временный HTML сохранён: {html_path}")

    # Формируем URL
    file_url = f"file://{html_path}"

    # Открываем в Chrome → если настроен --kiosk-printing → напечатает без диалога
    try:
        # Используем Chrome явно
        chrome_path = find_chrome_path()
        if chrome_path:
            import subprocess
            subprocess.Popen([
                chrome_path,
                "--new-window",
                "--kiosk-printing",
                "--disable-popup-blocking",
                file_url
            ])
            logging.info(f"🖨️ Chrome запущен для печати: {file_url}")
        else:
            webbrowser.open(file_url)
            logging.warning("⚠️ Chrome не найден → используем браузер по умолчанию")
    except Exception as e:
        logging.error(f"❌ Ошибка запуска Chrome: {e}")
        webbrowser.open(file_url)

def find_chrome_path():
    """Находит путь к Chrome."""
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return None

def cut_text(text, max_len):
    """Обрезает текст до указанной длины."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


async def show_dishes_by_category(order_id, category: str):
    """
    Показывает блюда по категории, редактируя сохранённое сообщение.
    """
    state = ORDER_STATE.get(order_id)
    if not state:
        logging.warning(f"❌ Состояние не найдено для order_id={order_id}")
        return

    temp_cart = state.get("temp_cart", [])
    dishes = [item for item in MENU_ITEMS if item["category"] == category]
    keyboard = []
    row = []

    for item in dishes:
        cart_item = next((it for it in temp_cart if it["name"] == item["name"]), None)
        qty = cart_item["qty"] if cart_item else 0

        btn_text = f"{item['name']} — {item['price']}₽"
        if qty > 0:
            btn_text = f"{item['name']} (x{qty}) — {item['price']}₽"

        btn = InlineKeyboardButton(btn_text, callback_data=f"add_{item['id']}")
        if len(row) >= 1:
            keyboard.append(row)
            row = [btn]
        else:
            row.append(btn)
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back_to_categories")])
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="finish_edit")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"🍽️ <b>Категория:</b> {category}\nВыберите блюдо:"

    # Fallback: если нет category_message_id — используем order_message_id
    message_id = state.get("category_message_id") or state.get("order_message_id")
    if not message_id:
        logging.error(f"❌ Не найден ни category_message_id, ни order_message_id для order_id={order_id}")
        return

    try:
        await bot_app.edit_message_text(
            chat_id=WORK_GROUP,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup
        )
        logging.info(f"✅ Обновлено сообщение с блюдами: {message_id}")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            try:
                await bot_app.edit_message_text(
                    chat_id=WORK_GROUP,
                    message_id=message_id,
                    text=text + " ",
                    reply_markup=reply_markup
                )
            except Exception as e2:
                logging.error(f"❌ Ошибка при редактировании (с пробелом): {e2}")
        else:
            logging.error(f"❌ Ошибка при редактировании: {e}")
# --- Получить категорию блюда ---
def get_item_category(name):
    item = next((i for i in MENU_ITEMS if i["name"] == name), None)
    return item["category"] if item else "Роллы"

# --- Показать зоны доставки ---
async def show_delivery_zones(message):
    keyboard = []
    for zone in DELIVERY_ZONES.keys():
        keyboard.append([InlineKeyboardButton(f"{zone.capitalize()} — {DELIVERY_ZONES[zone]} ₽", callback_data=f"zone_{zone}")])
    keyboard.append([InlineKeyboardButton("🚫 Без района", callback_data="zone_none")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("Выберите район доставки:", reply_markup=reply_markup)

# --- Печать на локальном принтере Windows ---
def print_on_local_printer(text):
    """
    Сохраняет чек в .txt без лишнего обрезания.
    Пытается разместить название, количество и цену в одной строке.
    Заменяет только проблемные символы.
    """
    MAX_LINE_LENGTH = 40  # Подходит для большинства чековых принтеров (58 мм)



    lines = text.split('\n')
    processed_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            processed_lines.append("")
            continue

        # Проверяем, содержит ли строка информацию о позиции: "• 2x Лава Ролл — 700 ₽"
        import re
        match = re.match(r'•\s*(\d+)x\s*(.+?)\s*—\s*(\d+)\s*RUB', line)
        if match:
            qty = match.group(1)
            name = match.group(2).strip()
            price = match.group(3)

            # Формируем одну строку: "Лава Ролл x2   700 RUB"
            item_line = f"{name} x{qty}"
            if len(item_line) > MAX_LINE_LENGTH - 10:
                item_line = item_line[:MAX_LINE_LENGTH - 13] + "..."
            price_part = f"{price:>6} RUB"
            full_line = item_line.ljust(MAX_LINE_LENGTH - len(price_part)) + price_part
            processed_lines.append(full_line)
        elif "ИТОГО:" in line:
            # Центрируем или выравниваем итог
            total_match = re.search(r'(\d+)\s*RUB', line)
            if total_match:
                total = total_match.group(1)
                left = "ИТОГО:"
                space = MAX_LINE_LENGTH - len(left) - len(total) - 6
                processed_lines.append(f"{left}{' ' * space}{total:>6} RUB")
            else:
                processed_lines.append(line)
        elif "⚠️" in line:
            # Комментарии
            comment = line.replace("⚠️", "").strip().capitalize()
            if len(comment) > MAX_LINE_LENGTH - 2:
                comment = comment[:MAX_LINE_LENGTH - 5] + "..."
            processed_lines.append(f" ⚠️{comment}")
        else:
            # Просто переносим длинные строки
            while len(line) > MAX_LINE_LENGTH:
                break_pos = line.rfind(' ', 0, MAX_LINE_LENGTH)
                if break_pos == -1:
                    break_pos = MAX_LINE_LENGTH
                chunk = line[:break_pos].rstrip()
                processed_lines.append(chunk)
                line = line[break_pos:].lstrip()
            if line:
                processed_lines.append(line)

    processed_text = '\n'.join(processed_lines)

    # --- Шаг 2: Сохраняем в файл ---
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, encoding="utf-8", mode="w") as f:
        f.write(processed_text)
        temp_file = f.name

    try:
        subprocess.Popen(["notepad.exe", temp_file])
    except Exception as e:
        logging.error(f"❌ Не удалось открыть файл: {e}")

    # --- Шаг 3: Печать через Windows Print System ---
    try:
        printers = [p[2] for p in win32print.EnumPrinters(2)]
        logging.info(f"🖨️ Доступные принтеры: {printers}")

        if PRINTER_NAME not in printers:
            raise Exception(f"Принтер '{PRINTER_NAME}' не найден. Доступные: {', '.join(printers)}")

        win32api.ShellExecute(
            0,
            "printto",
            temp_file,
            f'"{PRINTER_NAME}"',
            ".",
            0
        )
        logging.info(f"✅ Чек отправлен на принтер '{PRINTER_NAME}' через printto")

    except Exception as e:
        logging.error(f"❌ Ошибка печати через printto: {e}")
        try:
            os.startfile(temp_file)
        except:
            pass

def find_delivery_zone_by_address(address):
    if not address or len(address.strip()) < 2:
        return []

    try:
        df = pd.read_excel(ADDRESS_XLSX)
        street_col = next((col for col in df.columns if "street" in col.lower()), "street")
        zone_col = next((col for col in df.columns if "zone" in col.lower() or "район" in col.lower()), "zone")
        price_col = next((col for col in df.columns if "price" in col.lower() or "цена" in col.lower()), "price")

        input_clean = clean_street_name(address)
        if not input_clean:
            return []

        matches = []
        for _, row in df.iterrows():
            street_db = str(row[street_col])
            db_clean = clean_street_name(street_db)
            zone = str(row[zone_col]).strip()
            price = int(row[price_col]) if pd.notna(row[price_col]) else 0

            # Полное совпадение — приоритет
            if input_clean == db_clean:
                matches.append((zone, price, street_db))
            else:
                # Fuzzy-сравнение
                ratio = fuzz.token_sort_ratio(input_clean, db_clean)
                if ratio >= 80:  # Порог можно настроить
                    matches.append((zone, price, street_db))
                    logging.info(f"🔍 Fuzzy-совпадение: '{input_clean}' ~ '{db_clean}' (схожесть: {ratio})")

        # Убираем дубли
        seen = set()
        unique_matches = []
        for m in matches:
            key = (m[0], m[1], m[2].lower())  # zone, price, street
            if key not in seen:
                seen.add(key)
                unique_matches.append(m)

        logging.info(f"🔍 Поиск по адресу: '{address}' → clean='{input_clean}'")
        logging.info(f"   Найдено совпадений: {len(unique_matches)}")
        for zone, price, street_db in unique_matches:
            logging.info(f"   → Зона: {zone}, Цена: {price} ₽, Улица БД: {street_db}")

        return unique_matches

    except Exception as e:
        logging.error(f"❌ Ошибка при поиске зоны доставки: {e}")
        return []


# --- Запуск бота ---
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )

    logging.info("🔄 Запуск бота...")

    load_menu()
    load_delivery_zones()
    check_files()
    load_user_phones()  # ← Добавьте эту строку

    logging.info("🚀 Бот успешно запущен и готов к работе.")
    bot_app.run()