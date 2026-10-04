import datetime
import html
import json
import logging
import os
import platform
import sqlite3
import sys
import shutil
import threading
import time
import urllib.parse
import urllib.request
import telebot
from telebot import types


def telegram_message_to_html(message):
    """Зберігає форматування Telegram-повідомлення у вигляді HTML."""
    text = message.text if message.text is not None else (message.caption or "")
    entities = message.entities if message.text is not None else (message.caption_entities or [])
    if not text:
        return ""
    if not entities:
        return html.escape(text)
    boundary = {0: 0}
    u16 = 0
    for i, ch in enumerate(text):
        u16 += 2 if ord(ch) > 0xFFFF else 1
        boundary[u16] = i + 1
    def idx(v):
        if v in boundary:
            return boundary[v]
        return boundary[max((k for k in boundary if k <= v), default=0)]
    tags = {
        "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"), "strikethrough": ("<s>", "</s>"),
        "spoiler": ("<tg-spoiler>", "</tg-spoiler>"), "code": ("<code>", "</code>"),
        "pre": ("<pre>", "</pre>"), "blockquote": ("<blockquote>", "</blockquote>"),
        "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
    }
    events=[]
    for order, e in enumerate(entities):
        typ=getattr(e,"type","")
        start=idx(int(getattr(e,"offset",0))); end=idx(int(getattr(e,"offset",0))+int(getattr(e,"length",0)))
        if end<=start: continue
        if typ=="text_link":
            opening=f'<a href="{html.escape(getattr(e,"url","") or "", quote=True)}">'; closing="</a>"
        elif typ=="text_mention" and getattr(getattr(e,"user",None),"id",None):
            opening=f'<a href="tg://user?id={e.user.id}">'; closing="</a>"
        elif typ=="custom_emoji" and getattr(e,"custom_emoji_id",None):
            opening=f'<tg-emoji emoji-id="{html.escape(str(e.custom_emoji_id), quote=True)}">'; closing="</tg-emoji>"
        elif typ=="pre" and getattr(e,"language",""):
            opening=f'<pre><code class="language-{html.escape(e.language, quote=True)}">'; closing="</code></pre>"
        elif typ in tags:
            opening,closing=tags[typ]
        else:
            continue
        events.append((start,1,-end,order,opening)); events.append((end,0,-start,order,closing))
    events.sort(key=lambda x:(x[0],x[1],x[2],x[3]))
    out=[]; cursor=0
    for pos,_,_,_,tag in events:
        pos=max(0,min(len(text),pos))
        if pos>cursor:
            out.append(html.escape(text[cursor:pos])); cursor=pos
        out.append(tag)
    if cursor<len(text): out.append(html.escape(text[cursor:]))
    return "".join(out)


def stored_message_html(item):
    if not isinstance(item,dict): return ""
    rich=(item.get("html") or "").strip()
    if rich: return rich
    text=(item.get("text") or "").strip()
    return html.escape(text) if text else ""


# Налаштування логування: INFO/WARNING -> stdout, ERROR/CRITICAL -> stderr.
# У стандартному IDLE stderr показується червоним, тому помилки одразу видно.
def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    class MaxInfoFilter(logging.Filter):
        def filter(self, record):
            return logging.INFO <= record.levelno < logging.ERROR

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(MaxInfoFilter())
    stdout_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    ))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    ))

    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)

setup_logging()

# =====================================================================
# 💾 РОБОТА З JSON У ПАПЦІ РЕЗЕРВНОГО КОПІЮВАННЯ
# =====================================================================

# Папка резервного копіювання завжди знаходиться поруч із bot.py,
# незалежно від того, з якої робочої папки запущено програму.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_BACKUP = os.path.join(BASE_DIR, "Резервне копіювання")
os.makedirs(DIR_BACKUP, exist_ok=True)

# Один постійний файл зі збереженими даними.
BACKUP_FILE_PATH = os.path.join(DIR_BACKUP, "bot_data.json")

# SQLite зберігається ТІЛЬКИ в папці резервного копіювання.
SQLITE_FILE_PATH = os.path.join(DIR_BACKUP, "bot_data.sqlite3")


# ── Підрозділ: SQLite резервна копія ─────────────────────────────

