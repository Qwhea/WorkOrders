import asyncio
import logging
import re
import socket
import subprocess
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import pandas as pd
import json
import os
from fuzzywuzzy import process, fuzz

import tempfile
import win32print
import win32api

from datetime import timedelta


# --- Конфигурация ---
API_ID = 33621079
API_HASH = "5378ac906c789310f63f3c60f2063b6e"
BOT_TOKEN = "8472836665:AAGqmM0rVEbnWA_xjYdjmYh2wd6ytgHNRBk"

WORK_GROUP = -1003646541060
THREAD_NOW_ID = 3087
THREAD_FUTURE_ID = 3089

ORDERS_JSON = "orders.json"
ACTIVE_ORDERS_JSON = "active_orders.json"  # ← новое
FUTURE_ORDERS_JSON = "future_orders.json"  # ← новое
MENU_XLSX = "menu.xlsx"
ADDRESS_XLSX = "adress.xlsx"
PRINTER_NAME = "80C"

bot_app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Работа с данными ---
def load_orders():
    if os.path.exists(ORDERS_JSON):
        with open(ORDERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_orders(orders):
    with open(ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

def load_active_orders():
    if os.path.exists(ACTIVE_ORDERS_JSON):
        with open(ACTIVE_ORDERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_active_orders(orders):
    with open(ACTIVE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

def load_future_orders():
    if os.path.exists(FUTURE_ORDERS_JSON):
        with open(FUTURE_ORDERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_future_orders(orders):
    with open(FUTURE_ORDERS_JSON, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)


# --- Глобальные переменные ---
MENU_ITEMS = []
MENU_NAMES = []
DELIVERY_ZONES = {}  # { "район": цена }
STREET_NAMES = []    # Список чистых названий улиц из базы
USER_EDIT_STATE = {}
CATEGORIES = []


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

# --- Обработчики ---
@bot_app.on_message(filters.command("start"))
async def start(client, message):
    USER_EDIT_STATE.pop(message.from_user.id, None)
    await message.reply_text(
        "Привет! 🍣 Отправьте заказ **одним сообщением** в любом порядке. "
        "Укажите:\n- Названия блюд\n- Количество (опционально)\n- Время доставки\n- Адрес\n- Номер телефона\n\n"
        "Пример:\n"
        "2 Лава Креветка без сыра\n"
        "Завтра 19:00\n"
        "+7 999 123-45-67\n"
        "ул. Горького, д. 5, кв. 2, район Центр"
    )

@bot_app.on_message(filters.command("menu"))
async def send_menu(client, message):
    if not MENU_ITEMS:
        await message.reply_text("Меню временно недоступно.")
        return

    categories = {}
    for item in MENU_ITEMS:
        cat = item["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(f"{item['name']} — {item['price']} ₽")

    response = "📋 Наше меню:\n\n"
    for category, items in categories.items():
        response += f"<b>{category}</b>\n"
        response += "\n".join(items)
        response += "\n\n"

    await message.reply_text(response)

def parse_order_lines(lines):
    time_line = None
    time_line_full = None
    address_line = None
    phone_line = None

    time_pattern = r'([0-1]?[0-9]|2[0-3]):([0-5][0-9])'
    phone_pattern = r'(\+7|8)[- ]?\(?(\d{3})\)?[- ]?(\d{3})[- ]?(\d{2})[- ]?(\d{2})'

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        line_lower = line_stripped.lower()

        if not time_line and re.search(time_pattern, line_lower):
            time_match = re.search(time_pattern, line_lower)
            time_line = time_match.group(0)
            time_line_full = line_stripped

        elif not phone_line and re.search(phone_pattern, line_lower):
            digits = re.sub(r'\D', '', line_stripped)
            if digits.startswith('8'):
                digits = '7' + digits[1:]
            phone_line = '+' + digits if len(digits) == 11 else None

        elif any(word in line_lower for word in ['самовывоз', 'свой', 'лично', 'приду', 'заберу', 'заберу сам']):
            address_line = "Самовывоз"

    if address_line != "Самовывоз":
        potential_address_lines = []
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            if (time_line_full and line_stripped == time_line_full) or \
                    (phone_line and re.sub(r'\D', '', line_stripped) == re.sub(r'\D', '', phone_line)):
                continue
            potential_address_lines.append(line_stripped)

        def clean(s):
            """
            Очищает название улицы: убирает тип, номер дома, лишние символы.
            Работает даже с '30 лет Победы', 'Проспект Мира 100' и т.п.
            """
            s = str(s).strip().lower()

            # Шаг 1: Убираем тип улицы (даже если он в середине)
            s = re.sub(r'\b(?:ул\.?|улица|проспект|пр\.?|переулок|пер\.|набережная|шоссе|бульвар|аллея|площадь|пл\.?)\b\s*', '', s)

            # Шаг 2: Убираем номер дома ТОЛЬКО в конце строки
            s = re.sub(r',?\s*\d+[\s\-\/\\]?\w*\.*\s*(?:кв\.?\s*\d+|корпус\s*\d+|стр\.?\s*\d+)?\s*$', '', s)

            # Шаг 3: Убираем запятые, точки, дефисы и заменяем на один пробел
            s = re.sub(r'[,\.\-\s]+', ' ', s).strip()

            return s

        for line in potential_address_lines:
            line_clean = clean(line)
            if not line_clean:
                continue
            match, score = process.extractOne(line_clean, STREET_NAMES, scorer=fuzz.token_sort_ratio)
            if score >= 80:
                address_line = line
                logging.info(f"📍 Адрес распознан: '{line}' → '{match}' (схожесть: {score})")
                break

    # ВСЕГДА собираем dish_lines из нераспознанных строк
    dish_lines = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Проверяем, является ли строка временем
        is_time = time_line_full and line_stripped == time_line_full

        # Проверяем, является ли строкой телефоном
        is_phone = False
        if phone_line and line_stripped:
            digits_line = re.sub(r'\D', '', line_stripped)
            digits_phone = re.sub(r'\D', '', phone_line)
            if len(digits_line) >= 10 and len(digits_phone) >= 10:
                is_phone = digits_line[-10:] == digits_phone[-10:]

        # Проверяем, является ли строкой адресом
        is_address = address_line and line_stripped == address_line

        if is_time or is_phone or is_address:
            continue
        else:
            dish_lines.append(line_stripped)

    logging.info(f"📞 Найден телефон: {phone_line}")
    logging.info(f"⏰ Время: {time_line_full} ({time_line})")
    logging.info(f"🏠 Адрес: {address_line}")
    logging.info(f"🍽️ Блюда: {dish_lines}")

    return dish_lines, time_line, address_line, phone_line

def find_item_by_name(detected_item, threshold=60):
    if not detected_item or len(detected_item.strip()) < 2:
        return None

    detected_norm = detected_item.strip().lower()

    if detected_norm.isdigit() or len(detected_norm) < 3:
        return None

    if not MENU_ITEMS:
        logging.error("❌ MENU_ITEMS пуст!")
        return None

    best_match = None
    best_ratio = 0

    for item in MENU_ITEMS:
        name_norm = item["name"].lower()
        ratio = fuzz.token_sort_ratio(detected_norm, name_norm)
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = item

    if best_match and best_ratio >= threshold:
        logging.info(f"🔄 '{detected_item}' → '{best_match['name']}' ({best_ratio})")
        return best_match
    else:
        logging.warning(f"❌ Не найдено: '{detected_item}' (лучшая: {best_ratio}, порог: {threshold})")
        return None

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

def initialize_user_state(user_id):
    USER_EDIT_STATE[user_id] = {
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
        "awaiting_edit_order": False  # ← новое состояние
    }

@bot_app.on_message(filters.text & ~filters.command(["start", "menu"]))
async def handle_order(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    first_name = message.from_user.first_name

    if user_id not in USER_EDIT_STATE:
        initialize_user_state(user_id)

    state = USER_EDIT_STATE[user_id]

    # === РЕЖИМ РЕДАКТИРОВАНИЯ ЗАКАЗА ===
    if state.get("awaiting_edit_order"):
        if not text:
            await message.reply("❌ Сообщение пустое.")
            return

        state["awaiting_edit_order"] = False
        lines = text.split('\n')

        # Парсим
        dish_lines, time_guess, address_guess, phone_guess = parse_order_lines(lines)

        # Обновляем дату доставки
        delivery_date = None
        for line in lines:
            if re.search(r'([0-1]?[0-9]|2[0-3]):([0-5][0-9])', line.strip().lower()):
                delivery_date = parse_delivery_date(line.strip())
                break
        else:
            delivery_date = parse_delivery_date(text)

        # Обновляем телефон
        if phone_guess:
            state["phone"] = phone_guess
            await message.reply(f"📞 Телефон обновлён: {phone_guess}")

        # Обновляем время
        if time_guess:
            state["time"] = time_guess
            state["delivery_date"] = delivery_date
            await message.reply(f"⏰ Время обновлено: {time_guess}" + (f", дата: {delivery_date}" if delivery_date else ""))

        # Обновляем адрес
        if address_guess:
            state["address"] = address_guess
            matches = find_delivery_zone_by_address(address_guess)
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
                await show_zone_selection(message, matches)
                return

        # === Добавляем новые блюда к существующим ===
        found_items = []
        unrecognized = []

        for line in dish_lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # Комментарий после *
            if '*' in line_stripped:
                parts = line_stripped.split('*', 1)
                item_text = parts[0].strip()
                comment = parts[1].strip()
            else:
                item_text = line_stripped
                comment = ""

            # Количество
            qty_match = re.match(r'^(\d+)\s+(.+)$', item_text)
            quantity = 1
            search_text = item_text
            if qty_match:
                quantity = int(qty_match.group(1))
                search_text = qty_match.group(2).strip()

            matched_item = find_item_by_name(search_text, threshold=60)
            if matched_item:
                found_items.append({
                    "name": matched_item["name"],
                    "qty": quantity,
                    "comment": comment,
                    "source_price": matched_item["price"]
                })
            else:
                unrecognized.append(line)

        if unrecognized:
            await message.reply(f"❌ Не распознано: {', '.join(unrecognized)}")

        # Добавляем к существующим позициям
        existing_items = state["items"]
        for new_item in found_items:
            existing = next((it for it in existing_items if it["name"] == new_item["name"]), None)
            if existing:
                existing["qty"] += new_item["qty"]
                if new_item["comment"]:
                    existing["comment"] = new_item["comment"]
            else:
                existing_items.append(new_item)

        if found_items:
            items_str = ", ".join([f"{it['qty']}x {it['name']}" for it in found_items])
            await message.reply(f"✅ Добавлено: {items_str}")

        await update_order_message(user_id, first_name)  # ✅ Правильный вызов
        return

    # === ОБЫЧНЫЙ РЕЖИМ: новый заказ ===
    lines = text.split('\n')

    # Проверка внешнего заказа (my2can)
    if "Новый заказ от" in text:
        parsed = parse_external_order(text)
        if not parsed["items"]:
            await message.reply("❌ Не удалось распознать позиции.")
            return

        state.update({
            "items": [i.copy() for i in parsed["items"]],
            "temp_cart": [],
            "address": parsed["address"],
            "phone": parsed["phone"],
            "time": None,
            "delivery_date": datetime.now().strftime("%d.%m.%Y"),
            "delivery_matches": [],
            "order_message_id": None,
            "zone_selection_message_id": None,
            "category_message_id": None,
            "awaiting": None
        })

        matches = find_delivery_zone_by_address(parsed["address"])
        if matches:
            zone, price, _ = matches[0]
            state["delivery_zone"] = zone
            state["delivery_price"] = price
        else:
            state["delivery_zone"] = "Самовывоз"
            state["delivery_price"] = 0

        await show_editable_order_inline(message, parsed.get("client_name", "Клиент"))
        return

    # Обычный заказ
    dish_lines, time_guess, address_guess, phone_guess = parse_order_lines(lines)

    # Определение даты
    delivery_date = None
    for line in lines:
        if re.search(r'([0-1]?[0-9]|2[0-3]):([0-5][0-9])', line.strip().lower()):
            delivery_date = parse_delivery_date(line.strip())
            break
    else:
        delivery_date = parse_delivery_date(text)

    state["delivery_date"] = delivery_date

    # Парсинг блюд
    found_items = []
    unrecognized = []

    for line in dish_lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        if '*' in line_stripped:
            parts = line_stripped.split('*', 1)
            item_text = parts[0].strip()
            comment = parts[1].strip()
        else:
            item_text = line_stripped
            comment = ""

        qty_match = re.match(r'^(\d+)\s+(.+)$', item_text)
        quantity = 1
        search_text = item_text
        if qty_match:
            quantity = int(qty_match.group(1))
            search_text = qty_match.group(2).strip()

        matched_item = find_item_by_name(search_text, threshold=60)
        if matched_item:
            found_items.append({
                "name": matched_item["name"],
                "qty": quantity,
                "comment": comment,
                "source_price": matched_item["price"]
            })
        else:
            unrecognized.append(line)

    if unrecognized:
        await message.reply_text(f"❌ Не распознано: {', '.join(unrecognized)}")
        return

    if not found_items:
        await message.reply_text("❌ Ни одно блюдо не найдено.")
        return

    # Полная замена для нового заказа
    state["items"] = found_items
    state["time"] = time_guess
    state["address"] = address_guess
    state["phone"] = phone_guess

    if address_guess and "самовывоз" in address_guess.lower():
        state["delivery_zone"] = "Самовывоз"
        state["delivery_price"] = 0
        await show_editable_order_inline(message, first_name)
        return

    matches = find_delivery_zone_by_address(address_guess) if address_guess else []
    state["delivery_matches"] = matches

    if not matches:
        state["delivery_zone"] = "Самовывоз"
        state["delivery_price"] = 0
    elif len(matches) == 1:
        zone, price, _ = matches[0]
        state["delivery_zone"] = zone
        state["delivery_price"] = price
    else:
        await show_zone_selection(message, matches)
        return

    await show_editable_order_inline(message, first_name)

async def show_zone_selection(message, matches):
    """Отправляет кнопки для выбора правильной зоны."""
    keyboard = []
    for i, (zone, price, street_db) in enumerate(matches):
        keyboard.append([InlineKeyboardButton(f"{zone} — {price} ₽ ({street_db})", callback_data=f"select_zone_{i}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await message.reply_text("📍 Найдено несколько зон доставки. Выберите подходящую:", reply_markup=reply_markup)

    # Сохраняем ID сообщения с выбором зоны
    user_id = message.from_user.id
    USER_EDIT_STATE[user_id]["zone_selection_message_id"] = msg.id
    logging.info(f"📌 Сообщение с выбором зоны сохранено: {msg.id}")


async def show_editable_order_inline(message_or_callback, first_name):
    """Отправляет или редактирует сообщение с заказом и сохраняет его ID."""
    user_id = message_or_callback.from_user.id
    state = USER_EDIT_STATE.get(user_id)
    if not state:
        return

    delivery_zone = state.get("delivery_zone")
    delivery_cost = state.get("delivery_price", 0)
    delivery_date = state.get("delivery_date")

    total = calculate_total(state["items"], delivery_price=delivery_cost)
    order_text = (
            f"📦 <b>Ваш заказ</b>\n"
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

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить позицию", callback_data="add_item")],
            [InlineKeyboardButton("➖ Убрать позицию", callback_data="remove_item")],
            [InlineKeyboardButton("✏️ Редактировать заказ", callback_data="edit_order")],
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data="confirm_order")]
        ]
    )


    if hasattr(message_or_callback, "message"):  # callback
        try:
            msg = await message_or_callback.message.edit_text(order_text, reply_markup=keyboard)
            USER_EDIT_STATE[user_id]["order_message_id"] = msg.id
            logging.info(f"📌 Сохранён order_message_id: {msg.id} для {user_id}")
        except Exception as e:
            logging.error(f"Ошибка редактирования: {e}")
            msg = await message_or_callback.message.reply_text(order_text, reply_markup=keyboard)
            USER_EDIT_STATE[user_id]["order_message_id"] = msg.id
            logging.info(f"📌 Новое сообщение: {msg.id}")
    else:  # обычное сообщение
        msg = await message_or_callback.reply_text(order_text, reply_markup=keyboard)
        USER_EDIT_STATE[user_id]["order_message_id"] = msg.id
        logging.info(f"📌 Первичное сообщение: {msg.id}")

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


async def update_order_message(user_id, first_name):
    """Редактирует уже отправленное сообщение с заказом."""
    state = USER_EDIT_STATE.get(user_id)
    if not state:
        return

    message_id = state.get("order_message_id")
    if not message_id:
        return  # нечего редактировать

    delivery_zone = state.get("delivery_zone")
    delivery_cost = state.get("delivery_price", 0)
    delivery_date = state.get("delivery_date")

    total = calculate_total(state["items"], delivery_price=delivery_cost)
    order_text = (
            f"📦 <b>Ваш заказ</b>\n"
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

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить позицию", callback_data="add_item")],
            [InlineKeyboardButton("➖ Убрать позицию", callback_data="remove_item")],
            [InlineKeyboardButton("✏️ Редактировать заказ", callback_data="edit_order")],
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data="confirm_order")]
        ]
    )

    try:
        await bot_app.edit_message_text(
            chat_id=WORK_GROUP,
            message_id=message_id,
            text=order_text,
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения: {e}")

def parse_external_order(text):
    """
    Парсит заказ из my2can.com.
    Возвращает словарь: {
        items: [{"name", "qty", "comment", "source_price"}],
        address: str,
        phone: str,
        delivery_time: None,
        client_name: str
    }
    """
    lines = text.strip().split('\n')
    items = []
    address = None
    phone = None
    client_name = "Клиент"

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Имя клиента
        if line.startswith("Клиент:"):
            client_name = line.split(":", 1)[1].strip()

        # Телефон
        elif line.startswith("Телефон:"):
            digits = re.sub(r'\D', '', line)
            if digits.startswith('8'):
                digits = '7' + digits[1:]
            phone = '+' + digits if len(digits) == 11 else None

        # Адрес
        elif line.startswith("Адрес:"):
            addr_part = line.split(":", 1)[1].strip()
            # Убираем регион и район, оставляем только город/село и улицу
            if "сельское поселение" in addr_part.lower():
                addr_part = re.sub(r'.*сельское поселение[^,]*,', '', addr_part, flags=re.IGNORECASE)
            if "р-н." in addr_part or "район" in addr_part:
                addr_part = re.sub(r'Томская обл\.[^,]*,', '', addr_part)
                addr_part = re.sub(r'Парабельский р-н\.', '', addr_part)
            addr_part = re.sub(r'село\s+', '', addr_part, flags=re.IGNORECASE)
            addr_part = re.sub(r'дом', 'д.', addr_part, flags=re.IGNORECASE)
            addr_part = re.sub(r'квартира', 'кв.', addr_part, flags=re.IGNORECASE)
            addr_part = re.sub(r'\s+', ' ', addr_part).strip()
            address = addr_part

        # Позиции
        elif re.match(r'\d+\.\s*.+?-\s*\d+\s*ШТ\s*-\s*[\d\s,]+₽', line):
            match = re.match(r'\d+\.\s*(.+?)\s*-\s*(\d+)\s*ШТ\s*-\s*([\d\s,]+)\s*₽', line)
            if match:
                name = match.group(1).strip()
                qty = int(match.group(2))
                price_str = match.group(3).replace(' ', '').replace(',', '.')
                try:
                    price_total = int(float(price_str))
                except:
                    price_total = 0

                if "доставка" in name.lower():
                    i += 1
                    continue  # пропускаем как отдельную позицию

                items.append({
                    "name": name,
                    "qty": qty,
                    "comment": "",
                    "source_price": price_total // qty if qty > 0 else 0
                })

        i += 1

    return {
        "items": items,
        "address": address,
        "phone": phone,
        "delivery_time": None,
        "client_name": client_name
    }

# --- Обработка кнопок редактирования ---
@bot_app.on_callback_query()
async def handle_callback(client, callback):
    user_id = callback.from_user.id
    data = callback.data

    logging.info(f"🔔 Callback от {user_id}: {data}")
    if user_id not in USER_EDIT_STATE:
        logging.warning(f"⚠️ Нет состояния для пользователя {user_id}")
    else:
        logging.info(f"💬 Состояние найдено: {list(USER_EDIT_STATE[user_id].keys())}")

    if data.startswith("select_zone_"):
        idx = int(data.replace("select_zone_", ""))
        matches = USER_EDIT_STATE.get(user_id, {}).get("delivery_matches", [])
        if 0 <= idx < len(matches):
            zone, price, street_db = matches[idx]
            USER_EDIT_STATE[user_id]["delivery_zone"] = zone
            USER_EDIT_STATE[user_id]["delivery_price"] = price
            first_name = callback.from_user.first_name

            # Удаляем сообщение с выбором зоны
            zone_msg_id = USER_EDIT_STATE[user_id].get("zone_selection_message_id")
            if zone_msg_id:
                try:
                    await bot_app.edit_message_text(
                        chat_id=WORK_GROUP,
                        message_id=zone_msg_id,
                        text=f"✅ Выбрано: {zone}"
                    )
                except Exception as e:
                    logging.error(f"Не удалось отредактировать: {e}")

            # Обновляем основное сообщение с заказом
            await show_editable_order_inline(callback, first_name)
            await callback.answer(f"Зона выбрана: {zone}")
        else:
            await callback.answer("❌ Неверный выбор.")

    elif data == "edit_order":
        user_id = callback.from_user.id
        state = USER_EDIT_STATE.get(user_id)
        if not state:
            await callback.answer("❌ Сессия истекла.")
            return

        # Включаем режим редактирования
        state["awaiting_edit_order"] = True
        state["awaiting"] = None  # выключаем другие ожидания

        try:
            await callback.message.edit_text("✏️ Отправьте текст с изменениями:\n\n"
                                             "- Добавьте новые блюда (например: `2 Лава Креветка`)\n"
                                             "- Укажите новый телефон, время или адрес\n"
                                             "- Можно всё вместе")
            await callback.answer()
        except Exception as e:
            logging.error(f"❌ Ошибка при редактировании: {e}")
            await callback.answer("Ошибка интерфейса.")

    elif data == "confirm_order":
        state = USER_EDIT_STATE.get(user_id)
        if not state:
            await callback.answer("Заказ не найден.")
            return

        first_name = callback.from_user.first_name
        total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))

        order_text = (
                f"📦 <b>Новый заказ</b>\n"
                f"👤 {first_name}\n"
                f"📞 {state['phone']}\n"
                f"⏰ {state['time']}\n"
                f"📅 {state.get('delivery_date', 'Сегодня')}\n"
                f"🏠 {state['address']}\n"
                f"📍 Район: {state['delivery_zone'].capitalize() if state['delivery_zone'] else 'Не указан'}\n"
                f"🚚 Доставка: {state.get('delivery_price', 0)} ₽\n\n"
                f"🍣 Блюда:\n" + "\n".join([
            f"• {it['qty']}x {it['name']}" + (f" {it['comment']}" if it['comment'] else "")
            for it in state["items"]
        ]) +
                f"\n\n💰 Итого: {total} ₽"
        )

        # Отправляем в рабочую группу
        try:
            await bot_app.send_message(chat_id=WORK_GROUP, text=order_text)
            await callback.edit_message_text("✅ Заказ подтверждён и отправлен!")
        except Exception as e:
            await callback.edit_message_text("❌ Ошибка отправки заказа.")
            logging.error(f"Ошибка отправки заказа: {e}")
            return

        # === Определяем дату доставки ===
        delivery_date_str = state.get("delivery_date")
        today_str = datetime.now().strftime("%d.%m.%Y")

        is_today = delivery_date_str == today_str or not delivery_date_str

        # === Формируем объект заказа ===
        order_obj = {
            "user_id": user_id,
            "client_name": first_name,
            "phone": state["phone"],
            "address": state["address"],
            "time": state["time"],
            "delivery_date": delivery_date_str or today_str,
            "delivery_zone": state["delivery_zone"],
            "delivery_price": state.get("delivery_price", 0),
            "items": state["items"],
            "total": total,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        # === Сохраняем в нужную базу ===
        if is_today:
            active_orders = load_active_orders()
            active_orders.append(order_obj)
            save_active_orders(active_orders)
            logging.info(f"✅ Активный заказ сохранён: {order_obj['phone']}")
        else:
            future_orders = load_future_orders()
            future_orders.append(order_obj)
            save_future_orders(future_orders)
            logging.info(f"📅 Будущий заказ сохранён: {order_obj['delivery_date']} | {order_obj['phone']}")

        # === Печать чека только если сегодня ===
        if is_today:
            try:
                print_receipt_html(state)
                await callback.message.reply("🖨️ Чек отправлен на печать!")
            except Exception as e:
                logging.error(f"❌ Ошибка печати при подтверждении: {e}")
        else:
            try:
                # Генерируем текст чека
                receipt_text = generate_receipt_text(state)

                # Клавиатура с кнопкой печати
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🖨️ Распечатать чек", callback_data=f"print_future_{user_id}")]
                    ]
                )

                # Формируем красивый HTML-чек
                html_receipt = (
                    f"<b>📄 {state.get('delivery_date', 'Сегодня')} {state['time']}</b>\n"
                    f"────────────────────────\n"
                    f"📞 <a href='tel:{state['phone']}'>Номер телефона: {state['phone']}</a>\n"
                    f"🏠 Адрес: <code>{state['address']}</code>\n"
                    f"⏰ Время доставки: <b>{state['time']}</b>\n"
                    f"📅 Дата: <b>{state.get('delivery_date', 'Сегодня')}</b>\n"
                    f"📍 Район: <i>{state['delivery_zone'].capitalize() if state['delivery_zone'] else 'Не указан'}</i>\n"
                    f"🚚 Доставка: <b>{state.get('delivery_price', 0):,} ₽</b>\n"
                    f"────────────────────────\n"
                    f"<b>📋 СОСТАВ ЗАКАЗА:</b>\n"
                )

                for idx, item in enumerate(state["items"], start=1):
                    name = item["name"]
                    qty = item["qty"]
                    comment = item["comment"] if item["comment"] else ""
                    price_per_unit = item.get("source_price")
                    if price_per_unit is None:
                        menu_item = next((i for i in MENU_ITEMS if i["name"] == item["name"]), None)
                        price_per_unit = menu_item["price"] if menu_item else 0
                    line_total = price_per_unit * qty

                    html_receipt += (
                        f"\n<b>{idx}. {name}</b> ×{qty}\n"
                        f"   💰 <i>{line_total:,} ₽</i>"
                    )
                    if comment:
                        html_receipt += f"   ⚠️ <s>{comment}</s>"

                total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))
                html_receipt += (
                    f"\n────────────────────────\n"
                    f"💸 <b>ИТОГО: {total:,} ₽</b>\n"
                    f"────────────────────────\n"
                )


                # Отправляем чек с кнопкой
                msg = await bot_app.send_message(
                    chat_id=WORK_GROUP,
                    reply_to_message_id=THREAD_FUTURE_ID,
                    text=f"{html_receipt}",
                    reply_markup=keyboard
                )
                logging.info(f"📄 Чек с кнопкой отправлен в топик 'Будущие' (ID: {msg.id})")
            except Exception as e:
                logging.error(f"❌ Ошибка отправки чека в Telegram: {e}")

        # === Удаляем из состояния ===
        del USER_EDIT_STATE[user_id]

        # === Обновляем историю пользователя ===
        orders = load_orders()
        user_orders = orders.get(str(user_id), [])
        user_orders.append(order_obj)
        orders[str(user_id)] = user_orders
        save_orders(orders)

    elif data == "add_item":
        user_id = callback.from_user.id
        if user_id not in USER_EDIT_STATE:
            initialize_user_state(user_id)

        # ✅ Очищаем temp_cart при открытии меню добавления
        USER_EDIT_STATE[user_id]["temp_cart"] = []

        await show_categories(callback)
        await callback.answer()

    elif data == "remove_item":
        user_id = callback.from_user.id
        if user_id not in USER_EDIT_STATE:
            initialize_user_state(user_id)
        state = USER_EDIT_STATE[user_id]

        if not state.get("items"):
            await callback.answer("В заказе нет блюд.")
            return

        # Показываем текущие позиции с количеством
        keyboard = []
        for item in state["items"]:
            label = f"{item['name']} (x{item['qty']})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"remove_{item['name']}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_order")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await callback.message.edit_text("Выберите позицию для удаления:", reply_markup=reply_markup)
            await callback.answer()
        except Exception as e:
            logging.error(f"❌ Ошибка при открытии удаления: {e}")
            await callback.answer("Ошибка открытия меню удаления")

    elif data.startswith("remove_"):
        user_id = callback.from_user.id
        item_name = data.replace("remove_", "")
        state = USER_EDIT_STATE.get(user_id)
        if not state:
            await callback.answer("❌ Сессия истекла")
            return

        items = state["items"]
        item = next((it for it in items if it["name"] == item_name), None)
        if not item:
            await callback.answer("Позиция не найдена.")
            return

        if item["qty"] > 1:
            item["qty"] -= 1
            await callback.answer(f"➖ Уменьшено: {item_name} (осталось x{item['qty']})")
        else:
            items.remove(item)
            await callback.answer(f"🗑️ Удалено: {item_name}")

        # Обновляем основное сообщение с заказом
        first_name = callback.from_user.first_name
        await update_order_message(user_id, first_name)

        # ⬇️ ВАЖНО: перерисовываем и меню удаления!
        if items:  # если ещё есть позиции
            keyboard = []
            for it in items:
                label = f"{it['name']} (x{it['qty']})"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"remove_{it['name']}")])
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_order")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await callback.message.edit_text("Выберите позицию для удаления:", reply_markup=reply_markup)
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logging.error(f"❌ Ошибка при обновлении меню удаления: {e}")
        else:
            # Если больше нет позиций — возвращаемся к заказу
            try:
                await callback.message.edit_text("✅ Все позиции удалены.", reply_markup=None)
                await asyncio.sleep(1)
                await update_order_message(user_id, first_name)
            except Exception as e:
                logging.error(f"❌ Ошибка при выходе из удаления: {e}")


    elif data == "back_to_order":
        user_id = callback.from_user.id
        first_name = callback.from_user.first_name
        await update_order_message(user_id, first_name)
        await callback.answer()

    elif data.startswith("cat_"):
        category = data.replace("cat_", "")
        USER_EDIT_STATE[user_id]["last_category"] = category
        await show_dishes_by_category(user_id, category)
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

        user_id = callback.from_user.id
        if user_id not in USER_EDIT_STATE:
            initialize_user_state(user_id)

        temp_cart = USER_EDIT_STATE[user_id]["temp_cart"]
        existing = next((it for it in temp_cart if it["name"] == item["name"]), None)
        if existing:
            existing["qty"] += 1
        else:
            # ✅ Добавляем source_price при создании элемента
            temp_cart.append({
                "name": item["name"],
                "qty": 1,
                "comment": "",
                "source_price": item["price"]  # ← Ключевое исправление!
            })

        category = USER_EDIT_STATE[user_id].get("last_category")
        if not category:
            return

        await show_dishes_by_category(user_id, category)


    elif data == "back_to_categories":
        await show_categories(callback)
        await callback.answer()

    elif data == "finish_edit":
        user_id = callback.from_user.id
        state = USER_EDIT_STATE.get(user_id)
        if not state:
            return

        # Применяем всё из temp_cart в основной заказ
        temp_cart = state.get("temp_cart", [])
        cart = state.setdefault("items", [])

        for new_item in temp_cart:
            existing = next((it for it in cart if it["name"] == new_item["name"]), None)
            if existing:
                existing["qty"] += new_item["qty"]
            else:
                # ✅ Копируем source_price
                cart.append(new_item.copy())  # ← .copy() сохранит все поля

        # Очищаем буфер
        state["temp_cart"] = []

        # Обновляем сообщение
        first_name = callback.from_user.first_name
        await update_order_message(user_id, first_name)
        await callback.answer("✅ Изменения применены")

    elif data == "edit_zone":
        await show_delivery_zones(callback.message)
        await callback.answer()

    elif data.startswith("zone_"):
        zone = data.replace("zone_", "")
        USER_EDIT_STATE[user_id]["delivery_zone"] = zone
        await update_order_message(user_id, callback.from_user.first_name)
        await callback.answer(f"Район выбран: {zone.capitalize()}")

    elif data.startswith("print_future_"):
        target_user_id = int(data.replace("print_future_", ""))

        # Ищем заказ в базе будущих заказов
        future_orders = load_future_orders()
        order = next((ord for ord in future_orders if ord["user_id"] == target_user_id), None)

        if not order:
            await callback.answer("❌ Заказ не найден в базе будущих заказов.")
            logging.warning(f"❌ Заказ не найден в базе: user_id={target_user_id}")
            return

        # Формируем state для печати
        state_for_print = {
            "items": order["items"],
            "phone": order["phone"],
            "address": order["address"],
            "time": order["time"],
            "delivery_date": order["delivery_date"],
            "delivery_zone": order["delivery_zone"],
            "delivery_price": order["delivery_price"],
            "temp_cart": [],
            "awaiting_edit_order": False
        }

        try:
            print_receipt_html(state_for_print)
            await callback.answer("🖨️ Чек отправлен на печать!")
            logging.info(f"🖨️ Чек напечатан по кнопке (из базы): user_id={target_user_id}, заказ №{len(load_orders()) + 1}")
        except Exception as e:
            await callback.answer("❌ Ошибка печати.")
            logging.error(f"❌ Ошибка печати по кнопке: {e}")

    elif data == "print_receipt":
        state = USER_EDIT_STATE.get(user_id)
        if not state:
            await callback.answer("Нет данных для печати.")
            return

        total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))

        receipt_lines = []
        receipt_lines.append("   Магазин \"Орхидея\"")
        receipt_lines.append("-" * 22)
        receipt_lines.append(f"Заказ №{len(load_orders()) + 1:06d}")
        now = datetime.now().strftime("%d.%m %H:%M")
        receipt_lines.append(f"Время: {now}")
        receipt_lines.append("-" * 22)

        if state["phone"]:
            receipt_lines.append(f"Тел: {state['phone']}")
        if state["address"]:
            receipt_lines.append(f"Адрес: {state['address']}")
        if state["time"]:
            receipt_lines.append(f"Время: {state['time']}")
        if state.get("delivery_date"):
            receipt_lines.append(f"Дата: {state['delivery_date']}")
        if state["delivery_zone"]:
            receipt_lines.append(f"Район: {state['delivery_zone'].capitalize()}")
        receipt_lines.append(f"Доставка: {state.get('delivery_price', 0):>6} ₽")

        receipt_lines.append("-" * 22)

        # Добавляем позиции с нумерацией, количеством и ценой на новых строках
        for idx, item in enumerate(state["items"], start=1):
            name = item["name"]
            qty = item["qty"]
            comment = item["comment"] if item["comment"] else ""
            price_per_unit = item.get("source_price")
            if price_per_unit is None:
                menu_item = next((i for i in MENU_ITEMS if i["name"] == item["name"]), None)
                price_per_unit = menu_item["price"] if menu_item else 0
            line_total = price_per_unit * item["qty"]

            # Название блюда
            item_line = f"{idx}. {name}"
            receipt_lines.append(item_line)

            # Количество на новой строке
            qty_line = f"   Кол-во: {qty} шт."
            receipt_lines.append(qty_line)

            # Цена на новой строке
            price_line = f"   Цена: {line_total:,}".replace(",", " ") + " ₽"
            receipt_lines.append(price_line)

            # Комментарий (если есть)
            if comment:
                receipt_lines.append(f"   ⚠️{comment.capitalize()}")

            # Разделитель между позициями
            if idx < len(state["items"]):
                receipt_lines.append("-" * 22)

        receipt_lines.append("-" * 22)

        # Итого
        total_str = f"{total:,}".replace(",", " ") + " ₽"
        receipt_lines.append(f"ИТОГО:     {total_str:>8}")

        receipt_lines.append("-" * 22)
        receipt_lines.append("Спасибо за заказ!")
        receipt_lines.append("Приходите ещё!")

        receipt_text = "\n".join(receipt_lines)

        try:
            print_receipt_html(state)
            await callback.answer("🖨️ Чек отправлен на печать!")
        except Exception as e:
            await callback.answer("❌ Ошибка печати.")
            logging.error(f"Ошибка печати: {e}")

        try:
            await bot_app.send_message(
                chat_id=WORK_GROUP,
                text=f"🖨️ <b>Чек для печати (58мм)</b>:\n\n<pre>{receipt_text}</pre>"
            )
        except Exception as e:
            logging.error(f"Ошибка отправки чека в Telegram: {e}")

