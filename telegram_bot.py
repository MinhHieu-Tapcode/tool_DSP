"""Lightweight Telegram Bot long-polling listener (reply-only safe mode)."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import requests

from config import BASE_DIR, LOGS_DIR, ensure_runtime_dirs, load_dotenv

TZ_VN = timezone(timedelta(hours=7))
LOCK_PATH = BASE_DIR / "telegram_bot.lock"
LOG_PATH = LOGS_DIR / "telegram_bot.log"
TRIGGER_PHRASES = (
    "xu ly data",
    "chia data",
    "check trung",
    "chay tool",
)
REPLY_TEXT = "Vâng ạ 😊"


def normalize_vn(text: str) -> str:
    """Lowercase, map đ/Đ -> d, strip combining accents, and collapse whitespace."""
    s = (text or "").replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_like = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", ascii_like.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def should_reply_data_request(text: str) -> bool:
    """Return True if a message matches one of the configured data-request triggers."""
    norm = normalize_vn(text)
    if not norm:
        return False
    return any(phrase in norm for phrase in TRIGGER_PHRASES)


def _setup_logger() -> logging.Logger:
    ensure_runtime_dirs()
    logger = logging.getLogger("telegram_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={datetime.now(TZ_VN).isoformat()}\n")
        fh.flush()
        return fh
    except OSError:
        fh.close()
        return None


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


def _tg_request(
    session: requests.Session,
    token: str,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 35,
) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if payload is None:
            resp = session.get(url, timeout=timeout)
        else:
            resp = session.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        # Never expose the bot token embedded in the request URL
        raise RuntimeError(f"Telegram network error on {method}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code} on {method}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data.get('description', 'unknown')}")
    return data


def _load_allowed_chat_ids() -> Set[str]:
    allowed: Set[str] = set()
    primary = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if primary:
        allowed.add(primary)
    extra = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    for part in extra.split(","):
        cid = part.strip()
        if cid:
            allowed.add(cid)
    return allowed


def _extract_message_text(msg: Dict[str, Any]) -> str:
    return (msg.get("text") or msg.get("caption") or "").strip()


def run_polling(logger: logging.Logger) -> int:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Thiếu TELEGRAM_BOT_TOKEN trong .env hoặc biến môi trường.")
        return 1
    allowed_chat_ids = _load_allowed_chat_ids()
    if not allowed_chat_ids:
        logger.error("Thiếu TELEGRAM_CHAT_ID trong .env hoặc biến môi trường.")
        return 1

    session = requests.Session()
    me = _tg_request(session, token, "getMe", timeout=15)["result"]
    bot_id = me.get("id")
    username = me.get("username")
    can_read_all = me.get("can_read_all_group_messages")
    logger.info(
        "Bot sẵn sàng: @%s | can_read_all_group_messages=%s | allowed_chats=%s (reply-only safe mode)",
        username,
        can_read_all,
        sorted(allowed_chat_ids),
    )

    # Flush stale updates before entering long-polling loop
    offset: Optional[int] = None
    try:
        initial = _tg_request(
            session,
            token,
            "getUpdates",
            {"timeout": 0, "allowed_updates": ["message"]},
            timeout=15,
        )
        init_updates: List[Dict[str, Any]] = initial.get("result", [])
        if init_updates:
            offset = init_updates[-1]["update_id"] + 1
            logger.info("Bỏ qua %d update cũ trước khi khởi động; offset=%s", len(init_updates), offset)
    except Exception as exc:
        logger.warning("Không đọc được initial updates: %s", exc)

    logger.info("Đang lắng nghe tin nhắn nhóm (long-polling)...")
    while True:
        try:
            params: Dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            res = _tg_request(session, token, "getUpdates", params, timeout=35)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not isinstance(msg, dict):
                    continue
                sender = msg.get("from") or {}
                if sender.get("is_bot") or (bot_id is not None and sender.get("id") == bot_id):
                    continue
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                if chat_id not in allowed_chat_ids:
                    continue
                text = _extract_message_text(msg)
                if not text:
                    continue
                if should_reply_data_request(text):
                    sender_name = sender.get("first_name") or sender.get("username") or "unknown"
                    logger.info(
                        "Khớp từ khóa từ %s trong chat %s (msg_id=%s): %r -> reply",
                        sender_name,
                        chat_id,
                        msg.get("message_id"),
                        text[:120],
                    )
                    _tg_request(
                        session,
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": REPLY_TEXT,
                            "reply_to_message_id": msg.get("message_id"),
                        },
                        timeout=15,
                    )
        except KeyboardInterrupt:
            logger.info("Nhận tín hiệu dừng; thoát Telegram bot.")
            return 0
        except Exception as exc:
            logger.warning("Lỗi polling (thử lại sau 5s): %s", exc)
            time.sleep(5)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logger = _setup_logger()
    lock_fh = _acquire_lock()
    if lock_fh is None:
        logger.info("Đã có một tiến trình telegram_bot.py đang chạy; bỏ qua để tránh trùng.")
        return 0
    try:
        return run_polling(logger)
    finally:
        _release_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