def init_sqlite_backup():
    """Створює/оновлює SQLite-файл у папці резервного копіювання."""
    try:
        with sqlite3.connect(SQLITE_FILE_PATH) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_data (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        logging.info("SQLite резервна копія готова: %s", SQLITE_FILE_PATH)
    except Exception as e:
        logging.error("Помилка SQLite резервної копії: %s", e)


def save_sqlite_backup():
    """Записує актуальний JSON-стан у SQLite як додаткову резервну копію."""
    try:
        payload = {
            "HOMEWORK_STORAGE": HOMEWORK_STORAGE,
            "TEACHER_MESSAGE_STORAGE": TEACHER_MESSAGE_STORAGE,
            "duty_ratings": duty_ratings,
            "current_duty_person": current_duty_person,
            "USERS_STORAGE": USERS_STORAGE,
            "ADMIN_ACTION_LOG": ADMIN_ACTION_LOG,
            "poll_data": globals().get("poll_data", {}),
            "CUSTOM_POLL": globals().get("CUSTOM_POLL", {}),
            "PINNED_IMPORTANT_MESSAGE": globals().get("PINNED_IMPORTANT_MESSAGE", {}),
            "ADMIN_SETTINGS": globals().get("ADMIN_SETTINGS", {}),
            "ADMIN_ROLES": globals().get("ADMIN_ROLES", {}),
            "SCHEDULED_BROADCASTS": globals().get("SCHEDULED_BROADCASTS", []),
            "ADMIN_EVENT_LOG": globals().get("ADMIN_EVENT_LOG", []),
            "HAS_BELLS_CHANGES_TODAY": globals().get("HAS_BELLS_CHANGES_TODAY", False),
            "BELLS_CHANGES_TODAY_YES_TEXT": globals().get("BELLS_CHANGES_TODAY_YES_TEXT", ""),
            "HAS_BELLS_CHANGES_TOMORROW": globals().get("HAS_BELLS_CHANGES_TOMORROW", False),
            "BELLS_CHANGES_TOMORROW_YES_TEXT": globals().get("BELLS_CHANGES_TOMORROW_YES_TEXT", ""),
        }
        with sqlite3.connect(SQLITE_FILE_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_data (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            for key, value in payload.items():
                conn.execute(
                    "INSERT OR REPLACE INTO bot_data(key, value) VALUES (?, ?)",
                    (key, json.dumps(value, ensure_ascii=False))
                )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.execute("PRAGMA journal_mode=DELETE")
        logging.info("SQLite резервну копію оновлено.")
    except Exception as e:
        logging.error("Помилка запису SQLite резервної копії: %s", e)


init_sqlite_backup()



# ── Підрозділ: Завантаження даних ─────────────────────────────────

def load_data():
    """Завантажує дані з bot_data.json.

    Спочатку шукає bot_data.json поруч із bot.py, а якщо його немає —
    використовує файл із папки 'Резервне копіювання'.
    """
    json_candidates = [
        os.path.join(BASE_DIR, "bot_data.json"),
        BACKUP_FILE_PATH,
    ]

    for json_path in json_candidates:
        if not os.path.exists(json_path):
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                logging.info("Дані завантажено з JSON: %s", json_path)
                return data

            logging.error("JSON-файл має неправильний формат: %s", json_path)
        except Exception as e:
            logging.error("Помилка читання JSON %s: %s", json_path, e)

    logging.info("bot_data.json не знайдено — використовуються порожні дані.")
    return {}


# ── Підрозділ: Збереження JSON ───────────────────────────────────

def save_data_to_file():
    """Зберігає актуальні дані у JSON-файл всередині папки 'Резервне копіювання'."""
    data = {
        "HOMEWORK_STORAGE": HOMEWORK_STORAGE,
        "TEACHER_MESSAGE_STORAGE": TEACHER_MESSAGE_STORAGE,
        "duty_ratings": duty_ratings,
        "current_duty_person": current_duty_person,
        "USERS_STORAGE": USERS_STORAGE,
        "ADMIN_ACTION_LOG": ADMIN_ACTION_LOG,
        "HAS_CHANGES_TODAY": HAS_CHANGES_TODAY,
        "CHANGES_TODAY_YES_TEXT": CHANGES_TODAY_YES_TEXT,
        "HAS_CHANGES_TOMORROW": HAS_CHANGES_TOMORROW,
        "CHANGES_TOMORROW_YES_TEXT": CHANGES_TOMORROW_YES_TEXT,
        "HAS_BELLS_CHANGES_TODAY": HAS_BELLS_CHANGES_TODAY,
        "BELLS_CHANGES_TODAY_YES_TEXT": BELLS_CHANGES_TODAY_YES_TEXT,
        "HAS_BELLS_CHANGES_TOMORROW": HAS_BELLS_CHANGES_TOMORROW,
        "BELLS_CHANGES_TOMORROW_YES_TEXT": BELLS_CHANGES_TOMORROW_YES_TEXT,
        "CHANGE_STATUS_CONFIG_VERSION": CHANGE_STATUS_CONFIG_VERSION,
        "CHANGE_STATUS_DATES": CHANGE_STATUS_DATES,
        "poll_data": poll_data,
        "CUSTOM_POLL": globals().get("CUSTOM_POLL", {}),
        "PINNED_IMPORTANT_MESSAGE": PINNED_IMPORTANT_MESSAGE,
        "ADMIN_SETTINGS": ADMIN_SETTINGS,
        "ADMIN_ROLES": ADMIN_ROLES,
        "SCHEDULED_BROADCASTS": SCHEDULED_BROADCASTS,
        "ADMIN_EVENT_LOG": ADMIN_EVENT_LOG,
    }
    
    try:
        # Записуємо спочатку у тимчасовий файл, щоб при раптовому
        # вимкненні бота основний JSON не залишився пошкодженим.
        temp_path = BACKUP_FILE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, BACKUP_FILE_PATH)
        logging.info(f"Дані збережено: {BACKUP_FILE_PATH}")
        save_sqlite_backup()
    except Exception as e:
        logging.error(f"Помилка збереження JSON у папку: {e}")


# =====================================================================
# 🔐 .ENV ТА АДМІНІСТРАТОРИ — ВИЗНАЧАЄМО ДО БУДЬ-ЯКОГО ВИКОРИСТАННЯ
# =====================================================================
# Важливо: ADMIN_IDS потрібен нижче під час завантаження ADMIN_ROLES,
# тому його треба створити ДО saved_data / ADMIN_ROLES.

def load_dotenv_simple():
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception as e:
        logging.error("Помилка читання .env: %s", e)

load_dotenv_simple()

TOKEN = os.getenv("BOT_TOKEN", "")
try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except ValueError:
    GROUP_ID = 0
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

# Працює і з одним ADMIN_ID, і з додатковим списком ADMIN_IDS.
ADMIN_IDS = {ADMIN_ID} if ADMIN_ID else set()
for _raw_admin in os.getenv("ADMIN_IDS", "").split(","):
    _raw_admin = _raw_admin.strip()
    if not _raw_admin:
        continue
    try:
        ADMIN_IDS.add(int(_raw_admin))
    except ValueError:
        logging.warning("Некоректний ADMIN_IDS: %s", _raw_admin)

bot = telebot.TeleBot(TOKEN)

# Завантажуємо збережені дані при запуску бота
saved_data = load_data()

# ⚙️ Постійні налаштування адміністратора. Зберігаються у bot_data.json.
ADMIN_SETTINGS = saved_data.get("ADMIN_SETTINGS", {})
if not isinstance(ADMIN_SETTINGS, dict):
    ADMIN_SETTINGS = {}
ADMIN_SETTINGS.setdefault("auto_backup", True)
ADMIN_SETTINGS.setdefault("weather_cache_minutes", 10)
ADMIN_SETTINGS.setdefault("poll_auto_close", True)
ADMIN_SETTINGS.setdefault("poll_deadline_hours", 24)
ADMIN_SETTINGS.setdefault("collect_statistics", True)
ADMIN_SETTINGS.setdefault("backup_keep_count", 10)
ADMIN_SETTINGS.setdefault("log_level", "INFO")
if not isinstance(ADMIN_SETTINGS.get("menu_visible"), dict):
    ADMIN_SETTINGS["menu_visible"] = {}
if not isinstance(ADMIN_SETTINGS.get("menu_order"), list):
    ADMIN_SETTINGS["menu_order"] = []

# Розширені адміністративні дані. Усе зберігається у bot_data.json.
ADMIN_ROLES = saved_data.get("ADMIN_ROLES", {})
if not isinstance(ADMIN_ROLES, dict):
    ADMIN_ROLES = {}
for _aid in list(ADMIN_IDS):
    ADMIN_ROLES.setdefault(str(_aid), "owner")
for _raw_admin in list(ADMIN_ROLES.keys()):
    try:
        ADMIN_IDS.add(int(_raw_admin))
    except (TypeError, ValueError):
        pass

SCHEDULED_BROADCASTS = saved_data.get("SCHEDULED_BROADCASTS", [])
if not isinstance(SCHEDULED_BROADCASTS, list):
    SCHEDULED_BROADCASTS = []

ADMIN_EVENT_LOG = saved_data.get("ADMIN_EVENT_LOG", [])
if not isinstance(ADMIN_EVENT_LOG, list):
    ADMIN_EVENT_LOG = []

DEFAULT_MENU_ORDER = [
    "school",
    "canteen",
    "duty",
    "schedule",
    "schedule_changes",
    "bells_changes",
    "homework",
    "poll",
    "teacher",
    "important",
    "holiday_calendar",
    "global_search",
    "weather",
]
MENU_LABELS = {
    "school": "🏫 Коли ми йдемо в школу?",
    "canteen": "🍽 Меню харчування на завтра",
    "duty": "📢 Хто чергує?",
    "teacher": "📢 Повідомлення від вчителя",
    "important": "📌 Важливі повідомлення",
    "homework": "📚 Домашнє завдання",
    "holiday_calendar": "🎉 Календар свят",
    "global_search": "🔎 Глобальний пошук",
    "weather": "🌤 Погода в Охтирці",
    "poll": "🗳 Опитування на вибір чергового",
    "schedule": "📚 Розклад уроків",
    "schedule_changes": "🔔 Зміни розкладу уроків",
    "bells_changes": "🔔 Зміни розкладу дзвінків",
}
for _key in DEFAULT_MENU_ORDER:
    ADMIN_SETTINGS["menu_visible"].setdefault(_key, True)
if not ADMIN_SETTINGS["menu_order"]:
    ADMIN_SETTINGS["menu_order"] = list(DEFAULT_MENU_ORDER)
else:
    # Зберігаємо лише відомі кнопки, але встановлюємо новий порядок:
    # розклад — вище повідомлень вчителя, пошук і погода — в кінці.
    existing = [x for x in ADMIN_SETTINGS["menu_order"] if x in DEFAULT_MENU_ORDER]
    ordered = [x for x in DEFAULT_MENU_ORDER if x in existing]
    missing = [x for x in DEFAULT_MENU_ORDER if x not in ordered]
    ADMIN_SETTINGS["menu_order"] = ordered + missing

ROLE_PERMISSIONS = {
    "owner": {"content", "users", "control", "system"},
    "content": {"content"},
    "moderator": {"users", "control"},
}

# ── Підрозділ: Адміністратори та права доступу ─────────────────

def admin_role(user_id):
    return ADMIN_ROLES.get(str(user_id), "owner" if int(user_id) in ADMIN_IDS else "")

def admin_has_permission(user_id, section):
    if int(user_id) not in ADMIN_IDS:
        return False
    return section in ROLE_PERMISSIONS.get(admin_role(user_id), set()) or admin_role(user_id) == "owner"

def admin_setting(name, default=None):
    return ADMIN_SETTINGS.get(name, default)

def set_admin_setting(name, value):
    ADMIN_SETTINGS[name] = value
    save_data_to_file()

# Окреме закріплене/важливе повідомлення, яке адмін може змінювати
# без прив'язки до конкретного предмета чи вчителя.
PINNED_IMPORTANT_MESSAGE = saved_data.get("PINNED_IMPORTANT_MESSAGE", {})
if not isinstance(PINNED_IMPORTANT_MESSAGE, dict):
    PINNED_IMPORTANT_MESSAGE = {}
PINNED_IMPORTANT_MESSAGE.setdefault("text", "")
PINNED_IMPORTANT_MESSAGE.setdefault("html", "")
PINNED_IMPORTANT_MESSAGE.setdefault("created_at", "")

# =====================================================================
# 🔄 ВІДНОВЛЕННЯ ЗМІННИХ ЗІ ЗБЕРЕЖЕНИХ ДАНИХ
# =====================================================================

HOMEWORK_STORAGE = saved_data.get("HOMEWORK_STORAGE", {})
if not isinstance(HOMEWORK_STORAGE, dict):
    HOMEWORK_STORAGE = {}

TEACHER_MESSAGE_STORAGE = saved_data.get("TEACHER_MESSAGE_STORAGE", {})

# Новий формат: окреме повідомлення для класної керівнички
# та окреме повідомлення для кожного предмета.
# Старий формат {"text": ..., "photo": ...} автоматично переносимо
# до "class_teacher", щоб старе повідомлення не загубилося.
if not isinstance(TEACHER_MESSAGE_STORAGE, dict):
    TEACHER_MESSAGE_STORAGE = {}

if "class_teacher" not in TEACHER_MESSAGE_STORAGE and "subjects" not in TEACHER_MESSAGE_STORAGE:
    old_text = (TEACHER_MESSAGE_STORAGE.get("text") or "").strip()
    old_photo = TEACHER_MESSAGE_STORAGE.get("photo")
    if old_text and old_text != "Наразі немає нових повідомлень від вчителів.":
        TEACHER_MESSAGE_STORAGE = {
            "class_teacher": {"text": old_text, "photo": old_photo, "important": False, "pinned": False},
            "subjects": {},
        }
    else:
        TEACHER_MESSAGE_STORAGE = {
            "class_teacher": {"text": "", "photo": None, "important": False, "pinned": False},
            "subjects": {},
        }
else:
    TEACHER_MESSAGE_STORAGE.setdefault("class_teacher", {"text": "", "photo": None, "important": False, "pinned": False})
    TEACHER_MESSAGE_STORAGE.setdefault("subjects", {})
    if not isinstance(TEACHER_MESSAGE_STORAGE["subjects"], dict):
        TEACHER_MESSAGE_STORAGE["subjects"] = {}

# Міграція старих повідомлень: додаємо прапорець важливості.
if isinstance(TEACHER_MESSAGE_STORAGE.get("class_teacher"), dict):
    TEACHER_MESSAGE_STORAGE["class_teacher"].setdefault("important", False)
for _subject, _item in TEACHER_MESSAGE_STORAGE.get("subjects", {}).items():
    if isinstance(_item, dict):
        _item.setdefault("important", False)
        _item.setdefault("pinned", False)

duty_ratings = saved_data.get("duty_ratings", {})
if not isinstance(duty_ratings, dict):
    duty_ratings = {}

current_duty_person = saved_data.get("current_duty_person", "Ганна Міронова")

# Нові постійні дані: користувачі, прочитані повідомлення та журнал адмін-дій.
USERS_STORAGE = saved_data.get("USERS_STORAGE", {})
if not isinstance(USERS_STORAGE, dict):
    USERS_STORAGE = {}
ADMIN_ACTION_LOG = saved_data.get("ADMIN_ACTION_LOG", [])
if not isinstance(ADMIN_ACTION_LOG, list):
    ADMIN_ACTION_LOG = []
BOT_STARTED_AT = datetime.datetime.now()

# ── Підрозділ: Користувачі, статистика та журнал ───────────────

def record_admin_event(event_type, text, user_id=None):
    ADMIN_EVENT_LOG.append({
        "type": event_type,
        "text": text,
        "user_id": int(user_id) if user_id is not None else None,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    del ADMIN_EVENT_LOG[:-100]


def register_user(user):
    """Реєструє користувача та зберігає його налаштування між перезапусками."""
    uid = str(user.id)
    is_new = uid not in USERS_STORAGE
    old = USERS_STORAGE.get(uid, {}) if isinstance(USERS_STORAGE.get(uid, {}), dict) else {}
    USERS_STORAGE[uid] = {
        "first_name": user.first_name or old.get("first_name", ""),
        "last_name": user.last_name or old.get("last_name", ""),
        "username": user.username or old.get("username", ""),
        "last_seen": datetime.datetime.now().isoformat(timespec="seconds"),
        "stats": old.get("stats", {}) if isinstance(old.get("stats", {}), dict) else {},
    }
    if is_new:
        record_admin_event("user", f"Новий користувач: {(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip(), user.id)
    # Не записуємо в JSON кожен клік; оновлення відбувається при старті/діях.

def track_user_event(user_id, event):
    """Зберігає просту статистику використання розділів користувачем."""
    if not admin_setting("collect_statistics", True):
        return
    uid = str(user_id)
    user = USERS_STORAGE.setdefault(uid, {})
    stats = user.setdefault("stats", {})
    stats[event] = int(stats.get(event, 0)) + 1


def important_message_count():
    # Окремо від «📌 Закріпленого повідомлення»: тут рахуємо лише
    # повідомлення вчителів, які адмін позначив як важливі.
    return sum(1 for _, _, item in _teacher_message_items() if item.get("important", False)) if "_teacher_message_items" in globals() else 0


def pinned_message_count():
    return sum(1 for _, _, item in _teacher_message_items() if item.get("pinned", False)) if "_teacher_message_items" in globals() else 0


def log_admin_action(user_id, action):
    if int(user_id) not in ADMIN_IDS:
        return
    u = USERS_STORAGE.get(str(user_id), {})
    admin_name = " ".join(x for x in (u.get("first_name", ""), u.get("last_name", "")) if x).strip()
    ADMIN_ACTION_LOG.append({
        "admin_id": int(user_id),
        "admin_name": admin_name or f"ID {user_id}",
        "action": action,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    record_admin_event("admin", action, user_id)
    del ADMIN_ACTION_LOG[:-200]
    save_data_to_file()


# =====================================================================
# 🔄 СИНХРОНІЗАЦІЯ JSON → SQLite ПІСЛЯ ЗАПУСКУ
# =====================================================================
# JSON є джерелом актуальних даних. Після його завантаження всі
# HOMEWORK_STORAGE / TEACHER_MESSAGE_STORAGE / duty_ratings /
# current_duty_person автоматично записуються в SQLite.
save_sqlite_backup()

# =====================================================================
# ⚙️ ОСНОВНІ НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ БОТА
# =====================================================================

# Логування вже налаштоване вище: INFO -> stdout, ERROR/CRITICAL -> stderr.


# =====================================================================
# 🌤 ПОГОДА В ОХТИРЦІ — Open-Meteo + кешування
# =====================================================================
# Координати центру Охтирки: приблизно 50.3167 N, 34.9000 E.
WEATHER_LATITUDE = 50.3167
WEATHER_LONGITUDE = 34.9000
WEATHER_TIMEZONE = "Europe/Kyiv"
WEATHER_CACHE_TTL = 10 * 60  # значення за замовчуванням; адмін може змінити через налаштування
WEATHER_CACHE = {"data": None, "updated_at": 0.0}
WEATHER_CACHE_LOCK = threading.Lock()


# ── Підрозділ: Погода та кеш ────────────────────────────────────

def weather_description(code):
    """Перекладає WMO weather code у зрозумілий український опис та emoji."""
    descriptions = {
        0: ("☀️", "Ясно"),
        1: ("🌤", "Переважно ясно"),
        2: ("⛅", "Мінлива хмарність"),
        3: ("☁️", "Похмуро"),
        45: ("🌫", "Туман"),
        48: ("🌫", "Туман з памороззю"),
        51: ("🌦", "Мряка"),
        53: ("🌦", "Мряка"),
        55: ("🌧", "Сильна мряка"),
        56: ("🌧", "Крижана мряка"),
        57: ("🌧", "Сильна крижана мряка"),
        61: ("🌧", "Невеликий дощ"),
        63: ("🌧", "Дощ"),
        65: ("🌧", "Сильний дощ"),
        66: ("🌧", "Крижаний дощ"),
        67: ("🌧", "Сильний крижаний дощ"),
        71: ("🌨", "Невеликий сніг"),
        73: ("🌨", "Сніг"),
        75: ("❄️", "Сильний сніг"),
        77: ("🌨", "Снігові зерна"),
        80: ("🌦", "Невелика злива"),
        81: ("🌦", "Злива"),
        82: ("⛈", "Сильна злива"),
        85: ("🌨", "Невеликий сніговий заряд"),
        86: ("🌨", "Сильний сніговий заряд"),
        95: ("⛈", "Гроза"),
        96: ("⛈", "Гроза з градом"),
        99: ("⛈", "Сильна гроза з градом"),
    }
    return descriptions.get(int(code), ("🌡", "Невідомі погодні умови"))


def _weather_api_url():
    params = {
        "latitude": WEATHER_LATITUDE,
        "longitude": WEATHER_LONGITUDE,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": WEATHER_TIMEZONE,
        "forecast_days": 7,
        "wind_speed_unit": "kmh",
    }
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)


def get_weather_data(force_refresh=False):
    """Повертає погоду з кешу або автоматично оновлює її через Open-Meteo."""
    now = time.time()
    with WEATHER_CACHE_LOCK:
        cached = WEATHER_CACHE.get("data")
        cached_at = WEATHER_CACHE.get("updated_at", 0.0)
        cache_minutes = int(admin_setting("weather_cache_minutes", 10) or 10)
        cache_ttl = max(1, cache_minutes) * 60
        if cached is not None and not force_refresh and now - cached_at < cache_ttl:
            return cached, cached_at, True

    try:
        request = urllib.request.Request(
            _weather_api_url(),
            headers={"User-Agent": "8-G-class-telegram-bot/1.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict) or "current" not in data or "daily" not in data:
            raise ValueError("Погодний API повернув неповні дані")

        fetched_at = time.time()
        with WEATHER_CACHE_LOCK:
            WEATHER_CACHE["data"] = data
            WEATHER_CACHE["updated_at"] = fetched_at
        return data, fetched_at, False
    except Exception as e:
        logging.error("Помилка отримання погоди: %s", e)
        with WEATHER_CACHE_LOCK:
            if WEATHER_CACHE.get("data") is not None:
                return WEATHER_CACHE["data"], WEATHER_CACHE.get("updated_at", 0.0), True
        return None, 0.0, False


def format_weather_message(data, cached_at=0.0):
    current = data.get("current", {})
    daily = data.get("daily", {})
    times = daily.get("time", [])
    codes = daily.get("weather_code", [])
    max_t = daily.get("temperature_2m_max", [])
    min_t = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_probability_max", [])

    current_emoji, current_desc = weather_description(current.get("weather_code", 0))
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")

    def val(value, suffix=""):
        return f"{value:g}{suffix}" if isinstance(value, (int, float)) else "—"

    lines = [
        "🌤 <b>ОХТИРКА</b>",
        "",
        "📅 <b>Сьогодні</b>",
        f"{current_emoji} {current_desc}",
        f"🌡 Зараз: <b>{val(temp, '°C')}</b>",
        f"🫧 Відчувається: {val(feels, '°C')}",
        f"💧 Вологість: {val(humidity, '%')}",
        f"💨 Вітер: {val(wind, ' км/год')}",
    ]

    if len(times) > 1:
        code = codes[1] if len(codes) > 1 else 0
        emoji, desc = weather_description(code)
        high = max_t[1] if len(max_t) > 1 else None
        low = min_t[1] if len(min_t) > 1 else None
        rain = precip[1] if len(precip) > 1 else None
        lines += ["", "📅 <b>Завтра</b>", f"{emoji} {desc}", f"🌡 <b>{val(high, '°C')} / {val(low, '°C')}</b>"]
        if isinstance(rain, (int, float)):
            lines.append(f"🌧 Ймовірність опадів: <b>{val(rain, '%')}</b>")

    lines += ["", "📅 <b>ПРОГНОЗ НА 7 ДНІВ</b>"]
    day_names = ["Сьогодні", "Завтра", "Післязавтра", "Через 3 дні", "Через 4 дні", "Через 5 днів", "Через 6 днів"]
    for i, date_str in enumerate(times[:7]):
        code = codes[i] if i < len(codes) else 0
        emoji, desc = weather_description(code)
        high = max_t[i] if i < len(max_t) else None
        low = min_t[i] if i < len(min_t) else None
        rain = precip[i] if i < len(precip) else None
        try:
            date_label = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m")
        except Exception:
            date_label = str(date_str)
        rain_text = f" • 🌧 {val(rain, '%')}" if isinstance(rain, (int, float)) else ""
        lines.append(f"{emoji} <b>{day_names[i] if i < len(day_names) else date_label}</b> ({date_label}) — {desc}")
        lines.append(f"   🌡 {val(high, '°C')} / {val(low, '°C')}{rain_text}")

    if cached_at:
        updated = datetime.datetime.fromtimestamp(cached_at).strftime("%H:%M")
        lines.extend(["", f"🕒 Дані оновлено: <b>{updated}</b>"])
    return "\n".join(lines)

def weather_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 Оновити", callback_data="weather_refresh"),
        types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"),
    )
    return keyboard


def show_weather(call, force_refresh=False, send_new_message=False):
    """Показує погоду. З меню погода відкривається НОВИМ повідомленням,
    щоб головне меню залишалося на місці. Кнопка «Оновити» редагує вже
    саме повідомлення з погодою, а не головне меню.
    """
    data, cached_at, used_cache = get_weather_data(force_refresh=force_refresh)
    if data is None:
        text = (
            "🌤 <b>ПОГОДА — ОХТИРКА</b>\n\n"
            "❌ Не вдалося отримати погоду зараз.\n"
            "Спробуй натиснути «🔄 Оновити» трохи пізніше."
        )
    else:
        text = format_weather_message(data, cached_at)
        if used_cache:
            text += "\n\nℹ️ Показано кешовані дані, щоб не робити зайвий запит."

    try:
        bot.answer_callback_query(
            call.id,
            "Погоду оновлено 🌤" if not used_cache else "Показую актуальний кеш 🌤"
        )
    except Exception:
        pass

    if send_new_message:
        # ВАЖЛИВО: не чіпаємо повідомлення головного меню.
        bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=weather_keyboard(),
            parse_mode="HTML",
        )
    else:
        # Оновлення стосується тільки повідомлення з погодою.
        safe_edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=weather_keyboard(),
            parse_mode="HTML",
        )


def safe_edit_message_text(text, chat_id, message_id, **kwargs):
    """Безпечно редагує повідомлення.

    Telegram повертає 400 message is not modified, якщо новий текст
    та клавіатура повністю збігаються з поточними. Це не помилка бота,
    тому її тихо ігноруємо, щоб polling не показував ERROR.
    """
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            logging.debug("Повідомлення вже актуальне, редагування не потрібне.")
            return None
        raise


def safe_edit_message_reply_markup(chat_id, message_id, **kwargs):
    """Безпечно оновлює inline-клавіатуру без помилки 400."""
    try:
        return bot.edit_message_reply_markup(chat_id, message_id, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            logging.debug("Клавіатура вже актуальна, оновлення не потрібне.")
            return None
        raise

# Словник для красивого виведення днів тижня українською
WEEKDAYS_UKR = {
    0: "понеділок",
    1: "вівторок",
    2: "середа",
    3: "четвер",
    4: "п’ятниця",
    5: "субота",
    6: "неділя",
}

# Правильні короткі назви днів тижня для кнопок календаря
WEEKDAYS_SHORT_UKR = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Нд",
}

# =====================================================================
# 👥 ДАНІ ПРО УЧНІВ ТА СПИСКИ КЛАСУ
# =====================================================================

ALL_CLASS_STUDENTS = [
    {
        "name": "Оксана Білик",
        "username": None,
        "phone": "+380669071009",
    },
    {
        "name": "Дарина Щербина",
        "username": "@liiytr",
        "phone": "+380501460081",
    },
    {
        "name": "Ільїна Вікторія",
        "username": "@lexikszxprp",
        "phone": "+380990555674",
    },
    {
        "name": "Ганна Міронова",
        "username": "@sevbertz",
        "phone": "+380976344830",
    },
    {
        "name": "Аріана Зоренко",
        "username": "@arishaax25",
        "phone": "+380997626187",
    },
    {
        "name": "Аліна Васильченко",
        "username": "@Angelocek444",
        "phone": "+380954818002",
    },
    {
        "name": "Поліна Сергієнко",
        "username": "@Dgffcyuv464",
        "phone": "+380990822423",
    },
    {
        "name": "Олена Лозов'ягіна",
        "username": "@lenich_pon",
        "phone": "+380962291984",
    },
    {
        "name": "Денис Павлов",
        "username": "@den4ik_1901",
        "phone": "+380991869281",
    },
    {
        "name": "Ілля Мормалюк",
        "username": "@kuki_v_popke",
        "phone": "+380954882292",
    },
    {
        "name": "Кирил Левицький",
        "username": "@Zprovl",
        "phone": "+380991235324",
    },
    {
        "name": "Богдан Котолуп",
        "username": "@Meinardius",
        "phone": "+380955745037",
    },
    {
        "name": "Данііл Грипась",
        "username": "@adANHYh",
        "phone": "+380683596747",
    },
]

GIRLS_LIST = [
    "Оксана Білик",
    "Дарина Щербина",
    "Ільїна Вікторія",
    "Ганна Міронова",
    "Аріана Зоренко",
    "Аліна Васильченко",
    "Поліна Сергієнко",
    "Олена Лозов'ягіна",
]

SUBJECTS_LIST = [
    "Алгебра",
    "Англійська мова",
    "Біологія",
    "Всесвітня історія",
    "Географія",
    "Геометрія",
    "Громадянська освіта",
    "Зарубіжна література",
    "Здоров'я, безпека та добробут",
    "Інформатика",
    "Історія України",
    "Мистецтво",
    "Підприємництво і фінансова грамотність",
    "Технології",
    "Українська література",
    "Українська мова",
    "Фізика",
    "Хімія",
]


# =====================================================================
# 📌 КАЛЕНДАР КАНІКУЛ ТА СВЯТКОВИХ ДНІВ
# =====================================================================

VACATION_PERIODS = [
    ("2026-10-26", "2026-11-01"),  # 🍂 Осінні канікули
    ("2026-12-28", "2027-01-10"),  # ❄️ Зимові канікули
    ("2027-03-22", "2027-03-28"),  # 🌱 Весняні канікули
    ("2027-06-02", "2027-08-31"),  # ☀️ Літні канікули
]


# ── Підрозділ: Канікули ──────────────────────────────────────────

def is_vacation(check_date):
    for start_str, end_str in VACATION_PERIODS:
        start_date = datetime.date.fromisoformat(start_str)
        end_date = datetime.date.fromisoformat(end_str)
        if start_date <= check_date <= end_date:
            return True
    return False


# Українські свята для календаря. Дата визначається автоматично з натиснутої
# клітинки календаря — нічого вводити вручну не потрібно.
FIXED_HOLIDAYS = {
    (1, 1): [("🎆", "Новий рік")],
    (1, 14): [("✨", "Старий Новий рік")],
    (1, 22): [("🇺🇦", "День Соборності України")],
    (2, 14): [("❤️", "День святого Валентина")],
    (2, 21): [("💬", "Міжнародний день рідної мови")],
    (3, 8): [("🌷", "Міжнародний жіночий день")],
    (4, 1): [("😄", "День сміху")],
    (4, 22): [("🌍", "День Землі")],
    (5, 1): [("🌿", "День праці")],
    (5, 8): [("🕯️", "День пам’яті та перемоги над нацизмом")],
    (6, 1): [("👧", "День захисту дітей")],
    (6, 28): [("🇺🇦", "День Конституції України")],
    (7, 15): [("🇺🇦", "День Української Державності")],
    (8, 24): [("🇺🇦", "День Незалежності України")],
    (9, 1): [("📚", "День знань")],
    (10, 1): [
        ("🛡️", "День захисників і захисниць України"),
        ("🌾", "День українського козацтва"),
    ],
    (10, 5): [("👩‍🏫", "Всесвітній день учителів")],
    (11, 17): [("🎓", "Міжнародний день студентів")],
    (11, 21): [("🇺🇦", "День Гідності та Свободи")],
    (12, 6): [("🇺🇦", "День Збройних Сил України"), ("🎁", "День Святого Миколая")],
    (12, 25): [("🎄", "Різдво Христове")],
}


# ── Підрозділ: Рухомі та фіксовані свята ───────────────────────

def _easter_date(year):
    """Дата православного/українського Великодня за григоріанським календарем."""
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 6) % 7
    # 22 березня + d + e — дата за юліанським календарем.
    julian_day = 22 + d + e
    if julian_day <= 31:
        month, day = 3, julian_day
    else:
        month, day = 4, julian_day - 31
    # Переводимо юліанську дату до григоріанського календаря.
    julian = datetime.date(year, month, day)
    return julian + datetime.timedelta(days=13 if year < 2100 else 14)


# ── Підрозділ: Події календаря ──────────────────────────────────

def calendar_events_for_date(check_date):
    """Повертає всі свята/пам'ятні дати для конкретного року."""
    if not isinstance(check_date, datetime.date):
        try:
            check_date = datetime.date.fromisoformat(str(check_date))
        except (TypeError, ValueError):
            return []

    events = list(FIXED_HOLIDAYS.get((check_date.month, check_date.day), []))

    # Рухомі дати, які автоматично перераховуються щороку.
    easter = _easter_date(check_date.year)
    if check_date == easter - datetime.timedelta(days=2):
        events.append(("✝️", "Страсна п’ятниця"))
    if check_date == easter - datetime.timedelta(days=1):
        events.append(("🕯️", "Велика субота"))
    if check_date == easter:
        events.append(("✝️", "Великдень"))
    if check_date == easter + datetime.timedelta(days=49):
        events.append(("🕊️", "Трійця"))
    if check_date == easter - datetime.timedelta(days=7):
        events.append(("🌿", "Вербна неділя"))

    # День матері — друга неділя травня.
    if check_date.month == 5 and check_date.weekday() == 6 and 8 <= check_date.day <= 14:
        events.append(("💐", "День матері"))

    # День батька — третя неділя червня.
    if check_date.month == 6 and check_date.weekday() == 6 and 15 <= check_date.day <= 21:
        events.append(("👨‍👧‍👦", "День батька"))

    # День працівників освіти України — перша неділя жовтня.
    if check_date.month == 10 and check_date.weekday() == 6 and 1 <= check_date.day <= 7:
        events.append(("👩‍🏫", "День працівників освіти (День учителя)"))

    # День програміста — 256-й день року.
    if check_date == datetime.date(check_date.year, 1, 1) + datetime.timedelta(days=255):
        events.append(("💻", "День програміста"))

    # Прибираємо можливі дублікати, зберігаючи порядок.
    result = []
    seen = set()
    for icon, name in events:
        key = (icon, name)
        if key not in seen:
            seen.add(key)
            result.append((icon, name))
    return result


def holiday_for_date(check_date):
    """Сумісна функція: повертає свята одним рядком для старих екранів."""
    events = calendar_events_for_date(check_date)
    return " • ".join(f"{icon} {name}" for icon, name in events)


# =====================================================================
# 🔔 РОЗКЛАД ДЗВІНКІВ ТА ЗМІНИ РОЗКЛАДУ
# =====================================================================

BELLS_SCHEDULE_TEXT = (
    "\n\n"
    "🔔 <b>Розклад дзвінків:</b>\n\n"
    "1. 08:30 – 09:15 (10 хв)\n"
    "2. 09:25 – 10:10 (20 хв)\n"
    "3. 10:30 – 11:15 (20 хв)\n"
    "4. 11:35 – 12:20 (15 хв)\n"
    "5. 12:35 – 13:20 (10 хв)\n"
    "6. 13:30 – 14:15 (10 хв)\n"
    "7. 14:25 – 15:10 (—)\n"
)

HAS_CHANGES_TODAY = False
CHANGES_TODAY_YES_TEXT = (
    "⚠ На сьогодні є зміни у розкладі:\n"
    "• 6 — Хімія (асинхронно)\n"
    "• 7 — Громадянська освіта (асинхронно)"
)
CHANGES_TODAY_NO_TEXT = (
    "✅ На сьогодні немає жодних змін у розкладі. "
    "Уроки проходять за стандартним розкладом!"
)

HAS_CHANGES_TOMORROW = False
CHANGES_TOMORROW_YES_TEXT = (
    "⚠️ На завтра є зміни у розкладі:\n"
    "• 6 — Хімія (асинхронно)\n"
    "• 7 — Громадянська освіта (асинхронно)"
)
CHANGES_TOMORROW_NO_TEXT = (
    "✅ На завтра немає жодних змін у розкладі. "
    "Уроки проходять за стандартним розкладом!"
)

HAS_BELLS_CHANGES_TODAY = False
BELLS_CHANGES_TODAY_YES_TEXT = (
    "⚠️ На сьогодні є зміни у розкладі дзвінків:\n"
    "1. 08:30 – 09:10 (10 хв)\n"
    "2. 09:20 – 10:00 (20 хв)\n"
    "3. 10:20 – 10:50 (10 хв)\n"
    "4. 11:00 – 11:30 (10 хв)\n"
    "5. 11:40 – 12:10 (—)\n"
)
BELLS_CHANGES_TODAY_NO_TEXT = (
    "✅ На сьогодні немає змін у розкладі дзвінків. "
    "Дзвінки проходять за стандартним розкладом!"
)

HAS_BELLS_CHANGES_TOMORROW = False
BELLS_CHANGES_TOMORROW_YES_TEXT = (
    "⚠ На завтра є зміни у розкладі дзвінків:\n"
    "1. 08:30 – 09:10 (10 хв)\n"
    "2. 09:20 – 10:00 (20 хв)\n"
    "3. 10:20 – 10:50 (10 хв)\n"
    "4. 11:00 – 11:30 (10 хв)\n"
    "5. 11:40 – 12:10 (—)\n"
)
BELLS_CHANGES_TOMORROW_NO_TEXT = (
    "✅ На завтра немає змін у розкладі дзвінків. "
    "Дзвінки проходять за стандартним розкладом!"
)

# Значення змін розкладу можна змінювати з адмін-панелі; вони переживають перезапуск.
# Додатково зберігаємо дати, до яких прив'язані прапорці «сьогодні/завтра»,
# щоб після опівночі старі статуси не переносилися на новий день.
CHANGE_STATUS_CONFIG_VERSION = 2
_today_iso = datetime.date.today().isoformat()
_tomorrow_iso = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
CHANGE_STATUS_DATES = saved_data.get("CHANGE_STATUS_DATES", {})
if not isinstance(CHANGE_STATUS_DATES, dict):
    CHANGE_STATUS_DATES = {}

_saved_change_status_version = saved_data.get("CHANGE_STATUS_CONFIG_VERSION", 0)
if _saved_change_status_version < CHANGE_STATUS_CONFIG_VERSION:
    HAS_CHANGES_TODAY = False
    HAS_CHANGES_TOMORROW = False
    HAS_BELLS_CHANGES_TODAY = False
    HAS_BELLS_CHANGES_TOMORROW = False
    CHANGE_STATUS_DATES = {
        "schedule_today": _today_iso,
        "schedule_tomorrow": _tomorrow_iso,
        "bells_today": _today_iso,
        "bells_tomorrow": _tomorrow_iso,
    }
else:
    # Якщо дата «сьогодні» змінилася, не тягнемо вчорашній прапорець далі.
    if CHANGE_STATUS_DATES.get("schedule_today") != _today_iso:
        HAS_CHANGES_TODAY = False
        CHANGE_STATUS_DATES["schedule_today"] = _today_iso
    else:
        HAS_CHANGES_TODAY = bool(saved_data.get("HAS_CHANGES_TODAY", HAS_CHANGES_TODAY))
    if CHANGE_STATUS_DATES.get("schedule_tomorrow") != _tomorrow_iso:
        HAS_CHANGES_TOMORROW = False
        CHANGE_STATUS_DATES["schedule_tomorrow"] = _tomorrow_iso
    else:
        HAS_CHANGES_TOMORROW = bool(saved_data.get("HAS_CHANGES_TOMORROW", HAS_CHANGES_TOMORROW))
    if CHANGE_STATUS_DATES.get("bells_today") != _today_iso:
        HAS_BELLS_CHANGES_TODAY = False
        CHANGE_STATUS_DATES["bells_today"] = _today_iso
    else:
        HAS_BELLS_CHANGES_TODAY = bool(saved_data.get("HAS_BELLS_CHANGES_TODAY", HAS_BELLS_CHANGES_TODAY))
    if CHANGE_STATUS_DATES.get("bells_tomorrow") != _tomorrow_iso:
        HAS_BELLS_CHANGES_TOMORROW = False
        CHANGE_STATUS_DATES["bells_tomorrow"] = _tomorrow_iso
    else:
        HAS_BELLS_CHANGES_TOMORROW = bool(saved_data.get("HAS_BELLS_CHANGES_TOMORROW", HAS_BELLS_CHANGES_TOMORROW))

CHANGES_TODAY_YES_TEXT = saved_data.get("CHANGES_TODAY_YES_TEXT", CHANGES_TODAY_YES_TEXT)
CHANGES_TOMORROW_YES_TEXT = saved_data.get("CHANGES_TOMORROW_YES_TEXT", CHANGES_TOMORROW_YES_TEXT)
BELLS_CHANGES_TODAY_YES_TEXT = saved_data.get("BELLS_CHANGES_TODAY_YES_TEXT", BELLS_CHANGES_TODAY_YES_TEXT)
BELLS_CHANGES_TOMORROW_YES_TEXT = saved_data.get("BELLS_CHANGES_TOMORROW_YES_TEXT", BELLS_CHANGES_TOMORROW_YES_TEXT)


# =====================================================================
# 📚 СХОВИЩЕ ДАНИХ (ДЗ, ПОВІДОМЛЕННЯ ТА СТАНИ)
# =====================================================================

# Дані вже відновлені з JSON вище. Не створюємо сховища повторно,
# інакше завантажені ДЗ та повідомлення від вчителя будуть стерті.
user_states = {}


# =====================================================================
# 🧹 СТАТИСТИКА ТА ЛОГІКА ЧЕРГУВАНЬ
# =====================================================================

DEFAULT_DUTY_RATINGS = {
    "Оксана Білик": 2,
    "Дарина Щербина": 1,
    "Ільїна Вікторія": 2,
    "Ганна Міронова": 1,
    "Аріана Зоренко": 2,
    "Аліна Васильченко": 1,
    "Поліна Сергієнко": 1,
    "Олена Лозов'ягіна": 1,
    "Ілля Мормалюк": 2,
    "Кирил Левицький": 3,
    "Денис Павлов": 2,
    "Данііл Грипась": 1,
    "Богдан Котолуп": 1,
}

# Якщо JSON порожній/новий — беремо стартові значення.
# Якщо JSON уже має дані — вони не перезаписуються.
if not duty_ratings:
    duty_ratings = DEFAULT_DUTY_RATINGS.copy()

# current_duty_person уже відновлено з JSON вище.


# ── Підрозділ: Чергування ───────────────────────────────────────

def get_vote_word(count):
    if count % 10 == 1 and count % 100 != 11:
        return "голос"
    elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
        return "голоси"
    else:
        return "голосів"


def get_next_duty_person(poll_data):
    votes_dict = poll_data["votes"]
    if not votes_dict:
        return current_duty_person

    max_votes = max(votes_dict.values())
    if max_votes == 0:
        return current_duty_person

    winners = [cand for cand, cnt in votes_dict.items() if cnt == max_votes]
    if len(winners) > 1:
        winners.sort(key=lambda name: duty_ratings.get(name, 0))
        return winners[0]
    else:
        return winners[0]


def is_week_offline(check_date=None):
    """
    Визначає, чи є вказана дата офлайн-тижнем.
    Якщо дата не передана — перевіряється завтра.
    """
    if check_date is None:
        check_date = datetime.date.today() + datetime.timedelta(days=1)

    base_offline_monday = datetime.date(2026, 9, 21)
    weeks_passed = (check_date - base_offline_monday).days // 7
    return weeks_passed % 2 == 0


def is_next_week_offline():
    # Зберігаємо стару функцію для інших частин коду.
    return is_week_offline(
        datetime.date.today() + datetime.timedelta(days=1)
    )


# =====================================================================
# 🗳 ДАНІ ТА ІНТЕРФЕЙС ОПИТУВАННЯ
# =====================================================================

poll_data = {
    "is_active": False,
    "votes": {
        "Оксана Білик": 0,
        "Дарина Щербина": 0,
        "Ільїна Вікторія": 0,
        "Ганна Міронова": 0,
        "Аріана Зоренко": 0,
        "Аліна Васильченко": 0,
    },
    "voted_users": {},
    "target_date": (datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
    "started_at": "",
    "ends_at": "",
    "completed_at": "",
}

_saved_poll_data = saved_data.get("poll_data")
if isinstance(_saved_poll_data, dict):
    poll_data["is_active"] = bool(_saved_poll_data.get("is_active", False))
    saved_votes = _saved_poll_data.get("votes", {})
    if isinstance(saved_votes, dict):
        for candidate in poll_data["votes"]:
            try:
                poll_data["votes"][candidate] = int(saved_votes.get(candidate, 0))
            except (TypeError, ValueError):
                poll_data["votes"][candidate] = 0
    saved_voted = _saved_poll_data.get("voted_users", {})
    poll_data["voted_users"] = saved_voted if isinstance(saved_voted, dict) else {}
    saved_target_date = _saved_poll_data.get("target_date")
    try:
        poll_data["target_date"] = datetime.date.fromisoformat(str(saved_target_date)).isoformat()
    except (TypeError, ValueError):
        poll_data["target_date"] = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for _poll_field in ("started_at", "ends_at", "completed_at"):
        poll_data[_poll_field] = str(_saved_poll_data.get(_poll_field, "") or "")


# Окреме універсальне опитування, яке адмін може створювати з будь-яким питанням та кількома варіантами.
CUSTOM_POLL = saved_data.get("CUSTOM_POLL", {})
if not isinstance(CUSTOM_POLL, dict):
    CUSTOM_POLL = {}
CUSTOM_POLL.setdefault("is_active", False)
CUSTOM_POLL.setdefault("question", "")
CUSTOM_POLL.setdefault("options", [])
CUSTOM_POLL.setdefault("votes", {})
CUSTOM_POLL.setdefault("voted_users", {})

# ── Підрозділ: Опитування ───────────────────────────────────────

def custom_poll_text():
    return f"🗳 <b>{html.escape(CUSTOM_POLL.get('question') or 'Опитування')}</b>\n\nОбери один варіант:"

def custom_poll_keyboard():
    k = types.InlineKeyboardMarkup(row_width=1)
    for i, option in enumerate(CUSTOM_POLL.get("options", [])):
        count = int(CUSTOM_POLL.get("votes", {}).get(option, 0))
        k.add(types.InlineKeyboardButton(f"{option} — {count}", callback_data=f"custom_vote:{i}"))
    return k

def poll_target_date():
    """Дата, на яку обираємо чергового. За замовчуванням — завтра."""
    try:
        return datetime.date.fromisoformat(str(poll_data.get("target_date")))
    except (TypeError, ValueError):
        return datetime.date.today() + datetime.timedelta(days=1)


def poll_is_active():
    """Повертає стан опитування. Після дедлайну воно не завершується автоматично:
    голосування лише блокується, щоб адміністратор міг натиснути «Завершити»
    та отримати підсумки."""
    return bool(poll_data.get("is_active"))


def poll_voting_open():
    """Чи можна зараз голосувати. Після дедлайну голоси не приймаються."""
    if not poll_is_active():
        return False
    ends_at = poll_data.get("ends_at")
    if not ends_at:
        return True
    try:
        return datetime.datetime.now() < datetime.datetime.fromisoformat(ends_at)
    except (ValueError, TypeError):
        return True


def poll_total_votes():
    return sum(int(v) for v in poll_data.get("votes", {}).values())


def poll_percentage(count, total=None):
    total = poll_total_votes() if total is None else total
    return 0 if total <= 0 else round((count / total) * 100)


def get_poll_text():
    target = poll_target_date()
    target_date = target.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[target.weekday()]
    deadline_text = ""
    if poll_data.get("ends_at"):
        try:
            deadline = datetime.datetime.fromisoformat(poll_data["ends_at"])
            deadline_text = f"\n⏳ Голосування до: <b>{deadline.strftime('%d.%m.%Y %H:%M')}</b>"
            if datetime.datetime.now() >= deadline and poll_data.get("is_active"):
                deadline_text += (
                    "\n🔒 <b>Час голосування вийшов.</b> "
                    "Голоси більше не приймаються. "
                    "Натисни «Завершити опитування», щоб отримати результати."
                )
        except (ValueError, TypeError):
            pass

    return (
        f"<b>🗳 Опитування класу: Хто буде черговим на {day_name}, {target_date}?</b>\n\n"
        "📋 Час обрати чергового! Тисни на кнопку з іменем нижче 👇\n"
        "<i>Голос зараховується один раз (його можна змінити кнопкою нижче).</i>"
        + deadline_text
    )

def get_poll_keyboard():
    poll_markup = types.InlineKeyboardMarkup(row_width=1)

    if poll_voting_open():
        for candidate, count in poll_data["votes"].items():
            vote_word = get_vote_word(count)
            poll_markup.add(
                types.InlineKeyboardButton(
                    f"👤 {candidate} — {count} {vote_word}",
                    callback_data=f"vote_{candidate}",
                )
            )
        poll_markup.add(
            types.InlineKeyboardButton(
                "🔄 Змінити мій голос", callback_data="reset_my_vote"
            )
        )
    else:
        poll_markup.add(
            types.InlineKeyboardButton(
                "🔒 Голосування закрите", callback_data="poll_locked"
            )
        )

    # Завершити може тільки адміністратор — callback додатково перевіряє ADMIN_IDS.
    poll_markup.add(
        types.InlineKeyboardButton(
            "🏁 Завершити опитування", callback_data="finish_poll"
        )
    )
    return poll_markup


# =====================================================================
# 🏠 ГОЛОВНЕ МЕНЮ БОТА
# =====================================================================


@bot.message_handler(commands=["admin"])

# ── Підрозділ: Команди та головне меню ─────────────────────────

def admin_command(message):
    register_user(message.from_user)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⚠️ Доступ заборонено!")
        return
    bot.send_message(
        message.chat.id,
        "⚙️ <b>Адмін-панель</b>\n\n"
        "🔐 Тут доступні інструменти керування ботом: ДЗ, повідомлення, "
        "розклад, опитування, користувачі, статистика, резервні копії та розсилка.\n\n"
        "👇 <b>Оберіть потрібний розділ:</b>",
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


def main_menu_text():
    return (
        "✨ <b>Привіт!</b> 👋 Це твій особистий помічник <b>8-Г класу</b> 🏫⚡\n\n"
        "📚 <b>Усе важливе — в одному місці:</b> ДЗ, розклад, зміни, повідомлення, погода, опитування та календар свят.\n\n"
        "🚀 <b>Статуси біля кнопок оновлюються автоматично</b>, щоб ти одразу бачив, де є нова інформація.\n\n"
        "👇 <b>Обери потрібний розділ:</b>"
    )


def build_main_menu_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    important_status = "🟢" if ((PINNED_IMPORTANT_MESSAGE.get("text") or PINNED_IMPORTANT_MESSAGE.get("html") or "").strip() or important_message_count()) else "🔴"
    poll_status = "🟢" if poll_data.get("is_active") else "🔴"

    buttons = {
        "school": types.InlineKeyboardButton("🏫 Коли ми йдемо в школу?", callback_data="check_school"),
        "canteen": types.InlineKeyboardButton("🍽 Меню харчування на завтра", callback_data="check_canteen"),
        "duty": types.InlineKeyboardButton("📢 Хто чергує?", callback_data="check_duty"),
        "teacher": types.InlineKeyboardButton("📢 Повідомлення від вчителя", callback_data="open_teacher_msg_section"),
        "important": types.InlineKeyboardButton(f"{important_status} 📌 Важливі повідомлення", callback_data="important_messages"),
        "homework": types.InlineKeyboardButton("📚 Домашнє завдання", callback_data="open_homework_section"),
        "holiday_calendar": types.InlineKeyboardButton("🎉 Календар свят", callback_data="holiday_calendar_start"),
        "global_search": types.InlineKeyboardButton("🔎 Глобальний пошук", callback_data="global_search_start"),
        "weather": types.InlineKeyboardButton("🌤 Погода в Охтирці", callback_data="weather_okhtyrka"),
        "poll": types.InlineKeyboardButton(f"{poll_status} 🗳 Опитування на вибір чергового", callback_data="class_poll"),
        "schedule": types.InlineKeyboardButton("📚 Розклад уроків", callback_data="schedule_menu_start"),
        "schedule_changes": types.InlineKeyboardButton("🔔 Зміни розкладу уроків", callback_data="schedule_changes_menu_start"),
        "bells_changes": types.InlineKeyboardButton("🔔 Зміни розкладу дзвінків", callback_data="bells_changes_menu_start"),
    }
    order = ADMIN_SETTINGS.get("menu_order") or DEFAULT_MENU_ORDER
    visible = ADMIN_SETTINGS.get("menu_visible", {})

    # Основні кнопки. Розклад уроків, зміни розкладу уроків
    # та зміни дзвінків ідуть вище за повідомлення від вчителя.
    for key in order:
        if key in ("global_search", "weather"):
            continue
        if visible.get(key, True) and key in buttons:
            keyboard.add(buttons[key])

    # Інформація для класу залишається перед глобальним пошуком і погодою.
    keyboard.add(
        types.InlineKeyboardButton(
            "ℹ️ Інформація для класу",
            callback_data="class_information_menu"
        )
    )

    # Глобальний пошук та погода — в самому кінці меню.
    for key in ("global_search", "weather"):
        if visible.get(key, True) and key in buttons:
            keyboard.add(buttons[key])

    return keyboard


# Зберігаємо ID останнього головного меню для кожного чату, щоб статуси
# можна було автоматично оновити після запуску/завершення опитування або зміни ДЗ.
MAIN_MENU_MESSAGES = {}

def refresh_main_menu(chat_id):
    message_id = MAIN_MENU_MESSAGES.get(chat_id)
    if not message_id:
        return
    try:
        safe_edit_message_text(
            main_menu_text(), chat_id, message_id,
            reply_markup=build_main_menu_keyboard(), parse_mode="HTML"
        )
    except Exception:
        # Повідомлення могло бути видалене або Telegram уже змінив його.
        MAIN_MENU_MESSAGES.pop(chat_id, None)


@bot.message_handler(commands=["start", "menu"])
def send_menu(message):
    register_user(message.from_user)
    save_data_to_file()
    user_states.pop(message.from_user.id, None)
    try:
        sent = bot.send_message(
            message.chat.id, main_menu_text(),
            reply_markup=build_main_menu_keyboard(), parse_mode="HTML"
        )
        MAIN_MENU_MESSAGES[message.chat.id] = sent.message_id
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "class_information_menu")
def class_information_menu(call):
    """Загальний розділ корисної інформації — надсилається окремим повідомленням."""
    register_user(call.from_user)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(
        types.InlineKeyboardButton(
            "📜 Правила групи",
            url="https://docs.google.com/document/d/1a1XrzXn_6NkMKyXQKlCNGUDtRkucXGgFeHuD_iDl1BM/edit?usp=sharing",
        ),
        types.InlineKeyboardButton(
            "🧹 Правила чергування",
            url="https://docs.google.com/document/d/10wYy_Xd_0Lfr8yPewixY9-Ly3IUG-aWjRYTu9DtDw64/edit?usp=sharing",
        ),
        types.InlineKeyboardButton(
            "🤖 Графік роботи бота-помічника",
            url="https://docs.google.com/document/d/1fg6JtXvA1TCFAouCl29MPgv3WC84J3mPWwwGinCjFcM/edit?usp=sharing",
        ),
        types.InlineKeyboardButton(
            "📌 Довідка",
            url="https://docs.google.com/document/d/1sAiUlUHaxUKgPEdeyxKELYB47VTdFTLSNSLkYWqrizI/edit?usp=sharing",
        ),
    )
    bot.send_message(
        call.message.chat.id,
        "ℹ️ <b>ІНФОРМАЦІЯ ДЛЯ КЛАСУ</b>\n\n"
        "Тут зібрані правила групи, правила чергування, графік роботи бота та довідкова інформація.",
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_main_menu")
def back_to_main_menu_callback(call):
    user_states.pop(call.from_user.id, None)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    send_menu(call.message)


@bot.callback_query_handler(func=lambda call: call.data in ("weather_okhtyrka", "weather_refresh"))
def weather_okhtyrka_callback(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "weather_viewed")
    # Перше відкриття погоди — окремим новим повідомленням.
    # «Оновити» — оновлює тільки повідомлення з погодою.
    show_weather(
        call,
        force_refresh=(call.data == "weather_refresh"),
        send_new_message=(call.data == "weather_okhtyrka"),
    )


# ---------------------------------------------------------------------
# 📢 7. ПОВІДОМЛЕННЯ ВЧИТЕЛІВ
# ---------------------------------------------------------------------

# =====================================================================
# 📢 РОЗДІЛ: ПОВІДОМЛЕННЯ ВІД ВЧИТЕЛЯ
# =====================================================================


# ── Підрозділ: Перегляд повідомлень ────────────────────────────

def _teacher_message_exists(item):
    if not isinstance(item, dict):
        return False
    text_value = (item.get("text") or "").strip()
    return bool(text_value or item.get("photo"))


def teacher_message_count():
    count = 1 if _teacher_message_exists(
        TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
    ) else 0
    count += sum(
        1
        for item in TEACHER_MESSAGE_STORAGE.get("subjects", {}).values()
        if _teacher_message_exists(item)
    )
    return count


def teacher_messages_menu_keyboard(user_id=None):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    status = "✅" if teacher_message_count() > 0 else "❌"

    # Перегляд повідомлень доступний усім користувачам.
    keyboard.add(
        types.InlineKeyboardButton(
            f"👀 Переглянути повідомлення {status}",
            callback_data="view_teacher_msg",
        )
    )

    # Керування повідомленнями — ТІЛЬКИ для адміністратора.
    if user_id is not None and is_admin(user_id):
        keyboard.add(
            types.InlineKeyboardButton(
                "✏️ Додати / редагувати повідомлення",
                callback_data="edit_teacher_msg_start",
            ),
            types.InlineKeyboardButton(
                "📌 Важливість повідомлень",
                callback_data="manage_important_messages",
            ),
            types.InlineKeyboardButton(
                "🗑 Видалити окреме повідомлення",
                callback_data="delete_teacher_msg_start",
            ),
            types.InlineKeyboardButton(
                "🗑 Очистити всі повідомлення",
                callback_data="clear_teacher_msg",
            ),
        )
    return keyboard


# Залишаємо стару назву функції, якщо вона використовується в інших місцях.
def teacher_msg_menu_keyboard(user_id=None):
    return teacher_messages_menu_keyboard(user_id)


def teacher_msg_menu_text():
    return "📢 <b>Повідомлення від вчителя</b>\nОбери потрібну дію:"


@bot.callback_query_handler(
    func=lambda call: call.data == "open_teacher_msg_section"
)
def open_teacher_msg_section(call):
    user_states.pop(call.from_user.id, None)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    bot.send_message(
        call.message.chat.id,
        teacher_msg_menu_text(),
        reply_markup=teacher_messages_menu_keyboard(call.from_user.id),
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "back_to_teacher_msg_menu"
)
def teacher_msg_main_menu(call):
    user_states.pop(call.from_user.id, None)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    try:
        safe_edit_message_text(
            teacher_msg_menu_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=teacher_messages_menu_keyboard(call.from_user.id),
            parse_mode="HTML",
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "view_teacher_msg")
def view_teacher_message(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "messages_viewed")
    lines = ["📢 <b>ПОВІДОМЛЕННЯ ВІД УЧИТЕЛІВ</b>", ""]
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    items = []
    class_teacher = TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
    if _teacher_message_exists(class_teacher):
        items.append(("👩‍🏫 Класний керівник", class_teacher))
    for subject in SUBJECTS_LIST:
        item = TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        if _teacher_message_exists(item):
            items.append((f"📚 {subject}", item))
    if not items:
        lines.append("📭 <b>Наразі немає доданих повідомлень.</b>")
    else:
        items.sort(key=lambda pair: (not bool(pair[1].get("pinned", False)),))
        for title, item in items:
            lines.append(f"<b>{html.escape(title)}</b>")
            text_value = (item.get("text") or "").strip()
            lines.append(html.escape(text_value) if text_value else "📸 Фото повідомлення")
            lines.append("")
    keyboard.add(types.InlineKeyboardButton("🔙 Назад до меню повідомлень", callback_data="back_to_teacher_msg_menu"))
    bot.answer_callback_query(call.id)
    text = "\n".join(lines).rstrip()
    try:
        safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=keyboard, parse_mode="HTML")
    for title, item in items:
        if item.get("photo"):
            try: bot.send_photo(call.message.chat.id, item["photo"], caption=title)
            except Exception: pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("teacher_read:"))
def teacher_mark_read(call):
    key = call.data.split(":", 1)[1]
    item = TEACHER_MESSAGE_STORAGE.get("class_teacher", {}) if key == "class" else TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(key.split(":",1)[1], {})
    if not item or not _teacher_message_exists(item):
        bot.answer_callback_query(call.id, "Повідомлення вже видалено.", show_alert=True)
        return
    read_by = item.setdefault("read_by", [])
    uid = str(call.from_user.id)
    if uid not in [str(x) for x in read_by]:
        read_by.append(uid)
        save_data_to_file()
    bot.answer_callback_query(call.id, "✅ Позначено як прочитане!")
    view_teacher_message(call)

@bot.callback_query_handler(
    func=lambda call: call.data == "edit_teacher_msg_start"
)

# ── Підрозділ: Редагування повідомлень ─────────────────────────

def edit_teacher_msg_start(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    # Меню додавання — таке саме двоколонкове, як вибір предмета для ДЗ.
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    row = [
        types.InlineKeyboardButton(
            "Від класної керівнички",
            callback_data="teacher_target_class",
        )
    ]
    keyboard.add(*row)

    row = []
    for index, subject in enumerate(SUBJECTS_LIST):
        row.append(
            types.InlineKeyboardButton(
                subject,
                callback_data=f"teacher_target_{index}",
            )
        )
        if len(row) == 2:
            keyboard.add(*row)
            row = []

    if row:
        keyboard.add(*row)

    keyboard.add(
        types.InlineKeyboardButton(
            "❌ Скасувати",
            callback_data="back_to_teacher_msg_menu",
        )
    )

    safe_edit_message_text(
        "✍️ <b>Обери, від кого хочеш додати повідомлення:</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "teacher_target_class"
    or call.data.startswith("teacher_target_")
)
def teacher_message_target_selected(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    if call.data == "teacher_target_class":
        target = "class_teacher"
        target_title = "📢 Повідомлення від класної керівнички"
    else:
        try:
            index = int(call.data.replace("teacher_target_", ""))
            target = SUBJECTS_LIST[index]
        except (ValueError, IndexError):
            try:
                bot.answer_callback_query(
                    call.id, "⚠️ Помилка вибору!", show_alert=True
                )
            except Exception:
                pass
            return
        target_title = f"📚 {target}"

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user_states[call.from_user.id] = {
        "action": "waiting_teacher_message",
        "teacher_target": target,
        "chat_id": call.message.chat.id,
        "message_id": call.message.message_id,
    }

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            "❌ Скасувати",
            callback_data="back_to_teacher_msg_menu",
        )
    )

    safe_edit_message_text(
        f"✍️ Обрано: <b>{target_title}</b>.\n\n"
        "Надішли текст повідомлення або фото з підписом:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "delete_teacher_msg_start"
)

# ── Підрозділ: Видалення повідомлень ───────────────────────────

def delete_teacher_msg_start(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    row = []

    class_teacher = TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
    if _teacher_message_exists(class_teacher):
        row.append(
            types.InlineKeyboardButton(
                "👩‍🏫 Класна керівничка",
                callback_data="delete_teacher_target_class",
            )
        )

    for index, subject in enumerate(SUBJECTS_LIST):
        item = TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        if _teacher_message_exists(item):
            row.append(
                types.InlineKeyboardButton(
                    subject,
                    callback_data=f"delete_teacher_target_{index}",
                )
            )
            if len(row) == 2:
                keyboard.add(*row)
                row = []

    if row:
        keyboard.add(*row)

    if not _teacher_message_exists(class_teacher) and not any(
        _teacher_message_exists(
            TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        )
        for subject in SUBJECTS_LIST
    ):
        keyboard.add(
            types.InlineKeyboardButton(
                "🔙 Назад",
                callback_data="back_to_teacher_msg_menu",
            )
        )
        safe_edit_message_text(
            "📭 <b>Немає повідомлень для видалення.</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    keyboard.add(
        types.InlineKeyboardButton(
            "🔙 Назад",
            callback_data="back_to_teacher_msg_menu",
        )
    )

    safe_edit_message_text(
        "🗑 <b>Обери повідомлення, яке потрібно видалити:</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "delete_teacher_target_class"
    or call.data.startswith("delete_teacher_target_")
)
def delete_teacher_target_selected(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    if call.data == "delete_teacher_target_class":
        target = "class_teacher"
        target_title = "👩‍🏫 повідомлення від класної керівнички"
    else:
        try:
            index = int(call.data.replace("delete_teacher_target_", ""))
            target = SUBJECTS_LIST[index]
        except (ValueError, IndexError):
            try:
                bot.answer_callback_query(
                    call.id, "⚠️ Помилка вибору!", show_alert=True
                )
            except Exception:
                pass
            return
        target_title = f"📚 повідомлення з предмета «{target}»"

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "✅ Так, видалити",
            callback_data=f"confirm_delete_teacher_{'class' if target == 'class_teacher' else SUBJECTS_LIST.index(target)}",
        ),
        types.InlineKeyboardButton(
            "❌ Ні, залишити",
            callback_data="cancel_delete_teacher_msg",
        ),
    )

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    safe_edit_message_text(
        f"⚠️ <b>Точно видалити {target_title}?</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_delete_teacher_")
)
def confirm_delete_teacher_message(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    value = call.data.replace("confirm_delete_teacher_", "")
    if value == "class":
        target = "class_teacher"
        target_title = "повідомлення від класної керівнички"
    else:
        try:
            target = SUBJECTS_LIST[int(value)]
        except (ValueError, IndexError):
            try:
                bot.answer_callback_query(call.id, "⚠️ Помилка!", show_alert=True)
            except Exception:
                pass
            return
        target_title = f"повідомлення з предмета «{target}»"

    if target == "class_teacher":
        TEACHER_MESSAGE_STORAGE["class_teacher"] = {"text": "", "photo": None, "important": False}
    else:
        TEACHER_MESSAGE_STORAGE.setdefault("subjects", {})
        TEACHER_MESSAGE_STORAGE["subjects"].pop(target, None)

    save_data_to_file()

    try:
        bot.answer_callback_query(call.id, "🗑 Повідомлення видалено!", show_alert=True)
    except Exception:
        pass

    teacher_msg_main_menu(call)


@bot.callback_query_handler(
    func=lambda call: call.data == "cancel_delete_teacher_msg"
)
def cancel_delete_teacher_message(call):
    try:
        bot.answer_callback_query(call.id, "❌ Видалення скасовано.")
    except Exception:
        pass
    teacher_msg_main_menu(call)


@bot.callback_query_handler(func=lambda call: call.data == "clear_teacher_msg")
def clear_teacher_message(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    if teacher_message_count() == 0:
        try:
            bot.answer_callback_query(
                call.id, "📭 Немає повідомлень для видалення.", show_alert=True
            )
        except Exception:
            pass
        return

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "✅ Так, видалити все",
            callback_data="confirm_clear_teacher_msg",
        ),
        types.InlineKeyboardButton(
            "❌ Ні, залишити",
            callback_data="cancel_clear_teacher_msg",
        ),
    )

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    safe_edit_message_text(
        "⚠️ <b>Точно видалити всі повідомлення від вчителів?</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "confirm_clear_teacher_msg"
)
def confirm_clear_teacher_message(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Доступ заборонено!", show_alert=True
            )
        except Exception:
            pass
        return

    TEACHER_MESSAGE_STORAGE["class_teacher"] = {"text": "", "photo": None, "important": False}
    TEACHER_MESSAGE_STORAGE["subjects"] = {}
    save_data_to_file()

    try:
        bot.answer_callback_query(
            call.id, "🗑 Усі повідомлення видалено!", show_alert=True
        )
    except Exception:
        pass

    teacher_msg_main_menu(call)


@bot.callback_query_handler(
    func=lambda call: call.data == "cancel_clear_teacher_msg"
)
def cancel_clear_teacher_message(call):
    try:
        bot.answer_callback_query(call.id, "❌ Видалення скасовано.")
    except Exception:
        pass
    teacher_msg_main_menu(call)


# =====================================================================
# 📌 ОКРЕМЕ ЗАКРІПЛЕНЕ ПОВІДОМЛЕННЯ
# =====================================================================

@bot.callback_query_handler(func=lambda call: call.data == "admin_pinned_message")

# ── Підрозділ: Важливе повідомлення ────────────────────────────

def admin_pinned_message(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True)
        return

    textv = (PINNED_IMPORTANT_MESSAGE.get("text") or "").strip()
    if textv:
        body = (
            "📌 <b>ВАЖЛИВЕ ПОВІДОМЛЕННЯ</b>\n\n"
            f"{stored_message_html(PINNED_IMPORTANT_MESSAGE)}\n\n"
            "Це повідомлення показується користувачам у розділі 📌 Важливі повідомлення."
        )
    else:
        body = (
            "📌 <b>ВАЖЛИВЕ ПОВІДОМЛЕННЯ</b>\n\n"
            "📭 Повідомлення ще не додано."
        )

    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("✏️ Написати / змінити", callback_data="admin_pinned_message_edit"))
    if textv:
        k.add(types.InlineKeyboardButton("🗑 Видалити", callback_data="admin_pinned_message_delete"))
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text(body, call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_pinned_message_edit")
def admin_pinned_message_edit(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True)
        return

    user_states[call.from_user.id] = {"action": "waiting_pinned_important_message"}
    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_pinned_message"))
    safe_edit_message_text(
        "📌 <b>ВАЖЛИВЕ ПОВІДОМЛЕННЯ</b>\n\n"
        "✍️ Надішли повідомлення, яке потрібно показувати користувачам у розділі «📌 Важливі повідомлення».\n\n"
        "<b>Можеш форматувати текст прямо в Telegram</b> — жирний, курсив, підкреслення, закреслення, цитати, посилання тощо. Бот збереже це форматування.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_pinned_message_delete")
def admin_pinned_message_delete(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True)
        return
    PINNED_IMPORTANT_MESSAGE["text"] = ""
    PINNED_IMPORTANT_MESSAGE["html"] = ""
    PINNED_IMPORTANT_MESSAGE["created_at"] = ""
    save_data_to_file()
    log_admin_action(call.from_user.id, "Видалено важливе повідомлення")
    bot.answer_callback_query(call.id, "🗑 Важливе повідомлення видалено")
    admin_pinned_message(call)


# =====================================================================
# 📌 ВАЖЛИВІ ПОВІДОМЛЕННЯ + 🔎 ГЛОБАЛЬНИЙ ПОШУК
# =====================================================================

# ── Підрозділ: Важливі повідомлення для користувачів ──────────

def _teacher_message_items():
    items = []
    class_teacher = TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
    if _teacher_message_exists(class_teacher):
        items.append(("👩‍🏫 Класний керівник", "class_teacher", class_teacher))
    for subject in SUBJECTS_LIST:
        item = TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        if _teacher_message_exists(item):
            items.append((f"📚 {subject}", subject, item))
    return items


@bot.callback_query_handler(func=lambda call: call.data == "important_messages")
def important_messages(call):
    bot.answer_callback_query(call.id)
    lines = ["📌 <b>ВАЖЛИВІ ПОВІДОМЛЕННЯ</b>", ""]
    pinned_text = (PINNED_IMPORTANT_MESSAGE.get("text") or "").strip()
    items = [item for item in _teacher_message_items() if item[2].get("important", False)]
    if pinned_text:
        lines.append("📌 <b>Важливе повідомлення</b>")
        lines.append(stored_message_html(PINNED_IMPORTANT_MESSAGE))
        lines.append("")
    if not items and not pinned_text:
        lines.append("📭 Наразі важливих повідомлень немає.")
    elif items:
        for title, _, item in items:
            lines.append(f"📌 <b>{html.escape(title)}</b>")
            textv = (item.get("text") or "").strip()
            lines.append(html.escape(textv) if textv else "📸 Фото повідомлення")
            lines.append("")
    # Це ЗАВЖДИ окреме повідомлення. Головне меню не редагуємо.
    # Кнопка повернення також не редагує це повідомлення — вона просто
    # створює нове головне меню, як і інші окремі розділи бота.
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"))
    bot.send_message(
        call.message.chat.id,
        "\n".join(lines).rstrip(),
        reply_markup=k,
        parse_mode="HTML",
    )
    for title, _, item in items:
        if item.get("photo"):
            try:
                bot.send_photo(call.message.chat.id, item["photo"], caption=f"📌 {title}")
            except Exception:
                pass


@bot.callback_query_handler(func=lambda call: call.data == "close_important_messages")
def close_important_messages(call):
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "manage_important_messages")
def manage_important_messages(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True)
        return
    items = _teacher_message_items()
    k = types.InlineKeyboardMarkup(row_width=1)
    for title, key, item in items:
        status = "📌" if item.get("important", False) else "📄"
        pin = "📍" if item.get("pinned", False) else "▫️"
        target = "class" if key == "class_teacher" else f"subject:{SUBJECTS_LIST.index(key)}"
        k.add(types.InlineKeyboardButton(f"{status} {title}", callback_data=f"toggle_important:{target}"), types.InlineKeyboardButton(f"{pin} Закріпити", callback_data=f"toggle_pinned:{target}"))
    k.add(types.InlineKeyboardButton("🔙 До повідомлень", callback_data="back_to_teacher_msg_menu"))
    text = "📌 <b>ВАЖЛИВІСТЬ ПОВІДОМЛЕНЬ</b>\n\n📌 — важливе\n📄 — звичайне\n\nНатисни на повідомлення, щоб змінити статус."
    if not items:
        text += "\n\n📭 Повідомлень поки немає."
    bot.answer_callback_query(call.id)
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_important:"))
def toggle_important(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True)
        return
    target = call.data.split(":", 1)[1]
    if target == "class":
        item = TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
        title = "класної керівнички"
    elif target.startswith("subject:"):
        try:
            subject = SUBJECTS_LIST[int(target.split(":", 1)[1])]
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "⚠️ Помилка вибору", show_alert=True)
            return
        item = TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        title = f"предмета «{subject}»"
    else:
        bot.answer_callback_query(call.id, "⚠️ Невірне повідомлення", show_alert=True)
        return
    if not _teacher_message_exists(item):
        bot.answer_callback_query(call.id, "📭 Повідомлення вже видалено", show_alert=True)
        return
    item["important"] = not bool(item.get("important", False))
    save_data_to_file()
    log_admin_action(call.from_user.id, f"{'Позначено важливим' if item['important'] else 'Знято важливість'}: {title}")
    bot.answer_callback_query(call.id, "📌 Позначено важливим" if item["important"] else "📄 Важливість знято")
    manage_important_messages(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_pinned:"))
def toggle_pinned(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ заборонено", show_alert=True); return
    target = call.data.split(":", 1)[1]
    if target == "class":
        item = TEACHER_MESSAGE_STORAGE.get("class_teacher", {})
        title = "класного керівника"
    elif target.startswith("subject:"):
        try: subject = SUBJECTS_LIST[int(target.split(":",1)[1])]
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "⚠️ Невірне повідомлення", show_alert=True); return
        item = TEACHER_MESSAGE_STORAGE.get("subjects", {}).get(subject, {})
        title = f"предмета «{subject}»"
    else:
        bot.answer_callback_query(call.id, "⚠️ Невірне повідомлення", show_alert=True); return
    if not _teacher_message_exists(item):
        bot.answer_callback_query(call.id, "📭 Повідомлення вже видалено", show_alert=True); return
    item["pinned"] = not bool(item.get("pinned", False))
    save_data_to_file()
    log_admin_action(call.from_user.id, f"{'Закріплено' if item['pinned'] else 'Відкріплено'} повідомлення {title}")
    bot.answer_callback_query(call.id, "📌 Закріплено" if item["pinned"] else "📍 Відкріплено")
    manage_important_messages(call)


# ── Підрозділ: Глобальний пошук ────────────────────────────────

def _schedule_subjects_for_date(check_date):
    if is_vacation(check_date) or check_date.weekday() in [5, 6]:
        return []
    offline_status = is_week_offline(check_date)
    subject_mon_3 = "Здоров'я, безпека та добробут" if offline_status else "Підприємництво і фінансова грамотність"
    subject_fri_7 = "Історія України" if offline_status else "Громадянська освіта"
    schedules = {
        0: ["Фізика", "Зарубіжна література", subject_mon_3, "Англійська мова", "Алгебра", "Фізична культура", "Біологія"],
        1: ["Географія", "Хімія", "Українська мова", "Всесвітня історія", "Геометрія", "Українська література"],
        2: ["Українська мова", "Історія України", "Англійська мова", "Фізична культура", "Алгебра", "Інформатика", "Мистецтво"],
        3: ["Англійська мова", "Географія", "Технології", "Фізика", "Геометрія", "Українська література"],
        4: ["Українська мова", "Алгебра", "Біологія", "Фізична культура", "Інформатика", "Хімія", subject_fri_7],
    }
    return schedules.get(check_date.weekday(), [])


@bot.callback_query_handler(func=lambda call: call.data == "global_search_start")
def global_search_start(call):
    """Глобальний пошук: кожен перехід відкривається НОВИМ повідомленням.
    Головне меню та попередні повідомлення не редагуються.
    """
    bot.answer_callback_query(call.id)

    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("📚 ДЗ", callback_data="global_search_section:hw"),
        types.InlineKeyboardButton("📢 Повідомлення", callback_data="global_search_section:messages"),
    )
    k.add(
        types.InlineKeyboardButton("📅 Розклад", callback_data="global_search_section:schedule"),
        types.InlineKeyboardButton("🗳 Опитування", callback_data="global_search_section:poll"),
    )
    k.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"))
    bot.send_message(
        call.message.chat.id,
        "🔎 <b>ГЛОБАЛЬНИЙ ПОШУК</b>\n\n"
        "Обери, де хочеш щось знайти. Вводити текст не потрібно 👇",
        reply_markup=k,
        parse_mode="HTML",
    )


def _send_global_search_page(call, text, reply_markup):
    """Оновлює поточне повідомлення глобального пошуку.

    Саме перше відкриття глобального пошуку робиться окремим новим
    повідомленням у ``global_search_start``. Після цього всі переходи
    всередині пошуку редагують це саме повідомлення, щоб у чаті не
    накопичувалася купа однакових повідомлень. Головне меню при цьому
    залишається недоторканим.
    """
    safe_edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("global_search_section:"))