def generate_receipt_text(state):
    """
    Генерирует текст чека как строку.
    Используется для отправки в Telegram.
    """
    total = calculate_total(state["items"], delivery_price=state.get("delivery_price", 0))
    lines = []
    lines.append("   Магазин \"Орхидея\"")
    lines.append("-" * 22)
    lines.append(f"Заказ №{len(load_orders()) + 1:06d}")
    now = datetime.now().strftime("%d.%m %H:%M")
    lines.append(f"Время: {now}")
    lines.append("-" * 22)

    if state["phone"]:
        lines.append(f"Тел: {state['phone']}")
    if state["address"]:
        lines.append(f"Адрес: {state['address']}")
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

    order_num = len(load_orders()) + 1
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
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe")
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

async def show_categories(callback_query):
    """
    Показывает категории, редактируя текущее сообщение.
    """
    user_id = callback_query.from_user.id
    categories = sorted(list(set(item["category"] for item in MENU_ITEMS)))
    keyboard = []
    for cat in categories:
        keyboard.append([InlineKeyboardButton(cat, callback_data=f"cat_{cat}")])
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="finish_edit")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await callback_query.message.edit_text("Выберите категорию:", reply_markup=reply_markup)
        # Сохраняем ID сообщения (уже есть в order_message_id)
        USER_EDIT_STATE[user_id]["category_message_id"] = callback_query.message.id
        logging.info(f"📌 Отредактировано сообщение для категорий: {callback_query.message.id}")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            await callback_query.message.edit_text("Выберите категорию: ", reply_markup=reply_markup)
        else:
            logging.error(f"❌ Ошибка при редактировании: {e}")