def global_search_section(call):
    section = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)

    if section == "hw":
        subjects = []
        for day_data in HOMEWORK_STORAGE.values():
            if isinstance(day_data, dict):
                for subject in day_data:
                    if subject in SUBJECTS_LIST and subject not in subjects:
                        subjects.append(subject)
        subjects.sort(key=lambda x: SUBJECTS_LIST.index(x))

        k = types.InlineKeyboardMarkup(row_width=2)
        for subject in subjects:
            idx = SUBJECTS_LIST.index(subject)
            k.add(types.InlineKeyboardButton(f"📚 {subject}", callback_data=f"global_search_hw_subject:{idx}"))
        if not subjects:
            text = "📚 <b>ДЗ</b>\n\n📭 Збережених домашніх завдань поки немає."
        else:
            text = "📚 <b>ДЗ — ОБЕРИ ПРЕДМЕТ</b>\n\nНатисни на предмет, щоб побачити всі ДЗ з нього."
        k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
        _send_global_search_page(call, text, k)
        return

    if section == "messages":
        k = types.InlineKeyboardMarkup(row_width=2)
        k.add(types.InlineKeyboardButton("📌 Важливі", callback_data="global_search_messages:important"))
        k.add(types.InlineKeyboardButton("📢 Усі повідомлення", callback_data="global_search_messages:all"))
        k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
        _send_global_search_page(
            call,
            "📢 <b>ПОВІДОМЛЕННЯ</b>\n\nОбери, які повідомлення переглянути:",
            k,
        )
        return

    if section == "schedule":
        subjects = []
        start_date = datetime.date.today()
        for offset in range(62):
            d = start_date + datetime.timedelta(days=offset)
            for subject in _schedule_subjects_for_date(d):
                if subject not in subjects:
                    subjects.append(subject)
        subjects.sort(key=lambda x: SUBJECTS_LIST.index(x) if x in SUBJECTS_LIST else 999)

        k = types.InlineKeyboardMarkup(row_width=2)
        for subject in subjects:
            if subject in SUBJECTS_LIST:
                idx = SUBJECTS_LIST.index(subject)
                k.add(types.InlineKeyboardButton(f"📚 {subject}", callback_data=f"global_search_schedule_subject:{idx}"))
        k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
        _send_global_search_page(
            call,
            "📅 <b>РОЗКЛАД — ОБЕРИ ПРЕДМЕТ</b>\n\n"
            "Натисни на предмет, щоб побачити, коли він є в розкладі.",
            k,
        )
        return

    if section == "poll":
        k = types.InlineKeyboardMarkup(row_width=1)
        if poll_data.get("is_active"):
            k.add(types.InlineKeyboardButton("🗳 Відкрити активне опитування", callback_data="global_search_poll_open"))
            k.add(types.InlineKeyboardButton("📊 Результати", callback_data="global_search_poll_results"))
        else:
            k.add(types.InlineKeyboardButton("📭 Опитування зараз немає", callback_data="noop"))
        k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
        status = "🟢 Активне опитування є." if poll_data.get("is_active") else "⚪ Активного опитування зараз немає."
        _send_global_search_page(call, "🗳 <b>ОПИТУВАННЯ</b>\n\n" + status, k)


@bot.callback_query_handler(func=lambda call: call.data.startswith("global_search_hw_subject:"))
def global_search_hw_subject(call):
    try:
        subject = SUBJECTS_LIST[int(call.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет", show_alert=True)
        return

    found = []
    for date_key, day_data in HOMEWORK_STORAGE.items():
        if not isinstance(day_data, dict) or subject not in day_data:
            continue
        info = day_data.get(subject, {})
        found.append((date_key, info.get("text") or "📸 Фото завдання", info.get("photo")))
    found.sort(key=lambda x: x[0])

    lines = [f"📚 <b>ДЗ — {html.escape(subject)}</b>", ""]
    if not found:
        lines.append("📭 ДЗ з цього предмета не знайдено.")
    else:
        for date_key, textv, _ in found:
            try:
                d = datetime.date.fromisoformat(date_key)
                date_text = f"{WEEKDAYS_UKR[d.weekday()].capitalize()}, {d.strftime('%d.%m.%Y')}"
            except ValueError:
                date_text = date_key
            lines.append(f"📅 <b>{date_text}</b>")
            lines.append(html.escape(textv))
            lines.append("")

    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔙 До предметів ДЗ", callback_data="global_search_section:hw"))
    k.add(types.InlineKeyboardButton("🔎 Глобальний пошук", callback_data="global_search_start"))
    _send_global_search_page(call, "\n".join(lines).rstrip(), k)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("global_search_schedule_subject:"))
def global_search_schedule_subject(call):
    try:
        subject = SUBJECTS_LIST[int(call.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет", show_alert=True)
        return

    found = []
    start_date = datetime.date.today()
    for offset in range(62):
        d = start_date + datetime.timedelta(days=offset)
        lessons = _schedule_subjects_for_date(d)
        for lesson_no, item in enumerate(lessons, 1):
            if item == subject:
                found.append((d, lesson_no))

    lines = [f"📅 <b>РОЗКЛАД — {html.escape(subject)}</b>", ""]
    if not found:
        lines.append("📭 Цього предмета в найближчі 2 місяці не знайдено.")
    else:
        for d, lesson_no in found:
            lines.append(f"{WEEKDAYS_UKR[d.weekday()].capitalize()}, <b>{d.strftime('%d.%m.%Y')}</b> — {lesson_no}-й урок")

    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔙 До предметів розкладу", callback_data="global_search_section:schedule"))
    k.add(types.InlineKeyboardButton("🔎 Глобальний пошук", callback_data="global_search_start"))
    _send_global_search_page(call, "\n".join(lines).rstrip(), k)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "global_search_messages:important")
def global_search_messages_important(call):
    bot.answer_callback_query(call.id)
    items = [item for item in _teacher_message_items() if item[2].get("important", False)]
    pinned_text = (PINNED_IMPORTANT_MESSAGE.get("text") or "").strip()
    lines = ["📌 <b>ВАЖЛИВІ ПОВІДОМЛЕННЯ</b>", ""]
    if pinned_text:
        lines += [stored_message_html(PINNED_IMPORTANT_MESSAGE), ""]
    for title, _, item in items:
        lines += [f"📢 <b>{html.escape(title)}</b>", html.escape(item.get("text") or "📸 Фото повідомлення"), ""]
    if not pinned_text and not items:
        lines.append("📭 Важливих повідомлень немає.")
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔙 До повідомлень", callback_data="global_search_section:messages"))
    k.add(types.InlineKeyboardButton("🔎 Глобальний пошук", callback_data="global_search_start"))
    _send_global_search_page(call, "\n".join(lines).rstrip(), k)


@bot.callback_query_handler(func=lambda call: call.data == "global_search_messages:all")
def global_search_messages_all(call):
    bot.answer_callback_query(call.id)
    items = _teacher_message_items()
    lines = ["📢 <b>УСІ ПОВІДОМЛЕННЯ</b>", ""]
    if not items:
        lines.append("📭 Повідомлень немає.")
    else:
        for title, _, item in items:
            marker = "📌" if item.get("important", False) else "📢"
            lines += [f"{marker} <b>{html.escape(title)}</b>", html.escape(item.get("text") or "📸 Фото повідомлення"), ""]
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔙 До повідомлень", callback_data="global_search_section:messages"))
    k.add(types.InlineKeyboardButton("🔎 Глобальний пошук", callback_data="global_search_start"))
    _send_global_search_page(call, "\n".join(lines).rstrip(), k)


@bot.callback_query_handler(func=lambda call: call.data == "global_search_poll_open")
def global_search_poll_open(call):
    bot.answer_callback_query(call.id)
    if not poll_data.get("is_active"):
        text = "🗳 <b>ОПИТУВАННЯ</b>\n\n📭 Активного опитування вже немає."
        _send_global_search_page(call, text, types.InlineKeyboardMarkup())
        return
    k = get_poll_keyboard()
    k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
    _send_global_search_page(call, get_poll_text(), k)


@bot.callback_query_handler(func=lambda call: call.data == "global_search_poll_results")
def global_search_poll_results(call):
    bot.answer_callback_query(call.id)
    if not poll_data.get("is_active"):
        text = "🗳 <b>РЕЗУЛЬТАТИ ОПИТУВАННЯ</b>\n\n📭 Активного опитування зараз немає."
    else:
        total = sum(poll_data.get("votes", {}).values())
        lines = ["🗳 <b>РЕЗУЛЬТАТИ ОПИТУВАННЯ</b>", ""]
        for name, count in poll_data.get("votes", {}).items():
            lines.append(f"👤 {html.escape(name)} — <b>{count}</b>")
        lines.append(f"\n📊 Всього голосів: <b>{total}</b>")
        text = "\n".join(lines)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔙 До глобального пошуку", callback_data="global_search_start"))
    _send_global_search_page(call, text, k)

# =====================================================================
# 📚 РОЗДІЛ: ДОМАШНЄ ЗАВДАННЯ
# =====================================================================

# Міграція старого формату {"Алгебра": {...}} у формат {"YYYY-MM-DD": {"Алгебра": {...}}}.

# ── Підрозділ: Перегляд та пошук ДЗ ────────────────────────────

def migrate_homework_storage():
    global HOMEWORK_STORAGE
    if not HOMEWORK_STORAGE:
        return
    first_key = next(iter(HOMEWORK_STORAGE))
    try:
        datetime.date.fromisoformat(first_key)
        return
    except (ValueError, TypeError):
        pass
    tomorrow_key = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    old = HOMEWORK_STORAGE
    HOMEWORK_STORAGE = {tomorrow_key: {}}
    for subject, info in old.items():
        if isinstance(info, dict):
            info.pop("status_by_user", None)
            HOMEWORK_STORAGE[tomorrow_key][subject] = info
    save_data_to_file()

migrate_homework_storage()

# Видаляємо старі персональні статуси з даних після оновлення версії.
for _day in HOMEWORK_STORAGE.values():
    if isinstance(_day, dict):
        for _info in _day.values():
            if isinstance(_info, dict):
                _info.pop("status_by_user", None)



def hw_date_label(date_obj):
    return f"{WEEKDAYS_UKR[date_obj.weekday()]}, {date_obj.strftime('%d.%m.%Y')}"


# =====================================================================
# 🎉 РОЗДІЛ: КАЛЕНДАР СВЯТ
# =====================================================================

HOLIDAY_MONTHS_UKR = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"
]
HOLIDAY_DIGIT_EMOJI = {
    "0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣",
    "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣",
}


def _holiday_day_label(day):
    """Дата свята у вузькій клітинці Telegram-календаря: 4️⃣, 1️⃣5️⃣ тощо."""
    return "".join(HOLIDAY_DIGIT_EMOJI[ch] for ch in str(day))


def holiday_calendar_keyboard(year=None, month=None):
    """Окремий календар свят. Інші календарі бота свята не показують."""
    today = datetime.date.today()
    if year is None or month is None:
        year, month = today.year, today.month

    first = datetime.date(year, month, 1)
    next_month = datetime.date(
        year + (month == 12),
        1 if month == 12 else month + 1,
        1,
    )
    days_in_month = (next_month - first).days

    keyboard = types.InlineKeyboardMarkup(row_width=7)
    keyboard.add(
        types.InlineKeyboardButton(
            f"🎉 {HOLIDAY_MONTHS_UKR[month - 1]} {year}",
            callback_data="holiday_noop",
        )
    )
    keyboard.add(*[
        types.InlineKeyboardButton(x, callback_data="holiday_noop")
        for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    ])

    row = [
        types.InlineKeyboardButton(" ", callback_data="holiday_noop")
        for _ in range(first.weekday())
    ]

    for day in range(1, days_in_month + 1):
        d = datetime.date(year, month, day)
        events = calendar_events_for_date(d)

        # Святковий день — саме emoji-цифри (4️⃣, 8️⃣, 2️⃣5️⃣),
        # щоб календар залишався компактним і нічого не «вилазило».
        if events:
            label = _holiday_day_label(day)
        elif d == today:
            label = "🔵"
        else:
            label = str(day)

        row.append(
            types.InlineKeyboardButton(
                label,
                callback_data=f"holiday_date:{d.isoformat()}",
            )
        )
        if len(row) == 7:
            keyboard.row(*row)
            row = []

    if row:
        while len(row) < 7:
            row.append(types.InlineKeyboardButton(" ", callback_data="holiday_noop"))
        keyboard.row(*row)

    prev_month = first - datetime.timedelta(days=1)
    keyboard.row(
        types.InlineKeyboardButton(
            "⬅️", callback_data=f"holiday_month:{prev_month.year}-{prev_month.month:02d}"
        ),
        types.InlineKeyboardButton(
            "🔵 Сьогодні", callback_data=f"holiday_date:{today.isoformat()}"
        ),
        types.InlineKeyboardButton(
            "➡️", callback_data=f"holiday_month:{next_month.year}-{next_month.month:02d}"
        ),
    )
    keyboard.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"))
    return keyboard


def holiday_calendar_text(year, month):
    month_name = HOLIDAY_MONTHS_UKR[month - 1]
    return (
        f"🎉 <b>КАЛЕНДАР СВЯТ</b>\n\n"
        f"📅 <b>{month_name} {year}</b>\n\n"
        "🔢 Святкові дати позначені emoji-цифрами.\n"
        "Натисни на дату — побачиш назву свята."
    )


@bot.callback_query_handler(func=lambda call: call.data == "holiday_calendar_start")
def holiday_calendar_start(call):
    bot.answer_callback_query(call.id)
    today = datetime.date.today()
    bot.send_message(
        call.message.chat.id,
        holiday_calendar_text(today.year, today.month),
        reply_markup=holiday_calendar_keyboard(today.year, today.month),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "holiday_noop")
def holiday_noop(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("holiday_month:"))
def holiday_month_page(call):
    try:
        year, month = map(int, call.data.split(":", 1)[1].split("-"))
        datetime.date(year, month, 1)
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id, "⚠️ Помилка календаря", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        holiday_calendar_text(year, month),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=holiday_calendar_keyboard(year, month),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("holiday_date:"))
def holiday_date_selected(call):
    try:
        selected = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return

    events = calendar_events_for_date(selected)
    lines = [
        f"📅 <b>{selected.strftime('%d.%m.%Y')}</b> — {WEEKDAYS_UKR[selected.weekday()]}",
    ]
    if events:
        lines.append("")
        lines.append("🎉 <b>Свята та пам’ятні дати:</b>")
        lines.extend(f"{icon} {html.escape(name)}" for icon, name in events)
    else:
        lines.append("\n📭 На цю дату свят у календарі немає.")

    if is_vacation(selected):
        lines.append("\n🍂 <b>Канікули</b>")

    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("📅 До календаря свят", callback_data=f"holiday_calendar_return:{selected.year}-{selected.month:02d}"))
    bot.send_message(
        call.message.chat.id,
        "\n".join(lines),
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("holiday_calendar_return:"))
def holiday_calendar_return(call):
    try:
        year, month = map(int, call.data.split(":", 1)[1].split("-"))
        datetime.date(year, month, 1)
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id, "⚠️ Помилка календаря", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        holiday_calendar_text(year, month),
        reply_markup=holiday_calendar_keyboard(year, month),
        parse_mode="HTML",
    )


def homework_date_picker_keyboard(prefix="hw_view_date", start_offset=0, days=7):
    """Повноцінний календар ДЗ по місяцях."""
    keyboard = types.InlineKeyboardMarkup(row_width=7)
    today = datetime.date.today()
    start_offset = int(start_offset)
    base_date = today + datetime.timedelta(days=start_offset)
    year, month = base_date.year, base_date.month

    months_ukr = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
    keyboard.add(types.InlineKeyboardButton(f"{months_ukr[month-1]} {year}", callback_data="noop"))
    keyboard.add(*[types.InlineKeyboardButton(x, callback_data="noop") for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]])

    first = datetime.date(year, month, 1)
    days_in_month = (datetime.date(year + (month == 12), 1 if month == 12 else month + 1, 1) - first).days
    row = []
    for _ in range(first.weekday()):
        row.append(types.InlineKeyboardButton(" ", callback_data="noop"))
    digit_emoji = {
        "0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣",
        "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣",
    }

    for day in range(1, days_in_month + 1):
        d = datetime.date(year, month, day)
        has_homework = bool(HOMEWORK_STORAGE.get(d.isoformat(), {}))

        # У календарі ДЗ показуємо тільки ДЗ. Свята винесені
        # в окрему кнопку «🎉 Календар свят».
        label = (
            "".join(digit_emoji[ch] for ch in str(day))
            if has_homework else str(day)
        )
        if d == today:
            label = f"🔵 {label}"

        row.append(types.InlineKeyboardButton(label, callback_data=f"{prefix}:{d.isoformat()}"))
        if len(row) == 7:
            keyboard.row(*row); row = []
    if row:
        while len(row) < 7:
            row.append(types.InlineKeyboardButton(" ", callback_data="noop"))
        keyboard.row(*row)

    prev_month = first - datetime.timedelta(days=1)
    next_month = first + datetime.timedelta(days=days_in_month)
    keyboard.row(
        types.InlineKeyboardButton("⬅️", callback_data=f"hw_month:{prefix}:{prev_month.year}-{prev_month.month:02d}"),
        types.InlineKeyboardButton("Сьогодні", callback_data=f"{prefix}:{today.isoformat()}"),
        types.InlineKeyboardButton("➡️", callback_data=f"hw_month:{prefix}:{next_month.year}-{next_month.month:02d}"),
    )
    keyboard.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="back_to_hw_menu"))
    return keyboard


def homework_menu_keyboard(user_id=None):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    today_has_hw = bool(HOMEWORK_STORAGE.get(today.isoformat(), {}))
    tomorrow_has_hw = bool(HOMEWORK_STORAGE.get(tomorrow.isoformat(), {}))
    today_mark = "✅" if today_has_hw else "❌"
    tomorrow_mark = "✅" if tomorrow_has_hw else "❌"
    keyboard.add(
        types.InlineKeyboardButton(f"{today_mark} 📅 ДЗ на сьогодні", callback_data="hw_today"),
        types.InlineKeyboardButton(f"{tomorrow_mark} 📅 ДЗ на завтра", callback_data="hw_tomorrow"),
        types.InlineKeyboardButton("🗓 Обрати дату", callback_data="hw_date_start"),
        types.InlineKeyboardButton("🔎 Знайти ДЗ", callback_data="hw_search_start"),
    )
    # Кнопки керування ДЗ бачить тільки адміністратор.
    if user_id is not None and is_admin(user_id):
        keyboard.add(types.InlineKeyboardButton("✍️ Додати ДЗ", callback_data="add_hw"))
        keyboard.add(types.InlineKeyboardButton("🗑 Керування ДЗ", callback_data="hw_admin_manage"))
    return keyboard


def homework_menu_text():
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    today_count = len(HOMEWORK_STORAGE.get(today.isoformat(), {}))
    tomorrow_count = len(HOMEWORK_STORAGE.get(tomorrow.isoformat(), {}))
    monday = today - datetime.timedelta(days=today.weekday())
    week_count = sum(len(HOMEWORK_STORAGE.get((monday + datetime.timedelta(days=i)).isoformat(), {})) for i in range(7))
    return (
        "📚 <b>ДОМАШНІ ЗАВДАННЯ</b>\n\n"
        f"📅 <b>Сьогодні</b> — <b>{today_count}</b> предметів\n"
        f"📅 <b>Завтра</b> — <b>{tomorrow_count}</b> предметів\n"
        f"📆 <b>Цього тижня</b> — <b>{week_count}</b> предметів\n\n"
        "🎯 <b>У цьому розділі ти можеш:</b>\n"
        "• переглянути ДЗ на сьогодні або завтра;\n"
        "• відкрити будь-яку дату через календар;\n"
        "• знайти завдання за предметом або ключовим словом.\n\n"
        "🗓 <b>Дату вводити вручну не потрібно</b> — просто обери її в календарі.\n"
        "💡 У кнопках нижче: ✅ — ДЗ є, ❌ — на цей день ДЗ поки немає."
    )


def render_homework(date_obj, chat_id, message_id=None, user_id=None):
    """Показує домашнє завдання без персональних статусів."""
    date_key = date_obj.isoformat()
    day_data = HOMEWORK_STORAGE.get(date_key, {})
    # Події та свята НЕ показуємо в меню ДЗ.
    # Вони доступні тільки через окрему кнопку «🎉 Календар свят».
    lines = [f"📚 <b>ДЗ на {hw_date_label(date_obj)}</b>", ""]
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    if not day_data:
        lines.append("📭 На цю дату ДЗ ще немає.")
    else:
        for subject in SUBJECTS_LIST:
            info = day_data.get(subject)
            if not info:
                continue

            text = info.get("text") or "📸 Фото завдання"
            lines.append(f"🔹 <b>{subject}:</b> {text}")

            if info.get("photo"):
                try:
                    bot.send_photo(
                        chat_id,
                        info["photo"],
                        caption=f"📸 {subject} — {date_obj.strftime('%d.%m.%Y')}",
                    )
                except Exception:
                    pass

    keyboard.add(
        types.InlineKeyboardButton(
            "🔙 Назад до ДЗ", callback_data="back_to_hw_menu"
        )
    )
    text = "\n".join(lines)

    if message_id:
        try:
            safe_edit_message_text(
                text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML"
            )
            return True
        except Exception:
            pass

    try:
        bot.send_message(
            chat_id, text, reply_markup=keyboard, parse_mode="HTML"
        )
        return True
    except Exception:
        return False


@bot.callback_query_handler(func=lambda call: call.data == "open_homework_section")
def open_homework_section(call):
    register_user(call.from_user)
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, homework_menu_text(), reply_markup=homework_menu_keyboard(call.from_user.id), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_hw_menu")
def homework_main_menu(call):
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    try:
        safe_edit_message_text(homework_menu_text(), call.message.chat.id, call.message.message_id, reply_markup=homework_menu_keyboard(call.from_user.id), parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, homework_menu_text(), reply_markup=homework_menu_keyboard(call.from_user.id), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data in ("hw_today", "hw_tomorrow"))
def homework_date_shortcut(call):
    date_obj = datetime.date.today() if call.data == "hw_today" else datetime.date.today() + datetime.timedelta(days=1)
    bot.answer_callback_query(call.id)
    render_homework(date_obj, call.message.chat.id, call.message.message_id, user_id=call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "hw_date_start")
def homework_date_start(call):
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "🗓 <b>Обери дату для перегляду ДЗ:</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_date_picker_keyboard("hw_view_date", 0, 7),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_month:"))
def homework_month_page(call):
    try:
        _, prefix, ym = call.data.split(":", 2)
        year, month = map(int, ym.split("-"))
        datetime.date(year, month, 1)
        if prefix not in ("hw_view_date", "newhw_date", "hw_search_date", "admin_delete_hw_date", "clear_hw_picker"):
            raise ValueError
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id, "⚠️ Помилка календаря", show_alert=True)
        return
    today = datetime.date.today()
    offset = (datetime.date(year, month, 1) - today).days
    bot.answer_callback_query(call.id)
    safe_edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=homework_date_picker_keyboard(prefix, offset, 7))


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_view_date:"))
def homework_view_selected_date(call):
    try:
        date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    render_homework(date_obj, call.message.chat.id, call.message.message_id, user_id=call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "hw_search_start")
def homework_search_start(call):
    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(
        types.InlineKeyboardButton("📅 За датою", callback_data="hw_search_date_start"),
        types.InlineKeyboardButton("📚 За предметом", callback_data="hw_search_subject_menu"),
        types.InlineKeyboardButton("🔤 За ключовим словом", callback_data="hw_search_keyword_start"),
        types.InlineKeyboardButton("📋 На сьогодні", callback_data="hw_today"),
        types.InlineKeyboardButton("📋 На завтра", callback_data="hw_tomorrow"),
        types.InlineKeyboardButton("📆 На цей тиждень", callback_data="hw_week"),
        types.InlineKeyboardButton("🔙 Назад до ДЗ", callback_data="back_to_hw_menu"),
    )
    safe_edit_message_text("🔎 <b>ПОШУК ДЗ</b>\n\nОбери спосіб пошуку:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "hw_search_subject_menu")
def homework_search_subject_menu(call):
    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=2); row=[]
    for i, subject in enumerate(SUBJECTS_LIST):
        row.append(types.InlineKeyboardButton(subject, callback_data=f"hw_search_subject:{i}"))
        if len(row)==2: k.add(*row); row=[]
    if row: k.add(*row)
    k.add(types.InlineKeyboardButton("🔙 До пошуку", callback_data="hw_search_start"))
    safe_edit_message_text("📚 <b>Пошук за предметом</b>\n\nОбери предмет:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "hw_search_date_start")
def homework_search_date_start(call):
    # Для пошуку за датою використовуємо той самий календар,
    # що і для звичайного перегляду ДЗ та додавання ДЗ.
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "📅 <b>Знайти ДЗ за датою</b>\n\nОбери потрібну дату:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_date_picker_keyboard("hw_search_date", 0, 7),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_search_date:"))
def homework_search_selected_date(call):
    try:
        date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    # Показуємо ДЗ так само, як у розділі «Обрати дату».
    render_homework(
        date_obj,
        call.message.chat.id,
        call.message.message_id,
        user_id=call.from_user.id,
    )


@bot.callback_query_handler(func=lambda call: call.data == "hw_week")
def homework_week_view(call):
    """Показує ДЗ за поточний тиждень (понеділок-неділя)."""
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    lines = ["📆 <b>ДЗ НА ЦЕЙ ТИЖДЕНЬ</b>", ""]
    found_any = False
    for offset in range(7):
        d = monday + datetime.timedelta(days=offset)
        day_data = HOMEWORK_STORAGE.get(d.isoformat(), {})
        if not day_data:
            continue
        found_any = True
        lines.append(f"📅 <b>{WEEKDAYS_UKR[d.weekday()].capitalize()}, {d.strftime('%d.%m')}</b>")
        for subject, info in day_data.items():
            textv = html.escape(info.get("text") or "📸 Фото завдання")
            lines.append(f"   🔹 <b>{html.escape(subject)}</b> — {textv}")
        lines.append("")
    if not found_any:
        lines.append("📭 На цей тиждень ДЗ не знайдено.")
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("🔎 До пошуку ДЗ", callback_data="hw_search_start"))
    k.add(types.InlineKeyboardButton("📚 До меню ДЗ", callback_data="back_to_hw_menu"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text("\n".join(lines).rstrip(), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "hw_search_keyword_start")
def homework_search_keyword_start(call):
    user_states[call.from_user.id] = {"action": "waiting_hw_search_keyword"}
    bot.answer_callback_query(call.id)
    k=types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("🔙 Скасувати", callback_data="back_to_hw_menu"))
    safe_edit_message_text("🔤 <b>Пошук за ключовим словом</b>\n\nВведи слово або фразу:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_search_subject:"))