async def show_dishes_by_category(user_id: int, category: str):
    """
    Показывает блюда по категории, редактируя сохранённое сообщение.
    """
    state = USER_EDIT_STATE.get(user_id)
    if not state:
        return

    temp_cart = state.get("temp_cart", [])
    dishes = [item for item in MENU_ITEMS if item["category"] == category]
    keyboard = []
    row = []

    for item in dishes:
        cart_item = next((it for it in temp_cart if it["name"] == item["name"]), None)
        qty = cart_item["qty"] if cart_item else 0

        btn_text = f"{item['name']}"
        if qty > 0:
            btn_text = f"{item['name']} (x{qty})"
        btn_text += f" — {item['price']}₽"

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

    message_id = state.get("category_message_id")
    if not message_id:
        logging.warning(f"❌ Нет category_message_id для user_id={user_id}")
        return

    try:
        await bot_app.edit_message_text(
            chat_id=WORK_GROUP,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup
        )
        logging.info(f"✅ Обновлено сообщение: {message_id}")
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

            if input_clean == db_clean:
                matches.append((zone, price, street_db))

        logging.info(f"🔍 Поиск по адресу: '{address}' → clean='{input_clean}'")
        logging.info(f"   Найдено совпадений: {len(matches)}")
        for zone, price, street_db in matches:
            logging.info(f"   → Зона: {zone}, Цена: {price} ₽, Улица БД: {street_db}")

        return matches

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

    # Создаём пустые файлы, если не существуют
    if not os.path.exists(ACTIVE_ORDERS_JSON):
        save_active_orders([])
    if not os.path.exists(FUTURE_ORDERS_JSON):
        save_future_orders([])

    bot_app.run()

    logging.info("🚀 Бот успешно запущен и готов к работе.")