def homework_search_subject(call):
    try:
        index = int(call.data.split(":", 1)[1])
        subject = SUBJECTS_LIST[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    found = []
    for date_key, day_data in HOMEWORK_STORAGE.items():
        info = day_data.get(subject)
        if info:
            found.append((date_key, info.get("text") or "📸 Фото завдання"))

    lines = [f"🔎 <b>ДЗ з предмета: {subject}</b>", ""]
    if not found:
        lines.append("📭 Для цього предмета ДЗ не знайдено.")
    else:
        for date_key, textv in sorted(found):
            date_obj = datetime.date.fromisoformat(date_key)
            day_name = WEEKDAYS_UKR.get(date_obj.weekday(), "")
            date_text = date_obj.strftime('%d.%m.%Y')
            lines.append(
                f"🔹️ <b>{day_name}, {date_text}</b> — {textv}"
            )

    bot.send_message(
        call.message.chat.id,
        "\n".join(lines),
        reply_markup=homework_menu_keyboard(call.from_user.id),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "add_hw")

# ── Підрозділ: Додавання ДЗ ─────────────────────────────────────

def add_homework_start(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⚠️ Доступ заборонено!", show_alert=True)
        return
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    row=[]
    for i, subj in enumerate(SUBJECTS_LIST):
        row.append(types.InlineKeyboardButton(subj, callback_data=f"newhw_subject:{i}"))
        if len(row)==2:
            keyboard.add(*row); row=[]
    if row: keyboard.add(*row)
    keyboard.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="back_to_hw_menu"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text("📝 <b>Обери предмет:</b>", call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("newhw_subject:"))
def new_hw_subject(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⚠️ Доступ заборонено!", show_alert=True); return
    try: subject=SUBJECTS_LIST[int(call.data.split(":")[1])]
    except (ValueError,IndexError): bot.answer_callback_query(call.id,"Помилка!",show_alert=True); return
    user_states[call.from_user.id]={"action":"waiting_hw_date_admin","subject":subject,"chat_id":call.message.chat.id,"message_id":call.message.message_id}
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"📅 <b>{subject}</b>\n\n<b>Обери дату, на яку поставити ДЗ:</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_date_picker_keyboard("newhw_date", 0, 7),
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("newhw_date:"))
def new_hw_date_selected(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⚠️ Доступ заборонено!", show_alert=True)
        return
    try:
        date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return

    state = user_states.get(call.from_user.id, {})
    subject = state.get("subject")
    if not subject:
        bot.answer_callback_query(call.id, "⚠️ Спочатку обери предмет", show_alert=True)
        return

    state["date_key"] = date_obj.isoformat()
    state["action"] = "waiting_homework"
    state["chat_id"] = call.message.chat.id
    state["message_id"] = call.message.message_id
    user_states[call.from_user.id] = state

    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"✍️ Надішли текст або фото ДЗ з предмета <b>{subject}</b> на <b>{date_obj.strftime('%d.%m.%Y')}</b>.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "hw_admin_manage")

# ── Підрозділ: Керування ДЗ ────────────────────────────────────

def hw_admin_manage(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "⚙️ <b>Керування ДЗ</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_clear_menu_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "clear_hw_menu")

# ── Підрозділ: Очищення ДЗ ─────────────────────────────────────

def clear_hw_menu(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "🧹 <b>Очищення ДЗ</b>\n\n"
        "Обери, що потрібно очистити:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_clear_menu_keyboard(),
        parse_mode="HTML",
    )


def _clear_hw_confirm_keyboard(action):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("✅ Так, видалити", callback_data=action),
        types.InlineKeyboardButton("❌ Скасувати", callback_data="clear_hw_cancel"),
    )
    return k


@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_hw_date:"))
def clear_hw_date_confirm(call):
    if not admin_only(call):
        return
    target = call.data.split(":", 1)[1]
    if target == "today":
        date_obj = datetime.date.today()
    elif target == "tomorrow":
        date_obj = datetime.date.today() + datetime.timedelta(days=1)
    else:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return
    date_key = date_obj.isoformat()
    if date_key not in HOMEWORK_STORAGE:
        bot.answer_callback_query(call.id, "На цю дату ДЗ немає.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"⚠️ <b>Точно видалити ДЗ на {date_obj.strftime('%d.%m.%Y')}?</b>\n\n"
        "Усі ДЗ за цю дату будуть видалені.",
        call.message.chat.id, call.message.message_id,
        reply_markup=_clear_hw_confirm_keyboard(f"clear_hw_date_confirm:{date_key}"),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "clear_hw_choose_date")
def clear_hw_choose_date(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "🗓 <b>Обери дату для очищення ДЗ:</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=homework_date_picker_keyboard("clear_hw_picker", 0, 7),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_hw_picker:"))
def clear_hw_picker_selected(call):
    if not admin_only(call):
        return
    try:
        date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return
    date_key = date_obj.isoformat()
    if date_key not in HOMEWORK_STORAGE:
        bot.answer_callback_query(call.id, "На цю дату ДЗ немає.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"⚠️ <b>Точно видалити ДЗ на {date_obj.strftime('%d.%m.%Y')}?</b>\n\n"
        "Усі ДЗ за цю дату будуть видалені.",
        call.message.chat.id, call.message.message_id,
        reply_markup=_clear_hw_confirm_keyboard(f"clear_hw_date_confirm:{date_key}"),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_hw_date_confirm:"))
def clear_hw_date_confirmed(call):
    if not admin_only(call):
        return
    date_key = call.data.split(":", 1)[1]
    if date_key not in HOMEWORK_STORAGE:
        bot.answer_callback_query(call.id, "ДЗ вже видалено.", show_alert=True)
        return
    HOMEWORK_STORAGE.pop(date_key, None)
    save_data_to_file()
    refresh_main_menu(call.message.chat.id)
    log_admin_action(call.from_user.id, f"Очищено ДЗ на {date_key}")
    bot.answer_callback_query(call.id, "🗑 ДЗ за дату видалено!")
    admin_homework(call)


@bot.callback_query_handler(func=lambda call: call.data == "clear_hw_all_confirm")
def clear_hw_all_confirm(call):
    if not admin_only(call):
        return
    if not HOMEWORK_STORAGE:
        bot.answer_callback_query(call.id, "ДЗ для видалення немає.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "⚠️ <b>Точно видалити ВСІ ДЗ?</b>\n\n"
        "Буде видалено домашні завдання за всі збережені дати. Цю дію не можна скасувати.",
        call.message.chat.id, call.message.message_id,
        reply_markup=_clear_hw_confirm_keyboard("clear_hw_all_confirmed"),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "clear_hw_all_confirmed")
def clear_hw_all_confirmed(call):
    if not admin_only(call):
        return
    if not HOMEWORK_STORAGE:
        bot.answer_callback_query(call.id, "ДЗ вже очищено.", show_alert=True)
        return
    HOMEWORK_STORAGE.clear()
    save_data_to_file()
    refresh_main_menu(call.message.chat.id)
    log_admin_action(call.from_user.id, "Очищено всі ДЗ")
    bot.answer_callback_query(call.id, "🗑 Усі ДЗ видалено!")
    admin_homework(call)


@bot.callback_query_handler(func=lambda call: call.data == "clear_hw_cancel")
def clear_hw_cancel(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id, "Скасовано")
    admin_homework(call)

# =====================================================================
# 📚 РОЗКЛАД УРОКІВ ТА ЗМІНИ
# =====================================================================

# =====================================================================
# 📚 РОЗКЛАД УРОКІВ НА СЬОГОДНІ
# =====================================================================


def _schedule_change_button_label(title, date_obj, kind):
    """Повертає назву підкнопки зі статусом лише всередині підрозділу."""
    if is_vacation(date_obj) or date_obj.weekday() in (5, 6):
        active = False
    elif kind == "schedule":
        active = HAS_CHANGES_TODAY if date_obj == datetime.date.today() else HAS_CHANGES_TOMORROW
    else:
        active = HAS_BELLS_CHANGES_TODAY if date_obj == datetime.date.today() else HAS_BELLS_CHANGES_TOMORROW
    status = "🟢" if active else "🔴"
    when = "сьогодні" if date_obj == datetime.date.today() else "завтра"
    return f"{status} {title} на {when}"


@bot.callback_query_handler(func=lambda call: call.data == "schedule_menu_start")
def schedule_menu_start(call):
    """Загальний розділ розкладу — завжди нове повідомлення."""
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(
        types.InlineKeyboardButton("📅 Розклад уроків на сьогодні", callback_data="check_today_schedule"),
        types.InlineKeyboardButton("📅 Розклад уроків на завтра", callback_data="check_tomorrow_schedule"),
        types.InlineKeyboardButton("📆 Розклад уроків на тиждень", url="https://drive.google.com/file/d/1_6Itp6tjqhp6XbPMh8rdNMPIpa5IHgYx/view?usp=sharing"),
        types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"),
    )
    bot.send_message(
        call.message.chat.id,
        "📚 <b>РОЗКЛАД УРОКІВ</b>\n\nОбери, який розклад хочеш переглянути:",
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "schedule_changes_menu_start")
def schedule_changes_menu_start(call):
    """Загальний розділ змін уроків — статуси тільки на підкнопках."""
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    bot.answer_callback_query(call.id)
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(
        types.InlineKeyboardButton(_schedule_change_button_label("🔔 Зміни розкладу уроків", today, "schedule"), callback_data="check_schedule_changes_today"),
        types.InlineKeyboardButton(_schedule_change_button_label("🔔 Зміни розкладу уроків", tomorrow, "schedule"), callback_data="check_schedule_changes_tomorrow"),
        types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"),
    )
    bot.send_message(
        call.message.chat.id,
        "🔔 <b>ЗМІНИ РОЗКЛАДУ УРОКІВ</b>\n\nОбери день, для якого хочеш перевірити зміни:",
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "bells_changes_menu_start")
def bells_changes_menu_start(call):
    """Загальний розділ змін дзвінків — статуси тільки на підкнопках."""
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    bot.answer_callback_query(call.id)
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(
        types.InlineKeyboardButton(_schedule_change_button_label("🔔 Зміни розкладу дзвінків", today, "bells"), callback_data="check_bells_changes_today"),
        types.InlineKeyboardButton(_schedule_change_button_label("🔔 Зміни розкладу дзвінків", tomorrow, "bells"), callback_data="check_bells_changes_tomorrow"),
        types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"),
    )
    bot.send_message(
        call.message.chat.id,
        "🔔 <b>ЗМІНИ РОЗКЛАДУ ДЗВІНКІВ</b>\n\nОбери день, для якого хочеш перевірити зміни:",
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_today_schedule")

# ── Підрозділ: Розклад на сьогодні/завтра ──────────────────────

def callback_today_schedule(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    today = datetime.date.today()
    today_date = today.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[today.weekday()]
    weekday = today.weekday()
    offline_status = is_week_offline(today)

    if is_vacation(today):
        schedule_text = (
            f"<b>📚 Розклад уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Уроків немає."
        )
    elif weekday in [5, 6]:
        schedule_text = (
            f"<b>📚 Розклад уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Уроків немає."
        )
    else:
        if offline_status:
            subject_mon_3 = "Здоров'я, безпека та добробут"
            subject_fri_7 = "Історія України"
        else:
            subject_mon_3 = "Підприємництво і фінансова грамотність"
            subject_fri_7 = "Громадянська освіта"

        schedules = {
            0: (
                "1. Фізика\n"
                "2. Зарубіжна література\n"
                f"3. {subject_mon_3}\n"
                "4. Англійська мова\n"
                "5. Алгебра\n"
                "6. Фізична культура\n"
                "7. Біологія"
            ),
            1: (
                "1. Географія\n"
                "2. Хімія\n"
                "3. Українська мова\n"
                "4. Всесвітня історія\n"
                "5. Геометрія\n"
                "6. Українська література"
            ),
            2: (
                "1. Українська мова\n"
                "2. Історія України\n"
                "3. Англійська мова\n"
                "4. Фізична культура\n"
                "5. Алгебра\n"
                "6. Інформатика\n"
                "7. Мистецтво"
            ),
            3: (
                "1. Англійська мова\n"
                "2. Географія\n"
                "3. Технології\n"
                "4. Фізика\n"
                "5. Геометрія\n"
                "6. Українська література"
            ),
            4: (
                "1. Українська мова\n"
                "2. Алгебра\n"
                "3. Біологія\n"
                "4. Фізична культура\n"
                "5. Інформатика\n"
                "6. Хімія\n"
                f"7. {subject_fri_7}"
            ),
        }
        day_schedule = schedules.get(weekday, "Розклад відсутній")
        schedule_text = (
            f"<b>📚 Розклад уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            f"{day_schedule}"
            f"{BELLS_SCHEDULE_TEXT}"
        )

    bot.send_message(call.message.chat.id, schedule_text, parse_mode="HTML")

# =====================================================================
# 📚 РОЗКЛАД УРОКІВ НА ЗАВТРА
# =====================================================================

@bot.callback_query_handler(func=lambda call: call.data == "check_tomorrow_schedule")
def callback_tomorrow_schedule(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]
    weekday = tomorrow.weekday()
    offline_status = is_next_week_offline()

    if is_vacation(tomorrow):
        schedule_text = (
            f"<b>📚 Розклад уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Уроків немає."
        )
    elif weekday in [5, 6]:
        schedule_text = (
            f"<b>📚 Розклад уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Уроків немає."
        )
    else:
        if offline_status:
            subject_mon_3 = "Здоров'я, безпека та добробут"
            subject_fri_7 = "Історія України"
        else:
            subject_mon_3 = "Підприємництво і фінансова грамотність"
            subject_fri_7 = "Громадянська освіта"

        schedules = {
            0: (
                "1. Фізика\n"
                "2. Зарубіжна література\n"
                f"3. {subject_mon_3}\n"
                "4. Англійська мова\n"
                "5. Алгебра\n"
                "6. Фізична культура\n"
                "7. Біологія"
            ),
            1: (
                "1. Географія\n"
                "2. Хімія\n"
                "3. Українська мова\n"
                "4. Всесвітня історія\n"
                "5. Геометрія\n"
                "6. Українська література"
            ),
            2: (
                "1. Українська мова\n"
                "2. Історія України\n"
                "3. Англійська мова\n"
                "4. Фізична культура\n"
                "5. Алгебра\n"
                "6. Інформатика\n"
                "7. Мистецтво"
            ),
            3: (
                "1. Англійська мова\n"
                "2. Географія\n"
                "3. Технології\n"
                "4. Фізика\n"
                "5. Геометрія\n"
                "6. Українська література"
            ),
            4: (
                "1. Українська мова\n"
                "2. Алгебра\n"
                "3. Біологія\n"
                "4. Фізична культура\n"
                "5. Інформатика\n"
                "6. Хімія\n"
                f"7. {subject_fri_7}"
            ),
        }
        day_schedule = schedules.get(weekday, "Розклад відсутній")
        schedule_text = (
            f"<b>📚 Розклад уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            f"{day_schedule}"
            f"{BELLS_SCHEDULE_TEXT}"
        )

    bot.send_message(call.message.chat.id, schedule_text, parse_mode="HTML")


@bot.callback_query_handler(
    func=lambda call: call.data == "check_schedule_changes_today"
)

# ── Підрозділ: Зміни розкладу ──────────────────────────────────

def callback_schedule_changes_today(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    today = datetime.date.today()
    today_date = today.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[today.weekday()]

    if is_vacation(today):
        schedule_text = (
            f"<b>🔔 Зміни розкладу уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Змін розкладу немає."
        )
    elif today.weekday() in [5, 6]:
        schedule_text = (
            f"<b>🔔 Зміни розкладу уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Змін розкладу немає."
        )
    else:
        if HAS_CHANGES_TODAY:
            schedule_text = (
                f"<b>🔔 Зміни розкладу уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
                f"{CHANGES_TODAY_YES_TEXT}"
            )
        else:
            schedule_text = (
                f"<b>🔔 Зміни розкладу уроків на сьогодні ({day_name}, {today_date}):</b>\n\n"
                f"{CHANGES_TODAY_NO_TEXT}"
            )

    bot.send_message(call.message.chat.id, schedule_text, parse_mode="HTML")


@bot.callback_query_handler(
    func=lambda call: call.data == "check_schedule_changes_tomorrow"
)
def callback_schedule_changes_tomorrow(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]

    if is_vacation(tomorrow):
        schedule_text = (
            f"<b>🔔 Зміни розкладу уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Змін розкладу немає."
        )
    elif tomorrow.weekday() in [5, 6]:
        schedule_text = (
            f"<b>🔔 Зміни розкладу уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Змін розкладу немає."
        )
    else:
        if HAS_CHANGES_TOMORROW:
            schedule_text = (
                f"<b>🔔 Зміни розкладу уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
                f"{CHANGES_TOMORROW_YES_TEXT}"
            )
        else:
            schedule_text = (
                f"<b>🔔 Зміни розкладу уроків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
                f"{CHANGES_TOMORROW_NO_TEXT}"
            )

    bot.send_message(call.message.chat.id, schedule_text, parse_mode="HTML")


@bot.callback_query_handler(
    func=lambda call: call.data == "check_bells_changes_today"
)
def callback_bells_changes_today(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    today = datetime.date.today()
    today_date = today.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[today.weekday()]

    if is_vacation(today):
        bells_text = (
            f"<b>🔔 Зміни розкладу дзвінків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Змін розкладу дзвінків немає."
        )
    elif today.weekday() in [5, 6]:
        bells_text = (
            f"<b>🔔 Зміни розкладу дзвінків на сьогодні ({day_name}, {today_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Змін розкладу дзвінків немає."
        )
    else:
        if HAS_BELLS_CHANGES_TODAY:
            bells_text = (
                f"<b>🔔 Зміни розкладу дзвінків на сьогодні ({day_name}, {today_date}):</b>\n\n"
                f"{BELLS_CHANGES_TODAY_YES_TEXT}"
            )
        else:
            bells_text = (
                f"<b>🔔 Зміни розкладу дзвінків на сьогодні ({day_name}, {today_date}):</b>\n\n"
                f"{BELLS_CHANGES_TODAY_NO_TEXT}"
            )

    bot.send_message(call.message.chat.id, bells_text, parse_mode="HTML")


@bot.callback_query_handler(
    func=lambda call: call.data == "check_bells_changes_tomorrow"
)
def callback_bells_changes_tomorrow(call):
    register_user(call.from_user)
    track_user_event(call.from_user.id, "schedule_viewed")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]

    if is_vacation(tomorrow):
        bells_text = (
            f"<b>🔔 Зміни розкладу дзвінків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Змін розкладу дзвінків немає."
        )
    elif tomorrow.weekday() in [5, 6]:
        bells_text = (
            f"<b>🔔 Зміни розкладу дзвінків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Змін розкладу дзвінків немає."
        )
    else:
        if HAS_BELLS_CHANGES_TOMORROW:
            bells_text = (
                f"<b>🔔 Зміни розкладу дзвінків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
                f"{BELLS_CHANGES_TOMORROW_YES_TEXT}"
            )
        else:
            bells_text = (
                f"<b>🔔 Зміни розкладу дзвінків на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
                f"{BELLS_CHANGES_TOMORROW_NO_TEXT}"
            )

    bot.send_message(call.message.chat.id, bells_text, parse_mode="HTML")


# =====================================================================
# 🏫 ІНШІ РОЗДІЛИ ТА СТАТУСИ (ШКОЛА, СТОЛОВА, ЧЕРГУВАННЯ)
# =====================================================================


@bot.callback_query_handler(func=lambda call: call.data == "check_school")

# ── Підрозділ: Школа, столова та чергування ───────────────────

def callback_school_status(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]
    offline_status = is_next_week_offline()

    if is_vacation(tomorrow):
        school_news_text = (
            f"<b>🏫 Інформація щодо навчання на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Зараз тривають канікули!</b> Відпочивай і набирайся сил."
        )
    elif tomorrow.weekday() in [5, 6]:
        school_news_text = (
            f"<b>🏫 Інформація щодо навчання на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Завтра до школи не потрібно."
        )
    elif not offline_status:
        school_news_text = (
            f"<b>🏫 Інформація щодо навчання на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "💻 Завтра ми навчаємося <b>онлайн</b>!"
        )
    else:
        school_news_text = (
            f"<b>🏫 Інформація щодо навчання на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "Завтра ми навчаємося <b>офлайн (у школі / в укритті)</b>!\n"
        )

    bot.send_message(call.message.chat.id, school_news_text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "check_canteen")
def callback_canteen_status(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]
    offline_status = is_next_week_offline()

    if is_vacation(tomorrow):
        canteen_text = (
            f"<b>🍽 Меню харчування на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Меню харчування немає."
        )
    elif tomorrow.weekday() in [5, 6]:
        canteen_text = (
            f"<b>🍽 Меню харчування на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Меню харчування немає."
        )
    elif not offline_status:
        canteen_text = (
            f"<b>🍽 Меню харчування на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "💻 Зараз онлайн-навчання, тому <b>меню харчування неактуальне</b>."
        )
    else:
        canteen_text = (
            f"<b>🍽 Меню харчування на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "<blockquote>Пюре, гуляш, огірок, яйце, б/д із сиром, сік.</blockquote>"
        )

    bot.send_message(call.message.chat.id, canteen_text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "check_duty")
def callback_duty_status(call):
    global current_duty_person
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%d.%m.%Y")
    day_name = WEEKDAYS_UKR[tomorrow.weekday()]
    offline_status = is_next_week_offline()

    if is_vacation(tomorrow):
        duty_text = (
            f"<b>📅 Графік чергування в класі на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🌴 <b>Канікули!</b> Чергування у школі відсутнє."
        )
    elif tomorrow.weekday() in [5, 6]:
        duty_text = (
            f"<b>📅 Графік чергування в класі на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "🛌 <b>Вихідний день!</b> Чергування немає."
        )
    elif not offline_status:
        duty_text = (
            f"<b>📅 Графік чергування в класі на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            "💻 Зараз триває онлайн-навчання, тому <b>чергування у школі скасовано</b>."
        )
    else:
        duty_text = (
            f"<b>📅 Графік чергування в класі на завтра ({day_name}, {tomorrow_date}):</b>\n\n"
            f"Завтра чергує: <blockquote><tg-spoiler>{current_duty_person}</tg-spoiler></blockquote>\n\n"
            "<blockquote><i>💡 Щоб побачити, хто чергує, натисніть на зашифроване слово.</i></blockquote>"
        )

    bot.send_message(call.message.chat.id, duty_text, parse_mode="HTML")


# =====================================================================
# 🗳 ОБРОБНИКИ ПОДІЙ ОПИТУВАННЯ ТА РЕЙТИНГУ
# =====================================================================


@bot.callback_query_handler(func=lambda call: call.data == "class_poll")

# ── Підрозділ: Створення та запуск опитування ─────────────────

def callback_class_poll(call):
    """Відкриває налаштування часу перед запуском опитування."""
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id,
                "❌ Цю кнопку може натискати лише адміністратор!",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if poll_is_active():
        # Якщо опитування вже запущене, кнопку можна натискати скільки завгодно разів:
        # не показуємо помилку, а надсилаємо актуальне опитування окремим повідомленням.
        bot.answer_callback_query(call.id, "🗳 Відкриваю поточне опитування.")
        bot.send_message(
            call.message.chat.id,
            get_poll_text(),
            reply_markup=get_poll_keyboard(),
            parse_mode="HTML",
        )
        return

    # Нове опитування завжди створюється для завтрашнього дня.
    # Тому якщо сьогодні, наприклад, 05.10 — опитування буде на 06.10;
    # 06.10 → на 07.10 і т.д. Стару дату з JSON не переносимо на новий день.
    poll_data["target_date"] = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    poll_data["ends_at"] = ""
    poll_data["completed_at"] = ""
    save_data_to_file()

    bot.answer_callback_query(call.id)
    show_poll_start_time_menu(call)


def poll_start_time_keyboard():
    """Кнопки вибору точного часу завершення голосування."""
    k = types.InlineKeyboardMarkup(row_width=3)
    target = poll_target_date()
    deadline_date = target - datetime.timedelta(days=1)
    current_ends = poll_data.get("ends_at") or ""

    times = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]
    buttons = []
    for time_text in times:
        mark = "✅ " if current_ends.endswith(f"T{time_text}:00") else ""
        buttons.append(
            types.InlineKeyboardButton(
                f"{mark}{time_text}",
                callback_data=f"poll_start_time:{time_text}",
            )
        )
    for i in range(0, len(buttons), 3):
        k.row(*buttons[i:i + 3])

    k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="poll_start_cancel"))
    return k


def show_poll_start_time_menu(call):
    target = poll_target_date()
    deadline_date = target - datetime.timedelta(days=1)
    text = (
        "🗳 <b>ПІДГОТОВКА ОПИТУВАННЯ</b>\n\n"
        f"📅 Дата чергування: <b>{target.strftime('%d.%m.%Y')}</b> ({WEEKDAYS_UKR[target.weekday()]})\n\n"
        f"⏰ Обери, <b>до котрої години</b> можна голосувати "
        f"(<b>{deadline_date.strftime('%d.%m.%Y')}</b>):\n\n"
        "Після вибору часу це повідомлення перетвориться на саме опитування.\n"
        "Після дедлайну голосування автоматично заблокується, а адміністратор зможе натиснути «🏁 Завершити опитування»."
    )
    bot.send_message(
        call.message.chat.id,
        text,
        reply_markup=poll_start_time_keyboard(),
        parse_mode="HTML",
    )

@bot.callback_query_handler(func=lambda call: call.data == "poll_start_cancel")
def poll_start_cancel(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id, "Скасовано")
    admin_poll_menu(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("poll_start_time:"))
def poll_start_time_selected(call):
    if not admin_only(call):
        return
    if poll_is_active():
        bot.answer_callback_query(call.id, "🟢 Опитування вже активне.", show_alert=True)
        return

    time_text = call.data.split(":", 1)[1]
    try:
        selected_time = datetime.time.fromisoformat(time_text)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ Невірний час", show_alert=True)
        return

    target = poll_target_date()
    deadline = datetime.datetime.combine(target - datetime.timedelta(days=1), selected_time)

    poll_data["is_active"] = True
    poll_data["started_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    poll_data["ends_at"] = deadline.isoformat(timespec="seconds")
    poll_data["completed_at"] = ""
    poll_data["voted_users"] = {}
    for key in poll_data["votes"]:
        poll_data["votes"][key] = 0
    save_data_to_file()
    log_admin_action(call.from_user.id, f"Запущено опитування, дедлайн {deadline.strftime('%d.%m.%Y %H:%M')}")

    bot.answer_callback_query(call.id, f"🗳 Запущено до {deadline.strftime('%d.%m %H:%M')}")
    # Редагуємо ТІЛЬКИ повідомлення з вибором часу — не головне меню.
    safe_edit_message_text(
        get_poll_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_poll_keyboard(),
        parse_mode="HTML",
    )
    # Після запуску лише оновлюємо індикатор 🗳 у головному меню.
    refresh_main_menu(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("vote_"))

# ── Підрозділ: Голосування та результати ──────────────────────

def callback_vote_action(call):
    register_user(call.from_user)
    user_id = call.from_user.id
    user_fullname = (
        f"{call.from_user.first_name or ''} {call.from_user.last_name or ''}"
    ).strip()

    if not poll_voting_open():
        try:
            message = "🔒 Час голосування вже вийшов. Голоси більше не приймаються!" if poll_is_active() else "❌ Це опитування вже завершено!"
            bot.answer_callback_query(call.id, message, show_alert=True)
        except Exception:
            pass
        return

    user_id_str = str(user_id)
    if user_id_str in poll_data["voted_users"]:
        voted_for = poll_data["voted_users"][user_id_str]["candidate"]
        try:
            bot.answer_callback_query(
                call.id,
                f"❌ Ти вже проголосував(-ла) за: {voted_for}!\nЯкщо хочеш змінити голос, натисни кнопку «🔄 Змінити мій голос».",
                show_alert=True,
            )
        except Exception:
            pass
        return

    candidate_name = call.data.replace("vote_", "")

    if candidate_name in poll_data["votes"]:
        poll_data["votes"][candidate_name] += 1
        poll_data["voted_users"][user_id_str] = {
            "candidate": candidate_name,
            "name": user_fullname if user_fullname else f"Користувач {user_id}",
        }

        try:
            bot.answer_callback_query(
                call.id, f"✅ Твій голос за {candidate_name} зараховано!"
            )
        except Exception:
            pass

        try:
            safe_edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=get_poll_text(),
                reply_markup=get_poll_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data == "reset_my_vote")
def callback_reset_vote(call):
    user_id = call.from_user.id

    if not poll_voting_open():
        try:
            message = "🔒 Час голосування вже вийшов. Голоси більше не приймаються!" if poll_is_active() else "❌ Це опитування вже завершено!"
            bot.answer_callback_query(call.id, message, show_alert=True)
        except Exception:
            pass
        return

    user_id_str = str(user_id)
    if user_id_str not in poll_data["voted_users"]:
        try:
            bot.answer_callback_query(
                call.id,
                "ℹ️ Ти ще не голосував(-ла) у цьому опитуванні.",
                show_alert=True,
            )
        except Exception:
            pass
        return

    old_candidate = poll_data["voted_users"][user_id_str]["candidate"]

    if (
        old_candidate in poll_data["votes"]
        and poll_data["votes"][old_candidate] > 0
    ):
        poll_data["votes"][old_candidate] -= 1

    del poll_data["voted_users"][user_id_str]

    try:
        bot.answer_callback_query(
            call.id,
            "🔄 Твій голос скинуто! Тепер можеш проголосувати знову.",
        )
    except Exception:
        pass

    try:
        safe_edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=get_poll_text(),
            reply_markup=get_poll_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "finish_poll")
def callback_finish_poll(call):
    global current_duty_person

    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id,
                "❌ Завершувати опитування може лише адміністратор!",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if not poll_is_active():
        try:
            bot.answer_callback_query(
                call.id, "ℹ️ Опитування вже було завершено.", show_alert=True
            )
        except Exception:
            pass
        return

    poll_data["is_active"] = False
    poll_data["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    try:
        bot.answer_callback_query(
            call.id, "✅ Опитування успішно завершено!", show_alert=False
        )
    except Exception:
        pass

    votes_dict = poll_data["votes"]
    max_votes = max(votes_dict.values()) if votes_dict else 0

    results_text = "✨ <b>ПІДСУМКИ ГОЛОСУВАННЯ</b> ✨\n"
    results_text += "━━━━━━━━━━━━━━━━━━━━\n"
    results_text += "📊 <b>Офіційні результати вибору чергового:</b>\n\n"

    for cand, count in votes_dict.items():
        vote_word = get_vote_word(count)
        if count == max_votes and max_votes > 0:
            results_text += f"👑 <b>{cand} — {count} {vote_word}</b>\n"
        else:
            results_text += f"▫️ {cand} — {count} {vote_word}\n"

    results_text += "━━━━━━━━━━━━━━━━━━━━\n"

    if max_votes > 0:
        chosen_winner = get_next_duty_person(poll_data)
        current_duty_person = chosen_winner
        save_data_to_file()

        winners = [cand for cand, cnt in votes_dict.items() if cnt == max_votes]
        action_text = "набрала" if chosen_winner in GIRLS_LIST else "набрав"

        if len(winners) > 1:
            results_text += f"📌 <b>Нічия ({max_votes} голосів). За меншою кількістю чергувань обрано:</b> <b>{chosen_winner}</b>\n\n<i>Дякуємо всім за активність у голосуванні!</i> 🤝"
        else:
            winners_str = ", ".join(winners)
            results_text += f"📌 <b>Найбільше голосів {action_text}:</b> {winners_str}\n\n<i>Дякуємо всім за активність у голосуванні!</i> 🤝"
    else:
        results_text += (
            "📌 <i>Голосування завершено без активності з боку класу.</i>"
        )

    try:
        safe_edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=results_text,
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        pass

    save_data_to_file()
    refresh_main_menu(call.message.chat.id)

    private_report = (
        "📋 <b>ДЕТАЛЬНИЙ ЗВІТ ПРО ГОЛОСУВАННЯ</b> 📋\n━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    private_report += "<b>Хто за кого проголосував:</b>\n"

    voted_user_ids_set = set(poll_data["voted_users"].keys())

    if poll_data["voted_users"]:
        for uid, data_info in poll_data["voted_users"].items():
            name = data_info["name"]
            candidate = data_info["candidate"]
            private_report += (
                f"• <b>{name}</b> ➔ проголосував(-ла) за <b>{candidate}</b>\n"
            )
    else:
        private_report += "<i>Ніхто не проголосував.</i>\n"

    private_report += "\n━━━━━━━━━━━━━━━━━━━━\n"
    private_report += "❌ <b>Хто НЕ проголосував (пропустив):</b>\n"

    not_voted_list = []
    for student_obj in ALL_CLASS_STUDENTS:
        student_name = student_obj["name"]
        has_voted = False

        if (
            student_name == "Денис Павлов"
            and str(ADMIN_ID) in voted_user_ids_set
        ):
            has_voted = True
        else:
            for uid, data_info in poll_data["voted_users"].items():
                if student_name.lower() in data_info["name"].lower():
                    has_voted = True
                    break

        if not has_voted:
            not_voted_list.append(student_name)

    if not_voted_list:
        for student in not_voted_list:
            private_report += f"▫ {student}\n"
    else:
        private_report += "🎉 <b>Всі учні проголосували! Молодці!</b>\n"

    admin_markup = types.InlineKeyboardMarkup()
    admin_markup.add(
        types.InlineKeyboardButton(
            "📊 Рейтинг чергувань", callback_data="show_duty_rating"
        )
    )

    try:
        bot.send_message(
            ADMIN_ID, private_report, reply_markup=admin_markup, parse_mode="HTML"
        )
    except Exception as e:
        print(f"Не вдалося надіслати звіт в ЛС адміністратору: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "show_duty_rating")
def callback_show_duty_rating(call):
    if call.from_user.id not in ADMIN_IDS:
        try:
            bot.answer_callback_query(
                call.id,
                "❌ Цю кнопку може переглядати лише адміністратор!",
                show_alert=True,
            )
        except Exception as e:
            logging.warning(f"Не вдалося відповісти на callback_query: {e}")
        return

    try:
        bot.answer_callback_query(
            call.id, "✅ Рейтинг надіслано тобі в особисті повідомлення!"
        )
    except Exception as e:
        logging.warning(f"Не вдалося відповісти на callback_query: {e}")

    rating_text = "📊 <b>РЕЙТИНГ ЧЕРГУВАНЬ У КЛАСІ</b> 📊\n"
    rating_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    full_rating = {}
    for student in ALL_CLASS_STUDENTS:
        name = student["name"]
        full_rating[name] = duty_ratings.get(name, 0)

    sorted_rating = sorted(
        full_rating.items(), key=lambda x: x[1], reverse=True
    )

    for idx, (name, count) in enumerate(sorted_rating, start=1):
        if idx == 1 and count > 0:
            medal = "🥇"
        elif idx == 2 and count > 0:
            medal = "🥈"
        elif idx == 3 and count > 0:
            medal = "🥉"
        else:
            medal = "▫️"

        if count % 10 == 1 and count % 100 != 11:
            times_word = "раз"
        elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
            times_word = "рази"
        else:
            times_word = "разів"

        rating_text += f"{medal} <b>{name}</b> — {count} {times_word}\n"

    rating_text += "\n━━━━━━━━━━━━━━━━━━━━\n"
    rating_text += "<i>📈 Статистика оновлюється на основі проведених чергувань.</i>"

    try:
        bot.send_message(ADMIN_ID, rating_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не вдалося надіслати рейтинг в ЛС адміністратору: {e}")


# =====================================================================
# ⚙️ АДМІН-ПАНЕЛЬ
# =====================================================================
#
# Розділи адмін-панелі:
#   1. Доступ та безпека
#   2. Головне меню адміна
#   3. Користувачі
#   4. Статистика
#   5. Журнал дій
#   6. Розклад та дзвінки
#   7. ДЗ
#   8. Повідомлення вчителів
#   9. Опитування
#  10. Розсилка
#  11. Резервні копії
#  12. Службові інструменти
# =====================================================================

# ── Підрозділ: Доступ до адмін-панелі ──────────────────────────

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def admin_only(call):
    if not is_admin(call.from_user.id):
        try:
            bot.answer_callback_query(call.id, "⚠️ Доступ заборонено!", show_alert=True)
        except Exception:
            pass
        return False
    return True


def admin_cancel_keyboard(extra_buttons=None):
    """Клавіатура для будь-якого введення в адмін-панелі."""
    k = types.InlineKeyboardMarkup(row_width=1)
    if extra_buttons:
        for button in extra_buttons:
            k.add(button)
    k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_cancel_input"))
    return k


@bot.callback_query_handler(func=lambda call: call.data == "admin_cancel_input")
def admin_cancel_input(call):
    if not admin_only(call):
        return
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "❌ Дію скасовано")
    try:
        safe_edit_message_text(
            admin_panel_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            "❌ <b>Дію скасовано.</b>",
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_no_changes:"))
def admin_no_changes(call):
    if not admin_only(call):
        return

    parts = call.data.split(":", 2)
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "⚠️ Помилка", show_alert=True)
        return

    kind, target = parts[1], parts[2]

    if kind == "schedule":
        action = f"Встановлено: змін уроків немає ({target})"
        message = "✅ Зміни уроків вимкнено."

    elif kind == "bells":
        action = f"Встановлено: змін дзвінків немає ({target})"
        message = "✅ Зміни дзвінків вимкнено."

    else:
        bot.answer_callback_query(call.id, "⚠️ Невідома дія", show_alert=True)
        return

    # Ці змінні змінюються всередині функції.
    # Перепризначаємо через globals(), щоб не залежати від області видимості.
    if kind == "schedule":
        if target == "today":
            globals()["HAS_CHANGES_TODAY"] = False
        else:
            globals()["HAS_CHANGES_TOMORROW"] = False
    else:
        if target == "today":
            globals()["HAS_BELLS_CHANGES_TODAY"] = False
        else:
            globals()["HAS_BELLS_CHANGES_TOMORROW"] = False

    user_states.pop(call.from_user.id, None)
    save_data_to_file()
    log_admin_action(call.from_user.id, action)
    bot.answer_callback_query(call.id, "Готово!")
    bot.send_message(
        call.message.chat.id,
        message,
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


# ── Підрозділ: Структура адмін-панелі ─────────────────────────

def admin_panel_keyboard():
    """Головна адмін-панель: тільки розділи, без дублювання функцій."""
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("📚 Контент", callback_data="admin_section_content"),
        types.InlineKeyboardButton("👥 Користувачі та комунікація", callback_data="admin_section_users"),
    )
    k.add(
        types.InlineKeyboardButton("📊 Контроль", callback_data="admin_section_control"),
        types.InlineKeyboardButton("💾 Система", callback_data="admin_section_system"),
    )
    k.add(
        types.InlineKeyboardButton("🔄 Оновити панель", callback_data="admin_panel_refresh"),
        types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_main_menu"),
    )
    return k


def _admin_section_keyboard(section):
    k = types.InlineKeyboardMarkup(row_width=2)
    if section == "content":
        k.add(
            types.InlineKeyboardButton("📚 ДЗ", callback_data="admin_homework"),
            types.InlineKeyboardButton("📢 Повідомлення", callback_data="open_teacher_msg_section"),
        )
        k.add(
            types.InlineKeyboardButton("📌 Важливе повідомлення", callback_data="admin_pinned_message"),
            types.InlineKeyboardButton("📅 Розклад", callback_data="admin_schedule"),
        )
        k.add(types.InlineKeyboardButton("🗳 Опитування", callback_data="admin_poll_menu"))
    elif section == "users":
        k.add(
            types.InlineKeyboardButton("👥 Користувачі", callback_data="admin_users"),
            types.InlineKeyboardButton("📣 Розсилка", callback_data="admin_broadcast_start"),
        )
        k.add(types.InlineKeyboardButton("📅 Заплановані розсилки", callback_data="admin_scheduled_broadcasts"))
    elif section == "control":
        k.add(
            types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
            types.InlineKeyboardButton("📈 Активність", callback_data="admin_activity"),
        )
        k.add(types.InlineKeyboardButton("🛡 Журнал дій", callback_data="admin_logs"))
    elif section == "system":
        k.add(
            types.InlineKeyboardButton("💾 Резервна копія", callback_data="admin_backup"),
            types.InlineKeyboardButton("⚙️ Налаштування", callback_data="admin_settings"),
        )
        k.add(
            types.InlineKeyboardButton("🔧 Система", callback_data="admin_system"),
            types.InlineKeyboardButton("🛠 Інструменти", callback_data="admin_tools"),
        )
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    return k


def _show_admin_section(call, title, description, section):
    if not admin_only(call):
        return
    if not admin_has_permission(call.from_user.id, section):
        bot.answer_callback_query(call.id, "⚠️ У твоєї ролі немає доступу до цього розділу.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{title}\n\n{description}\n\n👇 <b>Оберіть потрібну функцію:</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=_admin_section_keyboard(section),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_section_content")
def admin_section_content(call):
    _show_admin_section(
        call,
        "📚 <b>КОНТЕНТ</b>",
        "Усе, що адміністратор додає та редагує для учнів: ДЗ, повідомлення, важливі повідомлення, розклад та опитування.",
        "content",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_section_users")
def admin_section_users(call):
    _show_admin_section(
        call,
        "👥 <b>КОРИСТУВАЧІ ТА КОМУНІКАЦІЯ</b>",
        "Керування користувачами та надсилання повідомлень аудиторії бота.",
        "users",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_section_control")
def admin_section_control(call):
    _show_admin_section(
        call,
        "📊 <b>КОНТРОЛЬ</b>",
        "Статистика, активність користувачів та журнал адміністративних дій.",
        "control",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_section_system")
def admin_section_system(call):
    _show_admin_section(
        call,
        "💾 <b>СИСТЕМА</b>",
        "Резервні копії, налаштування, стан бота та службові інструменти.",
        "system",
    )


def _last_backup_info():
    files = _backup_snapshot_files() if "_backup_snapshot_files" in globals() else []
    if not files:
        return "ще не створювався"
    path = files[0]
    try:
        dt = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "невідомо"


def _weather_status_text():
    with WEATHER_CACHE_LOCK:
        data = WEATHER_CACHE.get("data")
        updated = WEATHER_CACHE.get("updated_at", 0.0)
    if data is None:
        return "⚪ ще не перевірено"
    cache_minutes = int(admin_setting("weather_cache_minutes", 10) or 10)
    if updated and time.time() - updated < max(1, cache_minutes) * 60:
        return f"🟢 API + кеш ({cache_minutes} хв.)"
    return "🟡 кеш прострочений"


def admin_panel_text():
    """Центр керування: короткий стан усіх ключових систем прямо в адмін-панелі."""
    today = datetime.date.today()
    active_today = 0
    for u in USERS_STORAGE.values():
        try:
            if datetime.datetime.fromisoformat(u.get("last_seen", "")).date() == today:
                active_today += 1
        except (ValueError, TypeError):
            pass
    poll_active = poll_is_active() if "poll_is_active" in globals() else bool(poll_data.get("is_active"))
    poll_status = "🟢 активне" if poll_active else ("🔴 завершене" if poll_data.get("completed_at") else "⚪ немає активного")
    json_ok = os.path.exists(BACKUP_FILE_PATH)
    sqlite_ok = os.path.exists(SQLITE_FILE_PATH)
    hw_ok = isinstance(HOMEWORK_STORAGE, dict)
    msg_ok = isinstance(TEACHER_MESSAGE_STORAGE, dict)
    return (
        "⚙️ <b>ЦЕНТР КЕРУВАННЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 Бот: <b>🟢 Працює</b>\n"
        f"💾 JSON: <b>{'🟢' if json_ok else '🔴'}</b>\n"
        f"🗄 SQLite: <b>{'🟢' if sqlite_ok else '🔴'}</b>\n"
        f"🌤 Погода: <b>{_weather_status_text()}</b>\n"
        f"📚 ДЗ: <b>{'🟢' if hw_ok else '🔴'}</b>\n"
        f"📢 Повідомлення: <b>{'🟢' if msg_ok else '🔴'}</b>\n"
        f"🗳 Опитування: <b>{poll_status}</b>\n\n"
        f"👥 Користувачів: <b>{len(USERS_STORAGE)}</b>\n"
        f"🟢 Активних сьогодні: <b>{active_today}</b>\n\n"
        f"🕒 <b>Останній backup:</b> {_last_backup_info()}\n\n"
        "🔔 <b>ОСТАННІ ПОДІЇ</b>\n"
        + ("\n".join([f"• {html.escape(str(x.get('text','')))[:80]}" for x in ADMIN_EVENT_LOG[-3:][::-1]]) if ADMIN_EVENT_LOG else "📭 Подій немає")
        + "\n\n👇 <b>Оберіть розділ:</b>"
    )


# ---------------------------------------------------------------------
# 🔐 1. ДОСТУП ТА ГОЛОВНЕ МЕНЮ АДМІНА
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data in ("admin_panel", "admin_panel_refresh"))
def admin_panel(call):
    if not admin_only(call):
        return
    register_user(call.from_user)
    if call.data == "admin_panel_refresh":
        bot.answer_callback_query(call.id, "🔄 Панель оновлено")
    else:
        bot.answer_callback_query(call.id)

    # Не створюємо нове повідомлення щоразу: панель оновлюється на місці.
    try:
        safe_edit_message_text(
            admin_panel_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        try:
            bot.send_message(
                call.message.chat.id,
                admin_panel_text(),
                reply_markup=admin_panel_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------
# 🛠 12. СЛУЖБОВІ ІНСТРУМЕНТИ
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_tools")

# ── Підрозділ: Інструменти адміністратора ─────────────────────

def admin_tools(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("📤 Вивантажити JSON", callback_data="admin_export_json"),
        types.InlineKeyboardButton("📈 Активність", callback_data="admin_activity"),
        types.InlineKeyboardButton("🗳 Скинути голоси", callback_data="admin_reset_poll"),
        types.InlineKeyboardButton("🧪 Перевірити систему", callback_data="admin_system_check"),
        types.InlineKeyboardButton("🧹 Очистити стани", callback_data="admin_clear_states"),
        types.InlineKeyboardButton("💾 Зберегти дані", callback_data="admin_force_save"),
        types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"),
    )
    bot.send_message(
        call.message.chat.id,
        "🛠 <b>Службові інструменти</b>\n\n"
        "📤 Вивантажити поточний JSON-файл.\n"
        "📈 Переглянути активність користувачів.\n"
        "🗳 Скинути голоси поточного опитування.\n"
        "🧪 Перевірити, що бот відповідає.\n"
        "🧹 Очистити тимчасові стани користувачів.\n"
        "💾 Примусово зберегти всі дані.",
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_export_json")
def admin_export_json(call):
    if not admin_only(call):
        return
    save_data_to_file()
    try:
        with open(BACKUP_FILE_PATH, "rb") as f:
            bot.send_document(
                call.message.chat.id,
                f,
                caption="💾 Поточна резервна копія bot_data.json",
            )
        log_admin_action(call.from_user.id, "Вивантажено bot_data.json")
        bot.answer_callback_query(call.id, "📤 JSON надіслано!")
    except Exception as e:
        logging.error("Помилка експорту JSON: %s", e)
        bot.answer_callback_query(call.id, "❌ Не вдалося надіслати JSON", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "admin_activity")
def admin_activity(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    users = list(USERS_STORAGE.items())
    users.sort(key=lambda x: x[1].get("last_seen", ""), reverse=True)
    lines = ["📈 <b>Активність користувачів</b>", ""]
    if not users:
        lines.append("📭 Поки немає зареєстрованих користувачів.")
    else:
        for uid, data in users[:20]:
            name = data.get("first_name") or "Без імені"
            last_seen = data.get("last_seen", "невідомо")
            lines.append(f"• {name} — <code>{uid}</code>\n  🕐 {last_seen}")
        if len(users) > 20:
            lines.append(f"\n… ще {len(users) - 20} користувачів")
    k = types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔙 Інструменти", callback_data="admin_tools"))
    bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_reset_poll")
def admin_reset_poll(call):
    if not admin_only(call):
        return
    for candidate in poll_data.get("votes", {}):
        poll_data["votes"][candidate] = 0
    poll_data["voted_users"] = {}
    poll_data["is_active"] = False
    save_data_to_file()
    log_admin_action(call.from_user.id, "Скинуто результати опитування")
    bot.answer_callback_query(call.id, "🗳 Голоси скинуто!")
    bot.send_message(
        call.message.chat.id,
        "🗳 <b>Опитування скинуто</b>\n\n"
        "Усі голоси видалено, список тих, хто голосував, очищено, "
        "а опитування переведено у неактивний стан.",
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_test_bot")
def admin_test_bot(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id, "🧪 Бот працює!")
    bot.send_message(
        call.message.chat.id,
        "🧪 <b>Тест успішний</b>\n\n"
        "✅ Бот приймає callback-запити.\n"
        "✅ Адмін-доступ працює.\n"
        "✅ Сховище даних доступне.",
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_clear_states")
def admin_clear_states(call):
    if not admin_only(call):
        return
    count = len(user_states)
    user_states.clear()
    log_admin_action(call.from_user.id, f"Очищено тимчасові стани: {count}")
    bot.answer_callback_query(call.id, "🧹 Стани очищено!")
    bot.send_message(
        call.message.chat.id,
        f"🧹 <b>Тимчасові стани очищено</b>\n\n"
        f"Було очищено: <b>{count}</b> активних станів.",
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_system_check")
def admin_system_check(call):
    if not admin_only(call):
        return
    checks = []
    checks.append(("JSON", os.path.isfile(BACKUP_FILE_PATH)))
    checks.append(("SQLite", os.path.isfile(SQLITE_FILE_PATH)))
    checks.append(("Дані ДЗ", isinstance(HOMEWORK_STORAGE, dict)))
    checks.append(("Повідомлення", isinstance(TEACHER_MESSAGE_STORAGE, dict)))
    checks.append(("Розклад", bool(SUBJECTS_LIST)))
    checks.append(("Опитування", isinstance(poll_data, dict)))
    telegram_ok = False
    try:
        bot.get_me()
        telegram_ok = True
    except Exception:
        telegram_ok = False
    checks.append(("Telegram API", telegram_ok))
    lines = ["🧪 <b>ПЕРЕВІРКА СИСТЕМИ</b>", ""]
    for name, ok in checks:
        lines.append(f"{name:<14} {'✅' if ok else '❌'}")
    all_ok = all(ok for _, ok in checks)
    lines += ["", "🟢 Система працює нормально." if all_ok else "🔴 Є компоненти, які потребують перевірки."]
    k = types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("🔄 Перевірити ще раз", callback_data="admin_system_check"), types.InlineKeyboardButton("🔙 Інструменти", callback_data="admin_tools"))
    bot.answer_callback_query(call.id, "Перевірку завершено")
    safe_edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_force_save")
def admin_force_save(call):
    if not admin_only(call):
        return
    save_data_to_file()
    log_admin_action(call.from_user.id, "Примусово збережено дані")
    bot.answer_callback_query(call.id, "💾 Дані збережено!")
    bot.send_message(
        call.message.chat.id,
        "💾 <b>Дані успішно збережено</b>\n\n"
        "JSON та SQLite резервні копії оновлено.",
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# 📊 3. СТАТИСТИКА
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")

# ── Підрозділ: Статистика ──────────────────────────────────────

def admin_stats(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)

    total_hw = sum(len(v) for v in HOMEWORK_STORAGE.values() if isinstance(v, dict))
    today_hw = len(HOMEWORK_STORAGE.get(today.isoformat(), {}))
    tomorrow_hw = len(HOMEWORK_STORAGE.get(tomorrow.isoformat(), {}))
    week_hw = sum(
        len(HOMEWORK_STORAGE.get((monday + datetime.timedelta(days=i)).isoformat(), {}))
        for i in range(7)
    )

    active_today = 0
    active_week = 0
    for u in USERS_STORAGE.values():
        try:
            seen = datetime.datetime.fromisoformat(u.get("last_seen", ""))
            if seen.date() == today:
                active_today += 1
            if monday <= seen.date() <= sunday:
                active_week += 1
        except (ValueError, TypeError):
            pass

    votes = sum(poll_data.get("votes", {}).values())
    inactive = max(0, len(USERS_STORAGE) - active_week)
    viewed_hw = sum(int(u.get("stats", {}).get("homework_viewed", 0)) > 0 for u in USERS_STORAGE.values())
    viewed_messages = sum(int(u.get("stats", {}).get("messages_viewed", 0)) > 0 for u in USERS_STORAGE.values())
    viewed_schedule = sum(int(u.get("stats", {}).get("schedule_viewed", 0)) > 0 for u in USERS_STORAGE.values())
    viewed_weather = sum(int(u.get("stats", {}).get("weather_viewed", 0)) > 0 for u in USERS_STORAGE.values())
    text = (
        "📊 <b>СТАТИСТИКА БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>АКТИВНІСТЬ</b>\n"
        f"👥 Всього: <b>{len(USERS_STORAGE)}</b>\n"
        f"🟢 Сьогодні: <b>{active_today}</b>\n"
        f"🟡 За 7 днів: <b>{active_week}</b>\n"
        f"⚪ Неактивні: <b>{inactive}</b>\n\n"
        "📌 <b>ПЕРЕГЛЯДИ РОЗДІЛІВ</b>\n"
        f"📚 Відкривали ДЗ: <b>{viewed_hw}</b>\n"
        f"📢 Дивились повідомлення: <b>{viewed_messages}</b>\n"
        f"📅 Дивились розклад: <b>{viewed_schedule}</b>\n"
        f"🌤 Дивились погоду: <b>{viewed_weather}</b>\n\n"
        "📚 <b>ДЗ</b>\n"
        f"• сьогодні: <b>{today_hw}</b>\n"
        f"• завтра: <b>{tomorrow_hw}</b>\n"
        f"• цього тижня: <b>{week_hw}</b>\n"
        f"• всього: <b>{total_hw}</b>\n\n"
        "📢 <b>ПОВІДОМЛЕННЯ</b>\n"
        f"Всього: <b>{teacher_message_count()}</b>\n\n"
        "🗳 <b>ОПИТУВАННЯ</b>\n"
        f"• Активне: <b>{'🟢' if poll_data.get('is_active') else '⚪'}</b>\n"
        f"• Голосів: <b>{votes}</b>\n\n"
        "💾 <b>СИСТЕМА</b>\n"
        f"JSON: {'✅' if os.path.exists(BACKUP_FILE_PATH) else '❌'}\n"
        f"SQLite: {'✅' if os.path.exists(SQLITE_FILE_PATH) else '❌'}"
    )
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("🔄 Оновити", callback_data="admin_stats"),
        types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"),
    )
    bot.send_message(call.message.chat.id, text, reply_markup=k, parse_mode="HTML")


# ---------------------------------------------------------------------
# 👥 2. КОРИСТУВАЧІ
# ---------------------------------------------------------------------

def _user_is_active(data, days=7):
    try:
        seen = datetime.datetime.fromisoformat(data.get("last_seen", ""))
        return (datetime.datetime.now() - seen).days < days
    except Exception:
        return False


def _render_admin_users(call, filter_name="all"):
    users = list(USERS_STORAGE.items())
    if filter_name == "active":
        users = [(uid, u) for uid, u in users if _user_is_active(u)]
    elif filter_name == "inactive":
        users = [(uid, u) for uid, u in users if not _user_is_active(u)]
    users.sort(key=lambda x: x[1].get("last_seen", ""), reverse=True)
    label = {"all": "Усі", "active": "Активні", "inactive": "Неактивні"}.get(filter_name, "Усі")
    lines = [f"👥 <b>КОРИСТУВАЧІ</b> — {label}", "", f"Всього: <b>{len(USERS_STORAGE)}</b>", f"🟢 Активні: <b>{sum(_user_is_active(u) for u in USERS_STORAGE.values())}</b>", f"⚪ Неактивні: <b>{sum(not _user_is_active(u) for u in USERS_STORAGE.values())}</b>", ""]
    if not users:
        lines.append("📭 Користувачів не знайдено.")
    else:
        lines.append("👇 Обери користувача:")
    k = types.InlineKeyboardMarkup(row_width=1)
    for uid, data in users[:30]:
        name = (f"{data.get('first_name','')} {data.get('last_name','')}").strip() or "Без імені"
        k.add(types.InlineKeyboardButton(f"{'🟢' if _user_is_active(data) else '⚪'} {name} · {uid}", callback_data=f"admin_user:view:{uid}"))
    k.add(types.InlineKeyboardButton("🔎 Пошук", callback_data="admin_user_search"))
    k.add(types.InlineKeyboardButton("🟢 Активні", callback_data="admin_users_filter:active"), types.InlineKeyboardButton("⚪ Неактивні", callback_data="admin_users_filter:inactive"))
    k.add(types.InlineKeyboardButton("📋 Усі", callback_data="admin_users_filter:all"))
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    safe_edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_users")

# ── Підрозділ: Користувачі ─────────────────────────────────────

def admin_users(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    _render_admin_users(call, "all")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_users_filter:"))
def admin_users_filter(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    _render_admin_users(call, call.data.split(":", 1)[1])


@bot.callback_query_handler(func=lambda call: call.data == "admin_user_search")
def admin_user_search(call):
    if not admin_only(call): return
    user_states[call.from_user.id] = {"action": "waiting_admin_user_search"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔎 <b>Пошук користувача</b>\n\nВведи ім'я, username або Telegram ID:", reply_markup=admin_cancel_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_user:view:"))
def admin_user_view(call):
    if not admin_only(call): return
    uid = call.data.split(":", 2)[2]
    data = USERS_STORAGE.get(uid)
    if not data:
        bot.answer_callback_query(call.id, "Користувача не знайдено", show_alert=True); return
    bot.answer_callback_query(call.id)
    stats = data.get("stats", {}) if isinstance(data.get("stats", {}), dict) else {}
    name = (f"{data.get('first_name','')} {data.get('last_name','')}").strip() or "Без імені"
    username = f"@{data.get('username')}" if data.get("username") else "—"
    text = (
        "👤 <b>КОРИСТУВАЧ</b>\n\n"
        f"Ім'я: <b>{html.escape(name)}</b>\n"
        f"Username: <b>{html.escape(username)}</b>\n"
        f"ID: <code>{html.escape(uid)}</code>\n\n"
        f"{'🟢' if _user_is_active(data) else '⚪'} Остання активність: <b>{html.escape(data.get('last_seen','невідомо'))}</b>\n\n"
        f"📚 ДЗ: <b>{int(stats.get('homework_viewed',0))}</b> відкриттів\n"
        f"📢 Повідомлення: <b>{int(stats.get('messages_viewed',0))}</b>\n"
        f"📅 Розклад: <b>{int(stats.get('schedule_viewed',0))}</b>\n"
        f"🌤 Погода: <b>{int(stats.get('weather_viewed',0))}</b>"
    )
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🟢 Активні", callback_data="admin_users_filter:active"), types.InlineKeyboardButton("⚪ Неактивні", callback_data="admin_users_filter:inactive"))
    k.add(types.InlineKeyboardButton("🔙 Користувачі", callback_data="admin_users"))
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


# ---------------------------------------------------------------------
# 🛡 4. ЖУРНАЛ АДМІНСЬКИХ ДІЙ
# ---------------------------------------------------------------------

def _filtered_admin_logs(filter_name="all"):
    items = ADMIN_ACTION_LOG[-200:]
    today = datetime.date.today()
    if filter_name == "today":
        return [x for x in items if str(x.get("time", "")).startswith(today.isoformat())]
    if filter_name == "yesterday":
        d = today - datetime.timedelta(days=1)
        return [x for x in items if str(x.get("time", "")).startswith(d.isoformat())]
    if filter_name == "admin":
        return [x for x in items if x.get("admin_id") in ADMIN_IDS]
    action_words = {
        "hw": ("ДЗ", "домаш"),
        "msg": ("повідом",),
        "poll": ("опитув",),
        "broadcast": ("розсил",),
        "backup": ("резерв", "backup", "відновлено"),
    }
    if filter_name in action_words:
        words = action_words[filter_name]
        return [x for x in items if any(w.lower() in str(x.get("action", "")).lower() for w in words)]
    return items


def _render_admin_logs(call, filter_name="all"):
    items = _filtered_admin_logs(filter_name)
    labels = {"all":"Усі", "today":"Сьогодні", "yesterday":"Вчора", "admin":"За адміном", "hw":"ДЗ", "msg":"Повідомлення", "poll":"Опитування", "broadcast":"Розсилка", "backup":"Backup"}
    lines = [f"🛡 <b>ЖУРНАЛ ДІЙ</b> — {labels.get(filter_name, 'Усі')}", ""]
    if not items:
        lines.append("📭 Записів за цим фільтром немає.")
    else:
        for item in items[-30:][::-1]:
            try:
                t = datetime.datetime.fromisoformat(item.get("time", "")).strftime("%d.%m.%Y %H:%M")
            except (ValueError, TypeError):
                t = item.get("time", "")
            name = item.get("admin_name") or f"ID {item.get('admin_id', '')}"
            lines.append(
                f"🕒 <b>{html.escape(t)}</b> — <b>{html.escape(name)}</b>\n"
                f"   ➜ {html.escape(item.get('action', ''))}\n"
            )
    k = types.InlineKeyboardMarkup(row_width=3)
    k.add(
        types.InlineKeyboardButton("📅 Сьогодні", callback_data="admin_logs_filter:today"),
        types.InlineKeyboardButton("📅 Вчора", callback_data="admin_logs_filter:yesterday"),
        types.InlineKeyboardButton("👤 За адміном", callback_data="admin_logs_filter:admin"),
    )
    k.add(
        types.InlineKeyboardButton("📚 ДЗ", callback_data="admin_logs_filter:hw"),
        types.InlineKeyboardButton("📢 Повідомлення", callback_data="admin_logs_filter:msg"),
        types.InlineKeyboardButton("🗳 Опитування", callback_data="admin_logs_filter:poll"),
    )
    k.add(
        types.InlineKeyboardButton("📣 Розсилка", callback_data="admin_logs_filter:broadcast"),
        types.InlineKeyboardButton("💾 Backup", callback_data="admin_logs_filter:backup"),
        types.InlineKeyboardButton("📋 Усі", callback_data="admin_logs_filter:all"),
    )
    k.add(
        types.InlineKeyboardButton("🧹 Очистити журнал", callback_data="admin_clear_logs"),
        types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"),
    )
    safe_edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_logs")

# ── Підрозділ: Журнал дій ──────────────────────────────────────

def admin_logs(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    _render_admin_logs(call, "all")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_logs_filter:"))
def admin_logs_filter(call):
    if not admin_only(call):
        return
    filter_name = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    _render_admin_logs(call, filter_name)


@bot.callback_query_handler(func=lambda call: call.data == "admin_clear_logs")
def admin_clear_logs(call):
    if not admin_only(call):
        return
    ADMIN_ACTION_LOG.clear()
    save_data_to_file()
    bot.answer_callback_query(call.id, "🧹 Журнал очищено!")
    _render_admin_logs(call, "all")


# ---------------------------------------------------------------------
# 💾 10. РЕЗЕРВНІ КОПІЇ
# ---------------------------------------------------------------------

# ── Підрозділ: Резервні копії ──────────────────────────────────

def _create_backup_snapshot():
    save_data_to_file()
    folder=os.path.join(DIR_BACKUP,"backups"); os.makedirs(folder,exist_ok=True)
    stamp=datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path=os.path.join(folder,f"backup_{stamp}.json")
    shutil.copy2(BACKUP_FILE_PATH,path)
    return path

def _backup_snapshot_files():
    folder=os.path.join(DIR_BACKUP,"backups")
    if not os.path.isdir(folder): return []
    return sorted([os.path.join(folder,x) for x in os.listdir(folder) if x.endswith(".json")],reverse=True)

@bot.callback_query_handler(func=lambda call: call.data == "admin_backup")
def admin_backup(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("💾 Створити",callback_data="admin_backup_create"),types.InlineKeyboardButton("📂 Переглянути",callback_data="admin_backup_list"))
    k.add(types.InlineKeyboardButton("♻️ Відновити",callback_data="admin_backup_list_restore"),types.InlineKeyboardButton("📦 Експорт JSON",callback_data="admin_export_json"))
    k.add(types.InlineKeyboardButton("🗑 Очистити старі",callback_data="admin_backup_cleanup"))
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель",callback_data="admin_panel"))
    bot.send_message(call.message.chat.id,"💾 <b>РЕЗЕРВНЕ КОПІЮВАННЯ</b>\n\n💾 Створити окрему копію\n📂 Переглянути копії\n♻️ Відновити дані\n📦 Експортувати bot_data.json\n\n⚠️ Перед відновленням поточні дані автоматично копіюються.",reply_markup=k,parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_backup_create")
def admin_backup_create(call):
    if not admin_only(call): return
    try:
        path=_create_backup_snapshot(); log_admin_action(call.from_user.id,"Створено резервну копію")
        bot.answer_callback_query(call.id,"💾 Копію створено!")
        bot.send_message(call.message.chat.id,f"✅ <b>Резервну копію створено</b>\n\n📄 <code>{html.escape(os.path.basename(path))}</code>",reply_markup=admin_panel_keyboard(),parse_mode="HTML")
    except Exception as e:
        logging.exception("backup create: %s",e); bot.answer_callback_query(call.id,"❌ Помилка",show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "admin_backup_cleanup")
def admin_backup_cleanup(call):
    if not admin_only(call):
        return
    files = _backup_snapshot_files()
    keep = 10
    removed = 0
    for old_path in files[keep:]:
        try:
            os.remove(old_path)
            removed += 1
        except OSError:
            pass
    log_admin_action(call.from_user.id, f"Очищено старі резервні копії: {removed}")
    bot.answer_callback_query(call.id, f"🗑 Видалено: {removed}")
    bot.send_message(call.message.chat.id, f"🗑 <b>Старі резервні копії очищено</b>\n\nЗалишено останніх: <b>{min(len(files), keep)}</b>\nВидалено: <b>{removed}</b>", reply_markup=admin_panel_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_backup_list")
def admin_backup_list(call):
    if not admin_only(call): return
    files=_backup_snapshot_files(); lines=["📂 <b>РЕЗЕРВНІ КОПІЇ</b>",""]
    if not files: lines.append("📭 Копій ще немає.")
    else:
        for f in files[:15]:
            stamp = os.path.basename(f).removeprefix("backup_").removesuffix(".json").replace("_", " ").replace("-", ".", 2)
            lines.append(f"📦 <b>{html.escape(stamp)}</b> — {os.path.getsize(f)/1024:.1f} КБ")
    k=types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("💾 Створити",callback_data="admin_backup_create")); k.add(types.InlineKeyboardButton("🔙 Назад",callback_data="admin_backup"))
    safe_edit_message_text("\n".join(lines),call.message.chat.id,call.message.message_id,reply_markup=k,parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_backup_list_restore")
def admin_backup_list_restore(call):
    if not admin_only(call): return
    files=_backup_snapshot_files(); k=types.InlineKeyboardMarkup(row_width=1)
    if not files: text="♻️ <b>ВІДНОВЛЕННЯ</b>\n\n📭 Резервних копій немає."
    else:
        text="♻️ <b>ОБЕРИ КОПІЮ</b>\n\n⚠️ Перед відновленням буде створена аварійна копія поточних даних."
        for f in files[:10]:
            stamp = os.path.basename(f).removeprefix("backup_").removesuffix(".json")
            try:
                dt = datetime.datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S")
                label = f"📦 {dt.strftime('%d.%m.%Y %H:%M')} — {os.path.getsize(f)/1024:.1f} КБ"
            except Exception:
                label = os.path.basename(f)
            k.add(types.InlineKeyboardButton(label,callback_data=f"admin_restore_confirm:{os.path.basename(f)}"))
    k.add(types.InlineKeyboardButton("🔙 Назад",callback_data="admin_backup"))
    safe_edit_message_text(text,call.message.chat.id,call.message.message_id,reply_markup=k,parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_restore_confirm:"))
def admin_restore_confirm(call):
    if not admin_only(call): return
    filename = os.path.basename(call.data.split(":", 1)[1])
    path = os.path.join(DIR_BACKUP, "backups", filename)
    if not os.path.isfile(path):
        bot.answer_callback_query(call.id, "❌ Копію не знайдено", show_alert=True)
        return
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("♻️ Продовжити", callback_data=f"admin_restore:{filename}"), types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_backup_list_restore"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "⚠️ <b>УВАГА</b>\n\n"
        f"Обрана копія: <code>{html.escape(filename)}</code>\n\n"
        "Перед відновленням буде автоматично створена поточна резервна копія.\n"
        "Після відновлення потрібно буде перезапустити бота.",
        call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_restore:"))
def admin_restore_backup(call):
    if not admin_only(call): return
    filename=os.path.basename(call.data.split(":",1)[1]); path=os.path.join(DIR_BACKUP,"backups",filename)
    if not os.path.isfile(path): bot.answer_callback_query(call.id,"❌ Копію не знайдено",show_alert=True); return
    try:
        _create_backup_snapshot()
        with open(path,"r",encoding="utf-8") as f: data=json.load(f)
        if not isinstance(data,dict): raise ValueError("Невірний JSON")
        temp=BACKUP_FILE_PATH+".restore.tmp"
        with open(temp,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=4); f.flush(); os.fsync(f.fileno())
        os.replace(temp,BACKUP_FILE_PATH)
        bot.answer_callback_query(call.id,"♻️ Відновлено!")
        bot.send_message(call.message.chat.id,"♻️ <b>Резервну копію відновлено.</b>\n\n🔄 Перезапусти бота, щоб повністю застосувати всі відновлені дані.",reply_markup=admin_panel_keyboard(),parse_mode="HTML")
        log_admin_action(call.from_user.id,f"Відновлено резервну копію {filename}")
    except Exception as e:
        logging.exception("backup restore: %s",e); bot.answer_callback_query(call.id,"❌ Помилка відновлення",show_alert=True)


# ---------------------------------------------------------------------
# 🔧 11. СТАН СИСТЕМИ
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_system")

# ── Підрозділ: Стан системи та розклад ─────────────────────────

def admin_system(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    uptime = datetime.datetime.now() - BOT_STARTED_AT
    admins = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "не задані"
    text = (
        "🔧 <b>Система</b>\n\n"
        f"🤖 Бот: працює\n"
        f"⏱ Аптайм: {uptime.days} дн. {uptime.seconds // 3600} год. {(uptime.seconds % 3600) // 60} хв.\n"
        f"👑 Адміністратори: <code>{admins}</code>\n"
        f"📁 Резервна папка: <code>{DIR_BACKUP}</code>\n"
        f"📄 JSON існує: {'✅' if os.path.exists(BACKUP_FILE_PATH) else '❌'}\n"
        f"🗄 SQLite існує: {'✅' if os.path.exists(SQLITE_FILE_PATH) else '❌'}"
    )
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(types.InlineKeyboardButton("💾 Оновити резервну копію", callback_data="admin_backup"))
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    bot.send_message(call.message.chat.id, text, reply_markup=k, parse_mode="HTML")


# ---------------------------------------------------------------------
# 📅 5. РОЗКЛАД ТА ДЗВІНКИ
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_schedule")
def admin_schedule(call):
    if not admin_only(call):
        return
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("✏️ Уроки сьогодні", callback_data="admin_change_schedule:today"),
        types.InlineKeyboardButton("✏️ Уроки завтра", callback_data="admin_change_schedule:tomorrow"),
        types.InlineKeyboardButton("🔔 Дзвінки сьогодні", callback_data="admin_change_bells:today"),
        types.InlineKeyboardButton("🔔 Дзвінки завтра", callback_data="admin_change_bells:tomorrow"),
        types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"),
    )
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "📅 <b>Керування розкладом</b>\n\n"
        "Тут можна окремо змінювати уроки та розклад дзвінків на сьогодні або завтра.\n\n"
        "Напиши <b>НІ</b>, якщо змін немає.",
        call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_change_schedule:"))
def admin_change_schedule(call):
    if not admin_only(call):
        return
    target = call.data.split(":", 1)[1]
    user_states[call.from_user.id] = {"action": "waiting_schedule_change", "target": target}
    bot.answer_callback_query(call.id)
    k = admin_cancel_keyboard([
        types.InlineKeyboardButton(
            "🚫 Змін немає",
            callback_data=f"admin_no_changes:schedule:{target}"
        )
    ])
    bot.send_message(
        call.message.chat.id,
        f"✏️ Надішли новий текст змін уроків для <b>{'сьогодні' if target == 'today' else 'завтра'}</b>.\n\n"
        "Або натисни кнопку нижче, якщо змін немає.",
        reply_markup=k,
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_change_bells:"))
def admin_change_bells(call):
    if not admin_only(call):
        return
    target = call.data.split(":", 1)[1]
    user_states[call.from_user.id] = {"action": "waiting_bells_change", "target": target}
    bot.answer_callback_query(call.id)
    k = admin_cancel_keyboard([
        types.InlineKeyboardButton(
            "🚫 Змін немає",
            callback_data=f"admin_no_changes:bells:{target}"
        )
    ])
    bot.send_message(
        call.message.chat.id,
        f"🔔 Надішли новий розклад дзвінків для <b>{'сьогодні' if target == 'today' else 'завтра'}</b>.\n\n"
        "Або натисни кнопку нижче, якщо змін немає.",
        reply_markup=k,
        parse_mode="HTML"
    )


# ---------------------------------------------------------------------
# 🗳 8. КЕРУВАННЯ ОПИТУВАННЯМ
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_custom_poll_start")

# ── Підрозділ: Адмін-керування опитуваннями ───────────────────

def admin_custom_poll_start(call):
    if not admin_only(call): return
    user_states[call.from_user.id] = {"action": "waiting_custom_poll_question"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🗳 <b>Створення опитування</b>\n\nНадішли питання опитування.", reply_markup=admin_cancel_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data in ("custom_poll_open", "custom_poll_results"))
def custom_poll_open_or_results(call):
    register_user(call.from_user)
    if call.data == "custom_poll_open":
        if not CUSTOM_POLL.get("is_active"):
            bot.answer_callback_query(call.id, "📭 Активного опитування немає.", show_alert=True); return
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, custom_poll_text(), reply_markup=custom_poll_keyboard(), parse_mode="HTML")
    else:
        bot.answer_callback_query(call.id)
        total=sum(CUSTOM_POLL.get("votes", {}).values())
        lines=["📊 <b>РЕЗУЛЬТАТИ ОПИТУВАННЯ</b>", "", html.escape(CUSTOM_POLL.get("question", "")), ""]
        for opt in CUSTOM_POLL.get("options", []): lines.append(f"• {html.escape(opt)} — <b>{CUSTOM_POLL.get('votes',{}).get(opt,0)}</b>")
        lines.append(f"\n🗳 Всього голосів: <b>{total}</b>")
        bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=admin_panel_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("custom_vote:"))
def custom_vote(call):
    register_user(call.from_user)
    if not CUSTOM_POLL.get("is_active"):
        bot.answer_callback_query(call.id, "❌ Опитування завершено.", show_alert=True); return
    try: idx=int(call.data.split(":",1)[1]); option=CUSTOM_POLL["options"][idx]
    except (ValueError,IndexError,KeyError):
        bot.answer_callback_query(call.id, "⚠️ Варіант недоступний.", show_alert=True); return
    uid=str(call.from_user.id)
    if uid in CUSTOM_POLL.get("voted_users", {}):
        bot.answer_callback_query(call.id, "❌ Ти вже проголосував(-ла).", show_alert=True); return
    CUSTOM_POLL.setdefault("votes", {})[option]=int(CUSTOM_POLL.get("votes",{}).get(option,0))+1
    CUSTOM_POLL.setdefault("voted_users", {})[uid]={"option":option,"name":((call.from_user.first_name or "")+" "+(call.from_user.last_name or "")).strip()}
    save_data_to_file()
    bot.answer_callback_query(call.id, f"✅ Голос зараховано: {option}")
    safe_edit_message_text(custom_poll_text(), call.message.chat.id, call.message.message_id, reply_markup=custom_poll_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_custom_poll_finish")
def admin_custom_poll_finish(call):
    if not admin_only(call): return
    if not CUSTOM_POLL.get("is_active"):
        bot.answer_callback_query(call.id, "Опитування вже завершено.", show_alert=True); return
    CUSTOM_POLL["is_active"] = False
    save_data_to_file(); log_admin_action(call.from_user.id, "Завершено власне опитування")
    total=sum(CUSTOM_POLL.get("votes", {}).values())
    lines=["🗳 <b>ОПИТУВАННЯ ЗАВЕРШЕНО</b>", "", html.escape(CUSTOM_POLL.get("question", "")), ""]
    for opt in CUSTOM_POLL.get("options", []): lines.append(f"• {html.escape(opt)} — <b>{CUSTOM_POLL.get('votes',{}).get(opt,0)}</b>")
    lines.append(f"\n🗳 Всього голосів: <b>{total}</b>")
    bot.answer_callback_query(call.id, "Опитування завершено")
    bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=admin_panel_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_menu")
def admin_poll_menu(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    active = poll_is_active()
    locked = active and not poll_voting_open()
    if locked:
        status = "🔒 час голосування вийшов — очікує завершення"
    else:
        status = "🟢 активне" if active else ("🔴 завершене" if poll_data.get("completed_at") else "⚪ неактивне")

    target = poll_target_date()
    target_line = f"📅 Дата чергування: <b>{target.strftime('%d.%m.%Y')}</b> ({WEEKDAYS_UKR[target.weekday()]})"
    if is_vacation(target):
        target_line += "\n🍂 <b>Канікули</b>"

    deadline = ""
    if poll_data.get("ends_at"):
        try:
            deadline = f"\n⏳ Дедлайн голосування: <b>{datetime.datetime.fromisoformat(poll_data['ends_at']).strftime('%d.%m.%Y %H:%M')}</b>"
        except Exception:
            pass

    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("▶️ Запустити", callback_data="admin_poll_start"),
        types.InlineKeyboardButton("⏹ Завершити", callback_data="admin_poll_finish"),
    )
    k.add(
        types.InlineKeyboardButton("📊 Результати", callback_data="admin_poll_results"),
        types.InlineKeyboardButton("🔄 Повторити", callback_data="admin_poll_repeat"),
    )
    k.add(
        types.InlineKeyboardButton("📅 Дата опитування", callback_data="admin_poll_date_start"),
        types.InlineKeyboardButton("📝 Власне опитування", callback_data="admin_custom_poll_start"),
    )
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    bot.send_message(
        call.message.chat.id,
        f"🗳 <b>Керування опитуванням</b>\n\n{target_line}\n{status}{deadline}\n\n🗳 Голосів: <b>{poll_total_votes()}</b>",
        reply_markup=k,
        parse_mode="HTML",
    )


def poll_date_picker_keyboard(year=None, month=None):
    today = datetime.date.today()
    if year is None or month is None:
        target = poll_target_date()
        year, month = target.year, target.month

    first = datetime.date(year, month, 1)
    next_month = datetime.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    days_in_month = (next_month - first).days

    months_ukr = [
        "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
        "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"
    ]
    keyboard = types.InlineKeyboardMarkup(row_width=7)
    keyboard.add(types.InlineKeyboardButton(f"📅 {months_ukr[month - 1]} {year}", callback_data="noop"))
    keyboard.add(*[
        types.InlineKeyboardButton(x, callback_data="noop")
        for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    ])

    row = [types.InlineKeyboardButton(" ", callback_data="noop") for _ in range(first.weekday())]
    for day in range(1, days_in_month + 1):
        d = datetime.date(year, month, day)
        markers = []
        if d == poll_target_date():
            markers.append("✅")
        if d == today:
            markers.append("🔵")
        if is_vacation(d):
            markers.append("🍂")
        label = ("".join(markers) + str(day)) if markers else str(day)
        row.append(types.InlineKeyboardButton(label, callback_data=f"poll_date:{d.isoformat()}"))
        if len(row) == 7:
            keyboard.row(*row)
            row = []
    if row:
        while len(row) < 7:
            row.append(types.InlineKeyboardButton(" ", callback_data="noop"))
        keyboard.row(*row)

    prev_month = first - datetime.timedelta(days=1)
    next_first = next_month
    keyboard.row(
        types.InlineKeyboardButton("⬅️", callback_data=f"poll_month:{prev_month.year}-{prev_month.month:02d}"),
        types.InlineKeyboardButton("Сьогодні", callback_data=f"poll_date:{today.isoformat()}"),
        types.InlineKeyboardButton("➡️", callback_data=f"poll_month:{next_first.year}-{next_first.month:02d}"),
    )
    keyboard.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_poll_menu"))
    return keyboard


@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_date_start")
def admin_poll_date_start(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    target = poll_target_date()
    safe_edit_message_text(
        "📅 <b>ОБЕРИ ДАТУ ОПИТУВАННЯ</b>\n\n"
        "🍂 Канікули позначені значком 🍂.\n"
        "Після натискання бот сам визначить дату та день тижня.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=poll_date_picker_keyboard(target.year, target.month),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("poll_month:"))
def poll_month_page(call):
    if not admin_only(call):
        return
    try:
        year, month = map(int, call.data.split(":", 1)[1].split("-"))
        datetime.date(year, month, 1)
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id, "⚠️ Помилка календаря", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=poll_date_picker_keyboard(year, month),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("poll_date:"))
def poll_date_selected(call):
    if not admin_only(call):
        return
    try:
        selected = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return

    poll_data["target_date"] = selected.isoformat()
    # Якщо опитування ще не запущене — дата просто запам'ятовується.
    # Якщо воно вже активне, дата зміниться для наступного запуску.
    save_data_to_file()

    message = f"📅 {selected.strftime('%d.%m.%Y')} — {WEEKDAYS_UKR[selected.weekday()]}"
    if is_vacation(selected):
        message += "\n🍂 Канікули"
    bot.answer_callback_query(call.id, message, show_alert=True)
    admin_poll_menu(call)


@bot.callback_query_handler(func=lambda call: call.data == "poll_locked")
def poll_locked(call):
    if poll_is_active() and not poll_voting_open():
        bot.answer_callback_query(call.id, "🔒 Час голосування вийшов. Дочекайся завершення адміністратором.", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "ℹ️ Голосування закрите.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_repeat")
def admin_poll_repeat(call):
    if not admin_only(call): return
    if poll_is_active():
        bot.answer_callback_query(call.id, "🗳 Відкриваю поточне опитування.")
        bot.send_message(
            call.message.chat.id,
            get_poll_text(),
            reply_markup=get_poll_keyboard(),
            parse_mode="HTML",
        )
        return
    bot.answer_callback_query(call.id)
    show_poll_start_time_menu(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_deadline_menu")
def admin_poll_deadline_menu(call):
    if not admin_only(call): return
    k=types.InlineKeyboardMarkup(row_width=3)
    for hours in (6, 12, 24, 48, 72):
        mark = "✅ " if int(admin_setting("poll_deadline_hours",24)) == hours else ""
        k.add(types.InlineKeyboardButton(f"{mark}{hours} год", callback_data=f"admin_poll_deadline:{hours}"))
    k.add(types.InlineKeyboardButton("🔙 Опитування", callback_data="admin_poll_menu"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text("⏰ <b>ДЕДЛАЙН ОПИТУВАННЯ</b>\n\nОбери, через скільки годин автоматично завершувати нове опитування:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_poll_deadline:"))
def admin_poll_deadline_set(call):
    if not admin_only(call): return
    hours=int(call.data.split(":",1)[1])
    if hours not in (6,12,24,48,72): return
    set_admin_setting("poll_deadline_hours", hours)
    log_admin_action(call.from_user.id, f"Дедлайн опитування: {hours} год")
    bot.answer_callback_query(call.id, f"⏰ {hours} год")
    admin_poll_menu(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_start")
def admin_poll_start(call):
    if not admin_only(call):
        return
    if poll_is_active():
        # Навіть активне опитування можна відкрити повторно.
        bot.answer_callback_query(call.id, "🗳 Відкриваю поточне опитування.")
        bot.send_message(
            call.message.chat.id,
            get_poll_text(),
            reply_markup=get_poll_keyboard(),
            parse_mode="HTML",
        )
        return
    bot.answer_callback_query(call.id)
    show_poll_start_time_menu(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_finish")
def admin_poll_finish(call):
    if not admin_only(call):
        return
    callback_finish_poll(call)
    log_admin_action(call.from_user.id, "Завершено опитування")


@bot.callback_query_handler(func=lambda call: call.data == "admin_poll_results")
def admin_poll_results(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    lines = ["📊 <b>Поточні результати</b>", ""]
    if not poll_data.get("started_at"):
        lines.append("⚪ <b>Опитування ще не запускалося.</b>")
        lines.append("🗳 Коли його запустиш, тут з’явиться статистика голосів.")
    else:
        total = sum(poll_data.get("votes", {}).values())
        for name, count in poll_data.get("votes", {}).items():
            lines.append(f"👤 {name}: <b>{count}</b> — <b>{poll_percentage(count, total)}%</b>")
        lines.append(f"\n🗳 Всього голосів: <b>{total}</b>")
    bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=admin_panel_keyboard(), parse_mode="HTML")


# ---------------------------------------------------------------------
# 📚 6. КЕРУВАННЯ ДЗ
# ---------------------------------------------------------------------

# ── Підрозділ: Адмін-керування ДЗ ─────────────────────────────

def admin_homework_keyboard():
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("➕ Додати ДЗ", callback_data="add_hw"),
        types.InlineKeyboardButton("✏️ Редагувати ДЗ", callback_data="admin_edit_hw_start"),
        types.InlineKeyboardButton("🗑 Видалити ДЗ", callback_data="admin_delete_hw_start"),
        types.InlineKeyboardButton("📋 Копіювати ДЗ", callback_data="admin_copy_hw_start"),
        types.InlineKeyboardButton("🧹 Очистити ДЗ", callback_data="clear_hw_menu"),
        types.InlineKeyboardButton("🧹 Очистити минулі", callback_data="admin_hw_cleanup_past"),
        # Повернення саме до меню ДЗ, а не одразу в адмін-панель.
        types.InlineKeyboardButton("🔙 Меню ДЗ", callback_data="back_to_hw_menu"),
    )
    return k


@bot.callback_query_handler(func=lambda call: call.data == "admin_copy_hw_start")
def admin_copy_hw_start(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "📋 <b>Копіювання ДЗ</b>\n\nОбери дату, ЗВІДКИ копіювати:",
        call.message.chat.id, call.message.message_id,
        reply_markup=homework_date_picker_keyboard("admin_copy_hw_source", 0, 14),
        parse_mode="HTML",
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_copy_hw_source:"))
def admin_copy_hw_source(call):
    if not admin_only(call):
        return
    source = call.data.split(":", 1)[1]
    day_data = HOMEWORK_STORAGE.get(source, {})
    if not day_data:
        bot.answer_callback_query(call.id, "📭 На цю дату ДЗ немає.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"📋 <b>Копіювання ДЗ</b>\n\nДжерело: <b>{datetime.date.fromisoformat(source).strftime('%d.%m.%Y')}</b>\n\nОбери дату, КУДИ копіювати:",
        call.message.chat.id, call.message.message_id,
        reply_markup=homework_date_picker_keyboard(f"admin_copy_hw_target:{source}", 0, 14),
        parse_mode="HTML",
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_copy_hw_target:"))
def admin_copy_hw_target(call):
    if not admin_only(call):
        return
    payload = call.data.split(":", 2)
    if len(payload) != 3:
        bot.answer_callback_query(call.id, "⚠️ Невірні дані", show_alert=True); return
    source, target = payload[1], payload[2]
    if source == target:
        bot.answer_callback_query(call.id, "⚠️ Джерело і дата призначення однакові.", show_alert=True); return
    source_data = HOMEWORK_STORAGE.get(source, {})
    if not source_data:
        bot.answer_callback_query(call.id, "📭 ДЗ-джерело порожнє.", show_alert=True); return
    copied = 0
    target_data = HOMEWORK_STORAGE.setdefault(target, {})
    for subject, info in source_data.items():
        target_data[subject] = dict(info) if isinstance(info, dict) else info
        copied += 1
    save_data_to_file()
    refresh_main_menu(call.message.chat.id)
    log_admin_action(call.from_user.id, f"Скопійовано ДЗ {source} → {target}: {copied} предметів")
    bot.answer_callback_query(call.id, "📋 ДЗ скопійовано!")
    safe_edit_message_text(
        f"✅ <b>ДЗ скопійовано</b>\n\n📅 З: <b>{datetime.date.fromisoformat(source).strftime('%d.%m.%Y')}</b>\n📅 На: <b>{datetime.date.fromisoformat(target).strftime('%d.%m.%Y')}</b>\n📚 Скопійовано: <b>{copied}</b> предметів",
        call.message.chat.id, call.message.message_id,
        reply_markup=admin_homework_keyboard(), parse_mode="HTML"
    )

def homework_clear_menu_keyboard():
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("📅 На сьогодні", callback_data="clear_hw_date:today"),
        types.InlineKeyboardButton("📅 На завтра", callback_data="clear_hw_date:tomorrow"),
        types.InlineKeyboardButton("🗓 Обрати дату", callback_data="clear_hw_choose_date"),
        types.InlineKeyboardButton("🧹 Очистити всі ДЗ", callback_data="clear_hw_all_confirm"),
        types.InlineKeyboardButton("🔙 Керування ДЗ", callback_data="admin_homework"),
    )
    return k


@bot.callback_query_handler(func=lambda call: call.data == "admin_homework")
def admin_homework(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    count = sum(len(v) for v in HOMEWORK_STORAGE.values() if isinstance(v, dict))
    bot.send_message(
        call.message.chat.id,
        "📚 <b>Керування домашніми завданнями</b>\n\n"
        f"Збережено завдань: <b>{count}</b>\n\n"
        "Тут адміністратор може додавати, видаляти або повністю очищати ДЗ.",
        reply_markup=admin_homework_keyboard(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_hw_cleanup_past")
def admin_hw_cleanup_past(call):
    if not admin_only(call): return
    today = datetime.date.today().isoformat()
    old_dates = [d for d in HOMEWORK_STORAGE if d < today]
    count = sum(len(HOMEWORK_STORAGE.get(d, {})) for d in old_dates)
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🧹 Так, очистити", callback_data="admin_hw_cleanup_past_confirm"), types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_homework"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text(f"⚠️ <b>ОЧИЩЕННЯ МИНУЛОГО ДЗ</b>\n\nБуде видалено <b>{count}</b> завдань за минулі дати.\n\nПродовжити?", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_hw_cleanup_past_confirm")
def admin_hw_cleanup_past_confirm(call):
    if not admin_only(call): return
    today = datetime.date.today().isoformat()
    old_dates = [d for d in list(HOMEWORK_STORAGE) if d < today]
    count = sum(len(HOMEWORK_STORAGE.get(d, {})) for d in old_dates)
    for d in old_dates: HOMEWORK_STORAGE.pop(d, None)
    save_data_to_file(); log_admin_action(call.from_user.id, f"Очищено минулі ДЗ: {count}")
    bot.answer_callback_query(call.id, "🧹 Готово")
    safe_edit_message_text(f"✅ <b>Минулі ДЗ очищено</b>\n\nВидалено: <b>{count}</b> завдань.", call.message.chat.id, call.message.message_id, reply_markup=admin_homework_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_edit_hw_start")
def admin_edit_hw_start(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    safe_edit_message_text("✏️ <b>Редагування ДЗ</b>\n\nОбери дату:", call.message.chat.id, call.message.message_id, reply_markup=homework_date_picker_keyboard("admin_edit_hw_date", 0, 7), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_edit_hw_date:"))
def admin_edit_hw_date(call):
    if not admin_only(call): return
    try: date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True); return
    day_data = HOMEWORK_STORAGE.get(date_obj.isoformat(), {})
    if not day_data:
        bot.answer_callback_query(call.id, "📭 На цю дату ДЗ немає", show_alert=True); return
    k = types.InlineKeyboardMarkup(row_width=2); row=[]
    for subject in day_data:
        if subject in SUBJECTS_LIST:
            row.append(types.InlineKeyboardButton(f"✏️ {subject}", callback_data=f"admin_edit_hw:{date_obj.isoformat()}:{SUBJECTS_LIST.index(subject)}"))
            if len(row)==2: k.add(*row); row=[]
    if row: k.add(*row)
    k.add(types.InlineKeyboardButton("🔙 Керування ДЗ", callback_data="admin_homework"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text(f"✏️ <b>ДЗ на {date_obj.strftime('%d.%m.%Y')}</b>\n\nОбери предмет для редагування:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_edit_hw:"))
def admin_edit_hw(call):
    if not admin_only(call): return
    _, date_key, idx = call.data.split(":", 2)
    try: subject = SUBJECTS_LIST[int(idx)]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет", show_alert=True); return
    info = HOMEWORK_STORAGE.get(date_key, {}).get(subject)
    if not info:
        bot.answer_callback_query(call.id, "📭 ДЗ вже немає", show_alert=True); return
    user_states[call.from_user.id] = {"action":"waiting_homework_edit", "date_key":date_key, "subject":subject}
    bot.answer_callback_query(call.id)
    current = html.escape(info.get("text") or "📸 Фото завдання")
    safe_edit_message_text(f"✏️ <b>{html.escape(subject)}</b> — {datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')}\n\nПоточне ДЗ:\n{current}\n\nНадішли новий текст ДЗ.", call.message.chat.id, call.message.message_id, reply_markup=admin_cancel_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_delete_hw_start")
def admin_delete_hw_start(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        "🗑 <b>Видалення ДЗ</b>\n\nОбери дату, з якої потрібно видалити ДЗ:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=homework_date_picker_keyboard("admin_delete_hw_date", 0, 7),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_delete_hw_date:"))
def admin_delete_hw_date_selected(call):
    if not admin_only(call):
        return
    try:
        date_obj = datetime.date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "⚠️ Невірна дата", show_alert=True)
        return

    date_key = date_obj.isoformat()
    day_data = HOMEWORK_STORAGE.get(date_key, {})
    bot.answer_callback_query(call.id)

    if not day_data:
        safe_edit_message_text(
            f"📭 На <b>{date_obj.strftime('%d.%m.%Y')}</b> ДЗ немає.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_homework_keyboard(),
            parse_mode="HTML",
        )
        return

    k = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for subject in day_data:
        if subject in SUBJECTS_LIST:
            row.append(types.InlineKeyboardButton(
                f"🗑 {subject}",
                callback_data=f"admin_delete_hw:{date_key}:{SUBJECTS_LIST.index(subject)}"
            ))
            if len(row) == 2:
                k.add(*row)
                row = []
    if row:
        k.add(*row)
    k.add(types.InlineKeyboardButton("🔙 Керування ДЗ", callback_data="admin_homework"))
    safe_edit_message_text(
        f"🗑 <b>ДЗ на {date_obj.strftime('%d.%m.%Y')}</b>\n\nОбери предмет для видалення:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=k,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_delete_hw:") and not call.data.startswith("admin_delete_hw_confirm:"))
def admin_delete_hw_subject(call):
    if not admin_only(call):
        return
    _, date_key, idx = call.data.split(":", 2)
    try:
        subject = SUBJECTS_LIST[int(idx)]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет!", show_alert=True)
        return
    if subject not in HOMEWORK_STORAGE.get(date_key, {}):
        bot.answer_callback_query(call.id, "ДЗ вже видалено.", show_alert=True)
        return
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("🗑 Так, видалити", callback_data=f"admin_delete_hw_confirm:{date_key}:{idx}"),
        types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_homework"),
    )
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"⚠️ <b>Підтвердження видалення</b>\n\n📚 {html.escape(subject)}\n📅 {datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')}\n\nТочно видалити це ДЗ?",
        call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_delete_hw_confirm:"))
def admin_delete_hw_confirm(call):
    if not admin_only(call):
        return
    _, date_key, idx = call.data.split(":", 2)
    try:
        subject = SUBJECTS_LIST[int(idx)]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "⚠️ Невірний предмет!", show_alert=True); return
    if subject not in HOMEWORK_STORAGE.get(date_key, {}):
        bot.answer_callback_query(call.id, "ДЗ вже видалено.", show_alert=True); return
    HOMEWORK_STORAGE[date_key].pop(subject, None)
    if not HOMEWORK_STORAGE[date_key]:
        HOMEWORK_STORAGE.pop(date_key, None)
    save_data_to_file()
    log_admin_action(call.from_user.id, f"Видалено ДЗ: {subject} на {date_key}")
    bot.answer_callback_query(call.id, "🗑 ДЗ видалено!")
    admin_homework(call)


# ---------------------------------------------------------------------
# 📣 9. РОЗСИЛКА
# ---------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_start")

# ── Підрозділ: Розсилки ────────────────────────────────────────

def admin_broadcast_start(call):
    if not admin_only(call):
        return
    user_states[call.from_user.id] = {"action": "waiting_admin_broadcast_preview"}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📣 <b>Розсилка всім користувачам</b>\n\n"
        "Надішли текст або фото з підписом. Повідомлення буде надіслано всім користувачам, яких бот зберіг у статистиці.\n\n"
        "Натисни <b>❌ Скасувати</b>, якщо передумав.",
        reply_markup=admin_cancel_keyboard(),
        parse_mode="HTML"
    )


def broadcast_user_ids(audience, selected_ids=None):
    now = datetime.datetime.now()
    ids = []
    selected = {str(x) for x in (selected_ids or [])}
    for raw_uid, data in USERS_STORAGE.items():
        try:
            seen = datetime.datetime.fromisoformat(data.get("last_seen", ""))
        except Exception:
            seen = None
        include = True
        if audience == "selected":
            include = raw_uid in selected
        elif audience == "active":
            include = bool(seen and (now - seen).days <= 7)
        elif audience == "inactive":
            include = bool(not seen or (now - seen).days > 7)
        if include:
            try: ids.append(int(raw_uid))
            except (TypeError, ValueError): pass
    return ids


def broadcast_audience_label(audience):
    return {"all": "усі", "active": "активні за 7 днів", "inactive": "неактивні", "selected": "вибрані користувачі"}.get(audience, "усі")


def send_broadcast_payload(payload, audience="all", selected_ids=None):
    sent = failed = 0
    for uid in broadcast_user_ids(audience, selected_ids):
        try:
            if payload.get("photo"):
                bot.send_photo(uid, payload["photo"], caption=payload.get("caption_html") or payload.get("caption", ""), parse_mode="HTML")
            else:
                bot.send_message(uid, payload.get("html") or payload.get("text", ""), parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            logging.warning("Помилка розсилки користувачу %s: %s", uid, e)
    return sent, failed


@bot.callback_query_handler(func=lambda call: call.data in ("admin_broadcast_confirm", "admin_broadcast_cancel"))
def admin_broadcast_decision(call):
    if not admin_only(call): return
    state = user_states.get(call.from_user.id, {})
    if state.get("action") != "waiting_admin_broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Дані розсилки вже неактивні.", show_alert=True); return
    if call.data == "admin_broadcast_cancel":
        user_states.pop(call.from_user.id, None)
        bot.answer_callback_query(call.id, "Розсилку скасовано")
        safe_edit_message_text("❌ <b>Розсилку скасовано.</b>", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_keyboard(), parse_mode="HTML")
        return
    payload = state.get("payload", {})
    audience = state.get("audience", "all")
    selected_ids = state.get("selected_ids", [])
    sent, failed = send_broadcast_payload(payload, audience, selected_ids)
    recipients = len(broadcast_user_ids(audience, selected_ids))
    log_admin_action(call.from_user.id, f"Розсилка ({broadcast_audience_label(audience)}): успішно {sent}, помилок {failed}")
    user_states.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "📣 Розсилку виконано")
    safe_edit_message_text(f"📣 <b>Розсилку завершено</b>\n\n👥 Отримувачів: <b>{recipients}</b>\n✅ Надіслано: <b>{sent}</b>\n❌ Помилок: <b>{failed}</b>", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_broadcast_audience:"))
def admin_broadcast_audience(call):
    if not admin_only(call): return
    state = user_states.get(call.from_user.id, {})
    if state.get("action") != "waiting_admin_broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Розсилка вже неактивна.", show_alert=True); return
    audience = call.data.split(":", 1)[1]
    if audience not in ("all", "active", "inactive"):
        bot.answer_callback_query(call.id, "⚠️ Невірна аудиторія", show_alert=True); return
    state["audience"] = audience
    user_states[call.from_user.id] = state
    bot.answer_callback_query(call.id, f"👥 {broadcast_audience_label(audience)}")
    recipients = len(broadcast_user_ids(audience))
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("👥 Усі", callback_data="admin_broadcast_audience:all"), types.InlineKeyboardButton("🟢 Активні", callback_data="admin_broadcast_audience:active"))
    k.add(types.InlineKeyboardButton("⚪ Неактивні", callback_data="admin_broadcast_audience:inactive"))
    k.add(types.InlineKeyboardButton("🎯 Вибрати користувачів", callback_data="admin_broadcast_select_users"))
    k.add(types.InlineKeyboardButton("📅 Запланувати", callback_data="admin_broadcast_schedule"), types.InlineKeyboardButton("✅ Відправити", callback_data="admin_broadcast_confirm"))
    k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_broadcast_cancel"))
    safe_edit_message_text(f"📣 <b>ПОПЕРЕДНІЙ ПЕРЕГЛЯД РОЗСИЛКИ</b>\n\n{state.get('preview','')}\n\n👥 Аудиторія: <b>{broadcast_audience_label(audience)}</b>\n👥 Отримають: <b>{recipients}</b>", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_select_users")
def admin_broadcast_select_users(call):
    if not admin_only(call): return
    state=user_states.get(call.from_user.id,{})
    if state.get("action") != "waiting_admin_broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Розсилка вже неактивна.", show_alert=True); return
    state.setdefault("selected_ids", [])
    k=types.InlineKeyboardMarkup(row_width=1)
    users=list(USERS_STORAGE.items())
    users.sort(key=lambda x:x[1].get("last_seen",""), reverse=True)
    for uid,data in users[:30]:
        name=(f"{data.get('first_name','')} {data.get('last_name','')}").strip() or "Без імені"
        mark="✅" if uid in {str(x) for x in state["selected_ids"]} else "⬜"
        k.add(types.InlineKeyboardButton(f"{mark} {name} · {uid}", callback_data=f"admin_broadcast_select:{uid}"))
    k.add(types.InlineKeyboardButton("✅ Готово", callback_data="admin_broadcast_select_done"))
    k.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_broadcast_audience:all"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text(f"🎯 <b>ВИБІР КОРИСТУВАЧІВ</b>\n\nОбрано: <b>{len(state['selected_ids'])}</b>", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_broadcast_select:"))
def admin_broadcast_select(call):
    if not admin_only(call): return
    state=user_states.get(call.from_user.id,{})
    if state.get("action") != "waiting_admin_broadcast_confirm": return
    uid=call.data.split(":",1)[1]
    selected={str(x) for x in state.setdefault("selected_ids", [])}
    if uid in selected: selected.remove(uid)
    else: selected.add(uid)
    state["selected_ids"]=list(selected)
    state["audience"]="selected"
    user_states[call.from_user.id]=state
    bot.answer_callback_query(call.id)
    admin_broadcast_select_users(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_select_done")
def admin_broadcast_select_done(call):
    if not admin_only(call): return
    state=user_states.get(call.from_user.id,{})
    if state.get("action") != "waiting_admin_broadcast_confirm": return
    if not state.get("selected_ids"):
        bot.answer_callback_query(call.id, "⚠️ Обери хоча б одного користувача.", show_alert=True); return
    state["audience"]="selected"; user_states[call.from_user.id]=state
    bot.answer_callback_query(call.id, "🎯 Аудиторію вибрано")
    recipients=len(broadcast_user_ids("selected", state["selected_ids"]))
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("👥 Усі", callback_data="admin_broadcast_audience:all"), types.InlineKeyboardButton("🟢 Активні", callback_data="admin_broadcast_audience:active"))
    k.add(types.InlineKeyboardButton("⚪ Неактивні", callback_data="admin_broadcast_audience:inactive"), types.InlineKeyboardButton("🎯 Вибрані", callback_data="admin_broadcast_select_users"))
    k.add(types.InlineKeyboardButton("📅 Запланувати", callback_data="admin_broadcast_schedule"), types.InlineKeyboardButton("✅ Відправити", callback_data="admin_broadcast_confirm"))
    k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_broadcast_cancel"))
    safe_edit_message_text(f"📣 <b>ПОПЕРЕДНІЙ ПЕРЕГЛЯД</b>\n\n{state.get('preview','')}\n\n👥 Аудиторія: <b>вибрані користувачі</b>\n👥 Отримають: <b>{recipients}</b>", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_schedule")
def admin_broadcast_schedule(call):
    if not admin_only(call): return
    state = user_states.get(call.from_user.id, {})
    if state.get("action") != "waiting_admin_broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Розсилка вже неактивна.", show_alert=True); return
    state["action"] = "waiting_scheduled_broadcast_datetime"
    user_states[call.from_user.id] = state
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📅 <b>Запланувати розсилку</b>\n\nВведи дату і час у форматі:\n<code>05.10.2026 08:00</code>", reply_markup=admin_cancel_keyboard(), parse_mode="HTML")

# =====================================================================
# 💬 УНІВЕРСАЛЬНИЙ ОБРОБНИК ПОВІДОМЛЕНЬ
# =====================================================================
# Тут обробляються введення адміністратора та звичайні повідомлення.
# =====================================================================



@bot.message_handler(content_types=["text", "photo"])

# ── Підрозділ: Ввід адміністратора та користувача ─────────────

def handle_general_input(message):
    user_id = message.from_user.id
    register_user(message.from_user)

    if user_id not in user_states:
        return

    state = user_states.get(user_id)
    action = state.get("action")

    if action == "waiting_admin_user_search":
        if not admin_only(types.SimpleNamespace(from_user=message.from_user, id="")):
            user_states.pop(user_id, None)
            return
        q = (message.text or "").strip().lower()
        if not q:
            bot.send_message(message.chat.id, "❌ Введи ім'я, username або Telegram ID.")
            return
        user_states.pop(user_id, None)
        found = []
        for uid, data in USERS_STORAGE.items():
            blob = " ".join(str(data.get(k, "")) for k in ("first_name", "last_name", "username")) + " " + uid
            if q in blob.lower():
                found.append((uid, data))
        k = types.InlineKeyboardMarkup(row_width=1)
        for uid, data in found[:20]:
            name = (f"{data.get('first_name','')} {data.get('last_name','')}").strip() or "Без імені"
            k.add(types.InlineKeyboardButton(f"👤 {name} · {uid}", callback_data=f"admin_user:view:{uid}"))
        k.add(types.InlineKeyboardButton("🔙 Користувачі", callback_data="admin_users"))
        bot.send_message(message.chat.id, f"🔎 <b>Пошук: {html.escape(q)}</b>\n\nЗнайдено: <b>{len(found)}</b>", reply_markup=k, parse_mode="HTML")
        return

    if action == "waiting_admin_role_id":
        if user_id not in ADMIN_IDS:
            user_states.pop(user_id, None); return
        raw = (message.text or "").strip()
        try:
            target_id = int(raw)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Потрібен числовий Telegram ID.")
            return
        user_states.pop(user_id, None)
        ADMIN_IDS.add(target_id)
        ADMIN_ROLES[str(target_id)] = state.get("role", "content")
        save_data_to_file()
        log_admin_action(user_id, f"Додано адміністратора {target_id} з роллю {ADMIN_ROLES[str(target_id)]}")
        bot.send_message(message.chat.id, f"✅ Адміністратора <code>{target_id}</code> додано.\n👑 Роль: <b>{html.escape(ADMIN_ROLES[str(target_id)])}</b>", reply_markup=admin_panel_keyboard(), parse_mode="HTML")
        return

    if action == "waiting_scheduled_broadcast_datetime":
        raw = (message.text or "").strip()
        try:
            dt = datetime.datetime.strptime(raw, "%d.%m.%Y %H:%M")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Формат: <code>ДД.ММ.РРРР ГГ:ХХ</code>", parse_mode="HTML")
            return
        payload = state.get("payload", {})
        audience = state.get("audience", "all")
        SCHEDULED_BROADCASTS.append({
            "id": int(time.time() * 1000),
            "send_at": dt.isoformat(timespec="minutes"),
            "payload": payload,
            "audience": audience,
            "selected_ids": state.get("selected_ids", []),
            "created_by": user_id,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        save_data_to_file()
        log_admin_action(user_id, f"Заплановано розсилку на {dt.strftime('%d.%m.%Y %H:%M')} ({audience})")
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, f"📅 <b>Розсилку заплановано</b>\n\n🕒 {dt.strftime('%d.%m.%Y %H:%M')}\n👥 Аудиторія: <b>{audience}</b>", reply_markup=admin_panel_keyboard(), parse_mode="HTML")
        return

    if action == "waiting_custom_poll_question":
        question=(message.text or "").strip()
        if not question:
            bot.send_message(message.chat.id, "❌ Напиши питання опитування.")
            return
        user_states[user_id]={"action":"waiting_custom_poll_options","question":question}
        bot.send_message(message.chat.id, "🗳 Тепер надішли варіанти через кому.\nНаприклад: <code>Так, Ні, Не знаю</code>", reply_markup=admin_cancel_keyboard(), parse_mode="HTML")
        return

    if action == "waiting_custom_poll_options":
        options=[x.strip() for x in (message.text or "").split(",") if x.strip()]
        if len(options)<2:
            bot.send_message(message.chat.id, "❌ Потрібно щонайменше 2 варіанти через кому.")
            return
        # прибираємо дублікати, зберігаючи порядок
        options=list(dict.fromkeys(options))
        CUSTOM_POLL.update({"is_active":True,"question":state.get("question", ""),"options":options,"votes":{o:0 for o in options},"voted_users":{}})
        user_states.pop(user_id,None)
        save_data_to_file(); log_admin_action(user_id, f"Створено опитування: {state.get('question','')}")
        k=types.InlineKeyboardMarkup(row_width=2)
        k.add(types.InlineKeyboardButton("📊 Результати", callback_data="custom_poll_results"), types.InlineKeyboardButton("⏹ Завершити", callback_data="admin_custom_poll_finish"))
        bot.send_message(message.chat.id, "✅ <b>Опитування створено</b>\n\n"+custom_poll_text(), reply_markup=k, parse_mode="HTML")
        return

    if action == "waiting_admin_broadcast_preview":
        if user_id not in ADMIN_IDS:
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "⚠️ Доступ заборонено!")
            return
        if (message.text or "").strip().upper() == "СКАСУВАТИ":
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "❌ Розсилку скасовано.", reply_markup=admin_panel_keyboard())
            return
        if message.photo:
            caption_html = telegram_message_to_html(message)
            payload = {"photo": message.photo[-1].file_id, "caption": message.caption or "", "caption_html": caption_html}
            preview = "📣 <b>ПОПЕРЕДНІЙ ПЕРЕГЛЯД РОЗСИЛКИ</b>\n\n" + (caption_html or "📷 Фото без підпису")
        else:
            rich_html = telegram_message_to_html(message)
            payload = {"text": message.text or "", "html": rich_html}
            if not payload["text"].strip():
                bot.send_message(message.chat.id, "❌ Надішли текст або фото з підписом.")
                return
            preview = "📣 <b>ПОПЕРЕДНІЙ ПЕРЕГЛЯД РОЗСИЛКИ</b>\n\n" + rich_html
        user_states[user_id] = {"action": "waiting_admin_broadcast_confirm", "payload": payload, "audience": "all", "selected_ids": [], "preview": (rich_html if not message.photo else (caption_html or "📷 Фото без підпису"))}
        k = types.InlineKeyboardMarkup(row_width=2)
        k.add(types.InlineKeyboardButton("👥 Усі", callback_data="admin_broadcast_audience:all"), types.InlineKeyboardButton("🟢 Активні", callback_data="admin_broadcast_audience:active"))
        k.add(types.InlineKeyboardButton("⚪ Неактивні", callback_data="admin_broadcast_audience:inactive"))
        k.add(types.InlineKeyboardButton("📅 Запланувати", callback_data="admin_broadcast_schedule"), types.InlineKeyboardButton("✅ Відправити", callback_data="admin_broadcast_confirm"))
        k.add(types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_broadcast_cancel"))
        bot.send_message(message.chat.id, preview + f"\n\n👥 Аудиторія: <b>усі</b>\n👥 Отримають: <b>{len(USERS_STORAGE)}</b>", reply_markup=k, parse_mode="HTML")
        return

    if action == "waiting_admin_delete_hw_date":
        raw = (message.text or "").strip()
        try:
            date_obj = datetime.datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            bot.send_message(message.chat.id, "❌ Невірний формат. Введи ДД.ММ.РРРР.")
            return
        date_key = date_obj.isoformat()
        day_data = HOMEWORK_STORAGE.get(date_key, {})
        if not day_data:
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "📭 На цю дату ДЗ немає.", reply_markup=admin_panel_keyboard())
            return
        user_states.pop(user_id, None)
        k = types.InlineKeyboardMarkup(row_width=2)
        row = []
        for subject in day_data:
            if subject in SUBJECTS_LIST:
                row.append(types.InlineKeyboardButton(f"🗑 {subject}", callback_data=f"admin_delete_hw:{date_key}:{SUBJECTS_LIST.index(subject)}"))
                if len(row) == 2:
                    k.add(*row)
                    row = []
        if row:
            k.add(*row)
        k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, f"🗑 <b>ДЗ на {date_obj.strftime('%d.%m.%Y')}</b>\n\nОбери предмет для видалення:", reply_markup=k, parse_mode="HTML")
        return

    if action == "waiting_bells_change":
        global HAS_BELLS_CHANGES_TODAY, HAS_BELLS_CHANGES_TOMORROW, BELLS_CHANGES_TODAY_YES_TEXT, BELLS_CHANGES_TOMORROW_YES_TEXT
        target = state.get("target")
        textv = (message.text or "").strip()
        if target == "today":
            HAS_BELLS_CHANGES_TODAY = textv.upper() != "НІ"
            if HAS_BELLS_CHANGES_TODAY:
                BELLS_CHANGES_TODAY_YES_TEXT = textv
            CHANGE_STATUS_DATES["bells_today"] = datetime.date.today().isoformat()
        else:
            HAS_BELLS_CHANGES_TOMORROW = textv.upper() != "НІ"
            if HAS_BELLS_CHANGES_TOMORROW:
                BELLS_CHANGES_TOMORROW_YES_TEXT = textv
            CHANGE_STATUS_DATES["bells_tomorrow"] = (datetime.date.today()+datetime.timedelta(days=1)).isoformat()
        save_data_to_file()
        log_admin_action(user_id, f"Змінено зміни дзвінків: {target}")
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, "✅ Зміни розкладу дзвінків оновлено.", reply_markup=admin_panel_keyboard())
        return

    if action == "waiting_hw_search_date":
        raw=(message.text or "").strip()
        try: d=datetime.datetime.strptime(raw,"%d.%m.%Y").date()
        except ValueError:
            bot.send_message(message.chat.id,"❌ Невірна дата. Формат: ДД.ММ.РРРР."); return
        user_states.pop(user_id,None)
        day_data=HOMEWORK_STORAGE.get(d.isoformat(),{})
        lines=[f"📅 <b>ДЗ на {d.strftime('%d.%m.%Y')}</b>",""]
        if not day_data: lines.append("📭 На цю дату ДЗ не знайдено.")
        else:
            for subject,info in day_data.items(): lines.append(f"🔹 <b>{html.escape(subject)}</b>: {html.escape(info.get('text') or '📸 Фото завдання')}")
        bot.send_message(message.chat.id,"\n".join(lines),reply_markup=homework_menu_keyboard(user_id),parse_mode="HTML"); return

    if action == "waiting_hw_search_keyword":
        q=(message.text or "").strip().lower()
        if not q: bot.send_message(message.chat.id,"❌ Введи ключове слово або фразу."); return
        user_states.pop(user_id,None); found=[]
        for date_key,day_data in HOMEWORK_STORAGE.items():
            for subject,info in day_data.items():
                if q in f"{subject} {info.get('text','')}".lower(): found.append((date_key,subject,info.get('text') or '📸 Фото завдання'))
        lines=[f"🔤 <b>Результати пошуку: {html.escape(q)}</b>",""]
        if not found: lines.append("📭 Нічого не знайдено.")
        else:
            for date_key,subject,textv in sorted(found):
                lines.append(f"📅 <b>{datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')}</b> — 🔹 <b>{html.escape(subject)}</b>: {html.escape(textv)}")
        bot.send_message(message.chat.id,"\n".join(lines),reply_markup=homework_menu_keyboard(user_id),parse_mode="HTML"); return

    if action == "waiting_hw_date":
        raw=(message.text or "").strip()
        try: d=datetime.datetime.strptime(raw,"%d.%m.%Y").date()
        except ValueError:
            bot.send_message(message.chat.id,"❌ Невірний формат. Введи ДД.ММ.РРРР."); return
        user_states.pop(user_id,None); render_homework(d,message.chat.id); return

    if action == "waiting_hw_search":
        q=(message.text or "").strip().lower()
        user_states.pop(user_id,None)
        found=[]
        for date_key, day_data in HOMEWORK_STORAGE.items():
            for subject, info in day_data.items():
                blob=f"{subject} {info.get('text','')}".lower()
                if q in blob:
                    found.append((date_key,subject,info.get('text') or '📸 Фото завдання'))
        lines=[f"🔎 <b>Результати пошуку: {q}</b>",""]
        if not found: lines.append("📭 Нічого не знайдено.")
        else:
            for date_key,subject,textv in sorted(found): lines.append(f"📅 {datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')} — 🔹 <b>{subject}</b>: {textv}")
        bot.send_message(message.chat.id,"\n".join(lines),reply_markup=homework_menu_keyboard(user_id),parse_mode="HTML"); return

    if action == "waiting_hw_date_admin":
        raw=(message.text or "").strip()
        try: d=datetime.datetime.strptime(raw,"%d.%m.%Y").date()
        except ValueError:
            bot.send_message(message.chat.id,"❌ Невірний формат. Введи ДД.ММ.РРРР."); return
        state["date_key"]=d.isoformat(); state["action"]="waiting_homework"
        save_data_to_file()
        bot.send_message(
            message.chat.id,
            f"✍️ Тепер надішли текст або фото ДЗ з предмета <b>{state['subject']}</b> на {d.strftime('%d.%m.%Y')}.",
            reply_markup=admin_cancel_keyboard(),
            parse_mode="HTML"
        ); return

    if action == "waiting_schedule_change":
        global HAS_CHANGES_TODAY, HAS_CHANGES_TOMORROW, CHANGES_TODAY_YES_TEXT, CHANGES_TOMORROW_YES_TEXT
        target=state.get("target"); textv=(message.text or "").strip()
        if target=="today":
            HAS_CHANGES_TODAY=textv.upper()!="НІ"; CHANGES_TODAY_YES_TEXT=textv if HAS_CHANGES_TODAY else CHANGES_TODAY_YES_TEXT
            CHANGE_STATUS_DATES["schedule_today"] = datetime.date.today().isoformat()
        else:
            HAS_CHANGES_TOMORROW=textv.upper()!="НІ"; CHANGES_TOMORROW_YES_TEXT=textv if HAS_CHANGES_TOMORROW else CHANGES_TOMORROW_YES_TEXT
            CHANGE_STATUS_DATES["schedule_tomorrow"] = (datetime.date.today()+datetime.timedelta(days=1)).isoformat()
        save_data_to_file()
        log_admin_action(user_id,f"Змінено зміни розкладу: {target}"); user_states.pop(user_id,None)
        bot.send_message(message.chat.id,"✅ Зміни розкладу оновлено.",reply_markup=admin_panel_keyboard()); return

    if action == "waiting_homework_edit":
        date_key = state.get("date_key")
        subject = state.get("subject")
        textv = (message.text or message.caption or "").strip()
        if not textv:
            bot.send_message(message.chat.id, "❌ Надішли текст нового ДЗ.")
            return
        if date_key not in HOMEWORK_STORAGE or subject not in HOMEWORK_STORAGE[date_key]:
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "📭 ДЗ вже не знайдено.", reply_markup=admin_panel_keyboard())
            return
        HOMEWORK_STORAGE[date_key][subject]["text"] = textv
        if message.photo:
            HOMEWORK_STORAGE[date_key][subject]["photo"] = message.photo[-1].file_id
        save_data_to_file()
        log_admin_action(user_id, f"Змінено ДЗ: {subject} на {date_key}")
        user_states.pop(user_id, None)
        bot.send_message(message.chat.id, "✅ <b>ДЗ успішно змінено.</b>", reply_markup=admin_panel_keyboard(), parse_mode="HTML")
        return

    if action == "waiting_pinned_important_message":
        if user_id not in ADMIN_IDS:
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "⚠️ Доступ заборонено!")
            return
        textv = (message.text or message.caption or "").strip()
        if not textv:
            bot.send_message(message.chat.id, "❌ Надішли текст важливого повідомлення.")
            return
        PINNED_IMPORTANT_MESSAGE["text"] = textv
        PINNED_IMPORTANT_MESSAGE["html"] = telegram_message_to_html(message)
        PINNED_IMPORTANT_MESSAGE["created_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_data_to_file()
        log_admin_action(user_id, "Додано/змінено важливе повідомлення")
        user_states.pop(user_id, None)
        bot.send_message(
            message.chat.id,
            "✅ <b>Важливе повідомлення збережено!</b>\n\n"
            "Тепер користувачі побачать його в 📌 <b>Важливих повідомленнях</b>.",
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )
        return

    # Якщо це додавання/оновлення повідомлення від вчителя
    if action == "waiting_teacher_message":
        target = state.get("teacher_target")
        if not target:
            user_states.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "⚠️ Не вдалося визначити, для кого це повідомлення. Спробуй ще раз.",
            )
            return

        if message.photo:
            photo_id = message.photo[-1].file_id
            caption = message.caption or ""
            item = {"text": caption, "photo": photo_id, "important": False, "pinned": False}
        else:
            item = {"text": message.text or "", "photo": None, "important": False}

        if target == "class_teacher":
            TEACHER_MESSAGE_STORAGE["class_teacher"] = item
            target_title = "класної керівнички"
        else:
            TEACHER_MESSAGE_STORAGE.setdefault("subjects", {})
            TEACHER_MESSAGE_STORAGE["subjects"][target] = item
            target_title = f"предмета «{target}»"

        save_data_to_file()
        refresh_main_menu(message.chat.id)
        log_admin_action(user_id, f"Додано повідомлення: {target_title}")
        user_states.pop(user_id, None)

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "📢 До меню повідомлень",
                callback_data="back_to_teacher_msg_menu",
            )
        )

        bot.send_message(
            message.chat.id,
            f"✅ Повідомлення для {target_title} успішно додано!",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    # Якщо це додавання домашнього завдання
    if action == "waiting_homework":
        subject = state["subject"]
        date_key = state.get("date_key") or (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        homework_chat_id = state.get("chat_id", message.chat.id)
        homework_message_id = state.get("message_id")

        if message.photo:
            photo_id = message.photo[-1].file_id
            caption = message.caption or ""
            HOMEWORK_STORAGE.setdefault(date_key, {})[subject] = {
                "text": caption,
                "photo": photo_id,
            }
        else:
            HOMEWORK_STORAGE.setdefault(date_key, {})[subject] = {
                "text": message.text or "",
                "photo": None,
            }

        save_data_to_file()
        refresh_main_menu(message.chat.id)
        log_admin_action(user_id, f"Додано ДЗ: {subject} на {date_key}")
        user_states.pop(user_id, None)

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "📖 До меню домашніх завдань",
                callback_data="back_to_hw_menu",
            )
        )

        if homework_message_id:
            try:
                safe_edit_message_text(
                    f"✅ ДЗ з предмета <b>{subject}</b> на <b>{datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')}</b> успішно додано!",
                    homework_chat_id,
                    homework_message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                return
            except Exception:
                pass

        bot.send_message(
            message.chat.id,
            f"✅ ДЗ з предмета <b>{subject}</b> на <b>{datetime.date.fromisoformat(date_key).strftime('%d.%m.%Y')}</b> успішно додано!",
            reply_markup=keyboard,
            parse_mode="HTML",
        )


# =====================================================================
# ⚙️ НАЛАШТУВАННЯ БОТА (ЛИШЕ ДЛЯ АДМІНІСТРАТОРІВ)
# =====================================================================

@bot.callback_query_handler(func=lambda call: call.data == "admin_settings")

# ── Підрозділ: Налаштування ────────────────────────────────────

def admin_settings(call):
    if not admin_only(call):
        return
    bot.answer_callback_query(call.id)
    _render_admin_settings(call)


def _settings_status():
    auto_backup = "🟢 Увімкнено" if admin_setting("auto_backup", True) else "🔴 Вимкнено"
    stats = "🟢 Збирається" if admin_setting("collect_statistics", True) else "🔴 Вимкнена"
    poll = "🟢 Автоматично" if admin_setting("poll_auto_close", True) else "🔴 Вручну"
    cache = int(admin_setting("weather_cache_minutes", 10) or 10)
    keep = int(admin_setting("backup_keep_count", 10) or 10)
    level = str(admin_setting("log_level", "INFO")).upper()
    return auto_backup, stats, poll, cache, keep, level


def _admin_settings_keyboard():
    auto_backup, stats, poll, cache, keep, level = _settings_status()
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton(f"💾 Автобекап: {'🟢' if auto_backup.startswith('🟢') else '🔴'}", callback_data="admin_set_toggle:auto_backup"),
        types.InlineKeyboardButton(f"📊 Статистика: {'🟢' if stats.startswith('🟢') else '🔴'}", callback_data="admin_set_toggle:collect_statistics"),
    )
    k.add(
        types.InlineKeyboardButton(f"🗳 Автозакриття: {'🟢' if poll.startswith('🟢') else '🔴'}", callback_data="admin_set_toggle:poll_auto_close"),
        types.InlineKeyboardButton(f"🌤 Кеш: {cache} хв", callback_data="admin_weather_cache_menu"),
    )
    k.add(
        types.InlineKeyboardButton(f"📦 Копій: {keep}", callback_data="admin_backup_keep_menu"),
        types.InlineKeyboardButton(f"📝 Логи: {level}", callback_data="admin_log_level_toggle"),
    )
    k.add(types.InlineKeyboardButton("🎨 Налаштування меню", callback_data="admin_menu_settings"))
    k.add(types.InlineKeyboardButton("👑 Адміністратори та ролі", callback_data="admin_roles"))
    k.add(types.InlineKeyboardButton("🔔 Центр подій", callback_data="admin_events"))
    k.add(types.InlineKeyboardButton("🔄 Скинути налаштування", callback_data="admin_settings_reset"))
    k.add(types.InlineKeyboardButton("🔙 Адмін-панель", callback_data="admin_panel"))
    return k


def _render_admin_settings(call):
    auto_backup, stats, poll, cache, keep, level = _settings_status()
    text = (
        "⚙️ <b>НАЛАШТУВАННЯ БОТА</b>\n\n"
        f"💾 Автоматичний backup: <b>{auto_backup}</b>\n"
        f"📊 Збір статистики: <b>{stats}</b>\n"
        f"🗳 Автозакриття опитувань: <b>{poll}</b>\n"
        f"⏰ Дедлайн нового опитування: <b>{int(admin_setting('poll_deadline_hours', 24))} год.</b>\n"
        f"🌤 Кеш погоди: <b>{cache} хв.</b>\n"
        f"📦 Автоматично залишати копій: <b>{keep}</b>\n"
        f"📝 Рівень логування: <b>{level}</b>\n\n"
        "Натискай кнопки нижче, щоб змінити параметри. Зміни зберігаються автоматично."
    )
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=_admin_settings_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_set_toggle:"))
def admin_set_toggle(call):
    if not admin_only(call):
        return
    key = call.data.split(":", 1)[1]
    if key not in {"auto_backup", "collect_statistics", "poll_auto_close"}:
        bot.answer_callback_query(call.id, "⚠️ Невідома настройка", show_alert=True)
        return
    new_value = not bool(admin_setting(key, True))
    set_admin_setting(key, new_value)
    log_admin_action(call.from_user.id, f"Змінено налаштування {key}: {new_value}")
    bot.answer_callback_query(call.id, "🟢 Увімкнено" if new_value else "🔴 Вимкнено")
    _render_admin_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_weather_cache_menu")
def admin_weather_cache_menu(call):
    if not admin_only(call):
        return
    k = types.InlineKeyboardMarkup(row_width=3)
    for minutes in (5, 10, 30):
        label = f"{'✅ ' if int(admin_setting('weather_cache_minutes', 10)) == minutes else ''}{minutes} хв"
        k.add(types.InlineKeyboardButton(label, callback_data=f"admin_weather_cache:{minutes}"))
    k.add(types.InlineKeyboardButton("🔙 Налаштування", callback_data="admin_settings"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text("🌤 <b>КЕШ ПОГОДИ</b>\n\nОбери, як часто бот може оновлювати погоду з API:", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_weather_cache:"))
def admin_weather_cache_set(call):
    if not admin_only(call):
        return
    minutes = int(call.data.split(":", 1)[1])
    if minutes not in (5, 10, 30):
        bot.answer_callback_query(call.id, "⚠️ Невірне значення", show_alert=True)
        return
    set_admin_setting("weather_cache_minutes", minutes)
    log_admin_action(call.from_user.id, f"Кеш погоди: {minutes} хв")
    bot.answer_callback_query(call.id, f"🌤 Кеш: {minutes} хв")
    _render_admin_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_backup_keep_menu")
def admin_backup_keep_menu(call):
    if not admin_only(call):
        return
    k = types.InlineKeyboardMarkup(row_width=3)
    for count in (5, 10, 20):
        label = f"{'✅ ' if int(admin_setting('backup_keep_count', 10)) == count else ''}{count}"
        k.add(types.InlineKeyboardButton(label, callback_data=f"admin_backup_keep:{count}"))
    k.add(types.InlineKeyboardButton("🔙 Налаштування", callback_data="admin_settings"))
    bot.answer_callback_query(call.id)
    safe_edit_message_text("📦 <b>КІЛЬКІСТЬ BACKUP</b>\n\nСкільки останніх автоматичних копій залишати в папці резервних копій?", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_backup_keep:"))
def admin_backup_keep_set(call):
    if not admin_only(call):
        return
    count = int(call.data.split(":", 1)[1])
    if count not in (5, 10, 20):
        bot.answer_callback_query(call.id, "⚠️ Невірне значення", show_alert=True)
        return
    set_admin_setting("backup_keep_count", count)
    log_admin_action(call.from_user.id, f"Ліміт backup: {count}")
    bot.answer_callback_query(call.id, f"📦 Залишатиметься: {count}")
    _render_admin_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_log_level_toggle")
def admin_log_level_toggle(call):
    if not admin_only(call):
        return
    current = str(admin_setting("log_level", "INFO")).upper()
    new_level = "DEBUG" if current == "INFO" else "INFO"
    set_admin_setting("log_level", new_level)
    logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))
    log_admin_action(call.from_user.id, f"Рівень логування: {new_level}")
    bot.answer_callback_query(call.id, f"📝 Логи: {new_level}")
    _render_admin_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_settings_reset")
def admin_settings_reset(call):
    if not admin_only(call):
        return
    ADMIN_SETTINGS.update({
        "auto_backup": True,
        "weather_cache_minutes": 10,
        "poll_auto_close": True,
        "poll_deadline_hours": 24,
        "collect_statistics": True,
        "backup_keep_count": 10,
        "log_level": "INFO",
        "menu_visible": {k: True for k in DEFAULT_MENU_ORDER},
        "menu_order": list(DEFAULT_MENU_ORDER),
    })
    save_data_to_file()
    logging.getLogger().setLevel(logging.INFO)
    log_admin_action(call.from_user.id, "Скинуто налаштування бота до стандартних")
    bot.answer_callback_query(call.id, "🔄 Налаштування скинуто")
    _render_admin_settings(call)


# ── Підрозділ: Фонові задачі та авто-бекап ─────────────────────

def automatic_backup_if_needed():
    """Автоматично створює одну копію на календарний день."""
    if not admin_setting("auto_backup", True):
        return False
    folder = os.path.join(DIR_BACKUP, "backups")
    os.makedirs(folder, exist_ok=True)
    today_prefix = datetime.datetime.now().strftime("backup_%Y-%m-%d_")
    if any(name.startswith(today_prefix) and name.endswith(".json") for name in os.listdir(folder)):
        return False
    try:
        _create_backup_snapshot()
        keep = max(1, int(admin_setting("backup_keep_count", 10) or 10))
        files = _backup_snapshot_files()
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
        logging.info("Автоматичний щоденний backup створено.")
        return True
    except Exception as e:
        logging.error("Помилка автоматичного backup: %s", e)
        return False


def background_tasks_worker():
    last_backup_date = None
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date()
            # Автоматичний backup один раз на день.
            if last_backup_date != today:
                automatic_backup_if_needed()
                last_backup_date = today
            # Автоматично закриваємо опитування після дедлайну, якщо це дозволено в налаштуваннях.
            if admin_setting("poll_auto_close", True) and "poll_is_active" in globals():
                poll_is_active()

            # Виконуємо заплановані розсилки.
            now = datetime.datetime.now()
            changed = False
            for item in list(SCHEDULED_BROADCASTS):
                try:
                    send_at = datetime.datetime.fromisoformat(item.get("send_at", ""))
                except Exception:
                    send_at = now + datetime.timedelta(days=9999)
                if send_at <= now and not item.get("sent"):
                    sent, failed = send_broadcast_payload(item.get("payload", {}), item.get("audience", "all"), item.get("selected_ids", []))
                    item["sent"] = True
                    item["sent_at"] = now.isoformat(timespec="seconds")
                    item["result"] = {"sent": sent, "failed": failed}
                    record_admin_event("broadcast", f"Заплановану розсилку виконано: {sent} успішно, {failed} помилок", item.get("created_by"))
                    changed = True
            if changed:
                SCHEDULED_BROADCASTS[:] = [x for x in SCHEDULED_BROADCASTS if not (x.get("sent") and x.get("sent_at") and (now - datetime.datetime.fromisoformat(x["sent_at"])).days > 7)]
                save_data_to_file()

        except Exception:
            logging.exception("Помилка фонового завдання")
        time.sleep(30)


@bot.callback_query_handler(func=lambda call: call.data == "admin_scheduled_broadcasts")

# ── Підрозділ: Заплановані розсилки ───────────────────────────

def admin_scheduled_broadcasts(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    lines = ["📅 <b>ЗАПЛАНОВАНІ РОЗСИЛКИ</b>", ""]
    pending = [x for x in SCHEDULED_BROADCASTS if not x.get("sent")]
    pending.sort(key=lambda x: x.get("send_at", ""))
    if not pending:
        lines.append("📭 Запланованих розсилок немає.")
    else:
        for item in pending[:20]:
            try: dt=datetime.datetime.fromisoformat(item.get("send_at","")).strftime("%d.%m.%Y %H:%M")
            except Exception: dt=item.get("send_at","")
            lines.append(f"📅 <b>{html.escape(dt)}</b> — 👥 {html.escape(broadcast_audience_label(item.get('audience','all')))}")
    k=types.InlineKeyboardMarkup(row_width=1)
    for item in pending[:10]:
        k.add(types.InlineKeyboardButton(f"🗑 Скасувати {item.get('send_at','')[:16].replace('T',' ')}", callback_data=f"admin_scheduled_cancel:{item.get('id')}"))
    k.add(types.InlineKeyboardButton("🔄 Оновити", callback_data="admin_scheduled_broadcasts"), types.InlineKeyboardButton("🔙 Користувачі", callback_data="admin_section_users"))
    safe_edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_scheduled_cancel:"))
def admin_scheduled_cancel(call):
    if not admin_only(call): return
    item_id = call.data.split(":",1)[1]
    before=len(SCHEDULED_BROADCASTS)
    SCHEDULED_BROADCASTS[:] = [x for x in SCHEDULED_BROADCASTS if str(x.get("id")) != item_id]
    save_data_to_file()
    if len(SCHEDULED_BROADCASTS) < before:
        log_admin_action(call.from_user.id, f"Скасовано заплановану розсилку {item_id}")
        bot.answer_callback_query(call.id, "🗑 Скасовано")
    else:
        bot.answer_callback_query(call.id, "Не знайдено", show_alert=True)
    admin_scheduled_broadcasts(call)

# =====================================================================
# 🎨 РОЗШИРЕНІ НАЛАШТУВАННЯ: МЕНЮ, КОРИСТУВАЧІ, РОЛІ ТА ПОДІЇ
# =====================================================================

# ── Підрозділ: Налаштування меню ───────────────────────────────

def _menu_settings_keyboard():
    k = types.InlineKeyboardMarkup(row_width=3)
    order = ADMIN_SETTINGS.get("menu_order", DEFAULT_MENU_ORDER)
    visible = ADMIN_SETTINGS.get("menu_visible", {})
    for i, key in enumerate(order):
        state = "🟢" if visible.get(key, True) else "⚪"
        k.add(
            types.InlineKeyboardButton(f"{state} {MENU_LABELS.get(key, key)[:28]}", callback_data=f"admin_menu_toggle:{key}"),
            types.InlineKeyboardButton("⬆️", callback_data=f"admin_menu_move:{key}:up"),
            types.InlineKeyboardButton("⬇️", callback_data=f"admin_menu_move:{key}:down"),
        )
    k.add(types.InlineKeyboardButton("🔄 Скинути порядок", callback_data="admin_menu_reset"))
    k.add(types.InlineKeyboardButton("🔙 Налаштування", callback_data="admin_settings"))
    return k


@bot.callback_query_handler(func=lambda call: call.data == "admin_menu_settings")
def admin_menu_settings(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    visible = ADMIN_SETTINGS.get("menu_visible", {})
    shown = sum(1 for x in DEFAULT_MENU_ORDER if visible.get(x, True))
    safe_edit_message_text(
        f"🎨 <b>НАЛАШТУВАННЯ МЕНЮ</b>\n\n🟢 Показано: <b>{shown}</b>\n⚪ Приховано: <b>{len(DEFAULT_MENU_ORDER)-shown}</b>\n\nНатискай назву, щоб показати/сховати кнопку. Стрілки змінюють її порядок.",
        call.message.chat.id, call.message.message_id, reply_markup=_menu_settings_keyboard(), parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_menu_toggle:"))
def admin_menu_toggle(call):
    if not admin_only(call): return
    key = call.data.split(":",1)[1]
    if key not in DEFAULT_MENU_ORDER: return
    ADMIN_SETTINGS.setdefault("menu_visible", {})[key] = not ADMIN_SETTINGS.get("menu_visible", {}).get(key, True)
    save_data_to_file(); log_admin_action(call.from_user.id, f"Меню: {'показано' if ADMIN_SETTINGS['menu_visible'][key] else 'приховано'} {MENU_LABELS.get(key,key)}")
    bot.answer_callback_query(call.id, "🟢 Показано" if ADMIN_SETTINGS['menu_visible'][key] else "⚪ Приховано")
    admin_menu_settings(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_menu_move:"))
def admin_menu_move(call):
    if not admin_only(call): return
    parts = call.data.split(":")
    if len(parts) != 3: return
    key, direction = parts[1], parts[2]
    order = ADMIN_SETTINGS.get("menu_order", list(DEFAULT_MENU_ORDER))
    if key not in order: return
    i = order.index(key)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(order): order[i], order[j] = order[j], order[i]
    ADMIN_SETTINGS["menu_order"] = order
    save_data_to_file(); bot.answer_callback_query(call.id)
    admin_menu_settings(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_menu_reset")
def admin_menu_reset(call):
    if not admin_only(call): return
    ADMIN_SETTINGS["menu_order"] = list(DEFAULT_MENU_ORDER)
    ADMIN_SETTINGS["menu_visible"] = {x: True for x in DEFAULT_MENU_ORDER}
    save_data_to_file(); log_admin_action(call.from_user.id, "Скинуто налаштування головного меню")
    bot.answer_callback_query(call.id, "🔄 Меню скинуто")
    admin_menu_settings(call)


ROLE_NAMES = {"owner": "👑 Головний адміністратор", "content": "🛠 Контент-адмін", "moderator": "📊 Модератор"}


# ── Підрозділ: Ролі адміністраторів ───────────────────────────

def _roles_keyboard():
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("➕ Додати адміна", callback_data="admin_role_add"))
    for uid in sorted(ADMIN_IDS):
        role = admin_role(uid)
        k.add(types.InlineKeyboardButton(f"{ROLE_NAMES.get(role, role)} · {uid}", callback_data=f"admin_role_user:{uid}"))
    k.add(types.InlineKeyboardButton("🔙 Налаштування", callback_data="admin_settings"))
    return k


@bot.callback_query_handler(func=lambda call: call.data == "admin_roles")
def admin_roles(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    text = "👑 <b>КЕРУВАННЯ АДМІНІСТРАТОРАМИ</b>\n\nРолі визначають доступ до розділів адмін-панелі:\n\n👑 Головний — усе\n🛠 Контент-адмін — контент\n📊 Модератор — користувачі та контроль\n\n👇 Обери адміністратора або додай нового."
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=_roles_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_role_add")
def admin_role_add(call):
    if not admin_only(call): return
    if admin_role(call.from_user.id) != "owner":
        bot.answer_callback_query(call.id, "⚠️ Додавати адміністраторів може лише головний адміністратор.", show_alert=True); return
    user_states[call.from_user.id] = {"action": "waiting_admin_role_id", "role": "content"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "➕ <b>Новий адміністратор</b>\n\nНадішли Telegram ID користувача числом. Потім можна буде змінити його роль.", reply_markup=admin_cancel_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_role_user:"))
def admin_role_user(call):
    if not admin_only(call): return
    uid = int(call.data.split(":",1)[1])
    role = admin_role(uid)
    k = types.InlineKeyboardMarkup(row_width=2)
    if role != "owner":
        k.add(types.InlineKeyboardButton("🛠 Контент", callback_data=f"admin_role_set:{uid}:content"), types.InlineKeyboardButton("📊 Модератор", callback_data=f"admin_role_set:{uid}:moderator"))
        k.add(types.InlineKeyboardButton("🗑 Забрати права", callback_data=f"admin_role_remove:{uid}"))
    k.add(types.InlineKeyboardButton("🔙 Адміністратори", callback_data="admin_roles"))
    name = USERS_STORAGE.get(str(uid), {}).get("first_name") or "Невідомий"
    safe_edit_message_text(f"👤 <b>{html.escape(str(name))}</b>\n\nID: <code>{uid}</code>\nРоль: <b>{ROLE_NAMES.get(role, role)}</b>", call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_role_set:"))
def admin_role_set(call):
    if not admin_only(call) or admin_role(call.from_user.id) != "owner": return
    _, uid, role = call.data.split(":")
    uid = int(uid)
    if role not in ROLE_PERMISSIONS: return
    ADMIN_IDS.add(uid); ADMIN_ROLES[str(uid)] = role
    save_data_to_file(); log_admin_action(call.from_user.id, f"Змінено роль адміна {uid}: {role}")
    bot.answer_callback_query(call.id, "✅ Роль змінено")
    admin_role_user(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_role_remove:"))
def admin_role_remove(call):
    if not admin_only(call) or admin_role(call.from_user.id) != "owner": return
    uid = int(call.data.split(":",1)[1])
    if uid == call.from_user.id or admin_role(uid) == "owner":
        bot.answer_callback_query(call.id, "⚠️ Головного адміністратора не можна забрати.", show_alert=True); return
    ADMIN_IDS.discard(uid); ADMIN_ROLES.pop(str(uid), None)
    save_data_to_file(); log_admin_action(call.from_user.id, f"Забрано права адміністратора: {uid}")
    bot.answer_callback_query(call.id, "🗑 Права забрано")
    admin_roles(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_events")

# ── Підрозділ: Центр подій ────────────────────────────────────

def admin_events(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    lines = ["🔔 <b>ОСТАННІ ПОДІЇ</b>", ""]
    if not ADMIN_EVENT_LOG:
        lines.append("📭 Подій ще немає.")
    else:
        for item in ADMIN_EVENT_LOG[-15:][::-1]:
            try: t = datetime.datetime.fromisoformat(item.get("time","")).strftime("%d.%m.%Y %H:%M")
            except Exception: t = item.get("time","")
            icon = {"user":"🟢", "admin":"🛠", "broadcast":"📣"}.get(item.get("type"), "🔔")
            lines.append(f"{icon} <b>{html.escape(t)}</b> — {html.escape(item.get('text',''))}")
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🔄 Оновити", callback_data="admin_events"), types.InlineKeyboardButton("🧹 Очистити", callback_data="admin_events_clear"))
    k.add(types.InlineKeyboardButton("🔙 Налаштування", callback_data="admin_settings"))
    safe_edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "admin_events_clear")
def admin_events_clear(call):
    if not admin_only(call): return
    ADMIN_EVENT_LOG.clear(); save_data_to_file(); log_admin_action(call.from_user.id, "Очищено центр подій")
    bot.answer_callback_query(call.id, "🧹 Події очищено")
    admin_events(call)


@bot.callback_query_handler(func=lambda call: call.data == "admin_system_check")
def admin_system_check_extended(call):
    if not admin_only(call): return
    bot.answer_callback_query(call.id)
    telegram_ok = False
    try:
        bot.get_me(); telegram_ok = True
    except Exception:
        telegram_ok = False
    json_ok = os.path.exists(BACKUP_FILE_PATH)
    sqlite_ok = os.path.exists(SQLITE_FILE_PATH)
    weather_ok = WEATHER_CACHE.get("data") is not None
    text = (
        "⚡ <b>СТАН СИСТЕМИ</b>\n\n"
        f"🤖 Telegram: <b>{'🟢' if telegram_ok else '🔴'}</b>\n"
        f"💾 JSON: <b>{'🟢' if json_ok else '🔴'}</b>\n"
        f"🗄 SQLite: <b>{'🟢' if sqlite_ok else '🔴'}</b>\n"
        f"🌤 Weather API: <b>{'🟢' if weather_ok else '⚪ Не перевірено'}</b>\n"
        f"📦 Backup: <b>{'🟢' if _backup_snapshot_files() else '⚪'}</b>\n"
        f"📚 ДЗ: <b>{'🟢' if isinstance(HOMEWORK_STORAGE, dict) else '🔴'}</b>\n"
        f"📢 Повідомлення: <b>{'🟢' if isinstance(TEACHER_MESSAGE_STORAGE, dict) else '🔴'}</b>\n"
        f"🗳 Опитування: <b>{'🟢' if poll_data.get('is_active') else '⚪'}</b>\n\n"
        f"🕒 Перевірено: <b>{datetime.datetime.now().strftime('%H:%M:%S')}</b>"
    )
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🔄 Перевірити ще раз", callback_data="admin_system_check"), types.InlineKeyboardButton("🔙 Система", callback_data="admin_system"))
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=k, parse_mode="HTML")

# =====================================================================
# 🚀 ЗАПУСК БОТА
# =====================================================================

if __name__ == "__main__":
    save_data_to_file()
    automatic_backup_if_needed()
    threading.Thread(target=background_tasks_worker, daemon=True, name="bot-background-tasks").start()
    logging.info("Бот успішно запущено і готовий до роботи!")

# ─────────────────────────────────────────────────────────────
# 🌐 WEBHOOK / ДВА ПРИСТРОЇ
#
# Для справжньої роботи ПК + телефона одночасно Telegram updates
# приймає один публічний HTTPS webhook. Обидва пристрої можуть
# запускати цей самий код, але сам webhook має бути на сервері,
# доступному з Інтернету.
#
# У .env:
# BOT_INSTANCE=PRIMARY або SECONDARY
# WEBHOOK_URL=https://твій-домен.example.com
# WEBHOOK_PORT=8080
#
# PRIMARY запускає webhook-сервер.
# SECONDARY не створює другий webhook (Telegram дозволяє лише один).
# Обидва пристрої використовують спільну серверну базу/файли.
# ─────────────────────────────────────────────────────────────

BOT_INSTANCE = os.getenv("BOT_INSTANCE", "PRIMARY").strip().upper()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

if BOT_INSTANCE == "PRIMARY":
    if not WEBHOOK_URL:
        raise RuntimeError(
            "Для PRIMARY потрібно додати WEBHOOK_URL у .env, "
            "наприклад https://example.com"
        )

    try:
        from flask import Flask, request
    except ImportError as e:
        raise RuntimeError(
            "Для webhook встанови Flask: pip install flask"
        ) from e

    app = Flask(__name__)

    @app.get("/")
    def health():
        return "OK", 200

    @app.post("/telegram/webhook")
    def telegram_webhook():
        if not request.is_json:
            return "Bad Request", 400

        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        if update is not None:
            bot.process_new_updates([update])
        return "OK", 200

    # Telegram має надсилати updates тільки сюди.
    bot.remove_webhook()
    bot.set_webhook(
        url=f"{WEBHOOK_URL}/telegram/webhook",
        drop_pending_updates=False
    )

    logging.info("🟢 PRIMARY: webhook встановлено: %s/telegram/webhook", WEBHOOK_URL)
    logging.info("🌐 Webhook-сервер запускається на порту %s", WEBHOOK_PORT)

    app.run(host="0.0.0.0", port=WEBHOOK_PORT, threaded=True)

elif BOT_INSTANCE == "SECONDARY":
    logging.info("🟡 SECONDARY: працює без getUpdates і без другого webhook.")
    logging.info("🟡 Telegram updates обробляє PRIMARY/webhook-сервер.")
    # SECONDARY не викликає getUpdates і не змінює webhook.
    # Процес залишається запущеним для локальних задач/резервного копіювання.
    while True:
        time.sleep(60)

else:
    raise RuntimeError("BOT_INSTANCE має бути PRIMARY або SECONDARY.")

