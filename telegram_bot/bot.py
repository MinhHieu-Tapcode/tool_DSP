"""Standalone rule-based Telegram auto-reply bot (long-polling, hot-reload rules)."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests

BOT_DIR = Path(__file__).resolve().parent
ROOT_DIR = BOT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import LOGS_DIR, ensure_runtime_dirs, load_dotenv  # noqa: E402

TZ_VN = timezone(timedelta(hours=7))
DEFAULT_RULES_PATH = BOT_DIR / "rules.json"
LOCK_PATH = BOT_DIR / "telegram_bot.lock"
LOG_PATH = LOGS_DIR / "telegram_bot.log"


def normalize_vn(text: str) -> str:
    """Lowercase, map đ/Đ -> d, strip combining accents, and collapse whitespace."""
    s = (text or "").replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_like = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", ascii_like.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def sender_matches(sender: Optional[Dict[str, Any]], allowed_users: Sequence[str]) -> bool:
    """Return True if allowed_users is empty or sender matches by user_id, @username, or name."""
    targets = [str(u).strip() for u in (allowed_users or []) if str(u).strip()]
    if not targets:
        return True
    if not isinstance(sender, dict):
        return False

    uid = str(sender.get("id", "")).strip()
    uname = str(sender.get("username", "")).strip().lower()
    first = str(sender.get("first_name", "")).strip()
    last = str(sender.get("last_name", "")).strip()
    full_norm = normalize_vn(f"{first} {last}".strip())
    first_norm = normalize_vn(first)

    for raw in targets:
        if raw.lstrip("-").isdigit():
            if uid and uid == raw:
                return True
        elif raw.startswith("@"):
            if uname and uname == raw[1:].lower():
                return True
        else:
            norm_t = normalize_vn(raw)
            if uname and uname == raw.lower():
                return True
            if norm_t and norm_t in {first_norm, full_norm}:
                return True
    return False


def text_matches(text: str, phrases: Sequence[str], match_mode: str = "exact") -> bool:
    """Compare normalized message text against phrases using 'exact' or 'contains'."""
    norm_text = normalize_vn(text)
    if not norm_text:
        return False
    mode = (match_mode or "exact").strip().lower()
    for phrase in phrases or []:
        norm_p = normalize_vn(phrase)
        if not norm_p:
            continue
        if mode == "contains":
            if norm_p in norm_text:
                return True
        elif norm_text == norm_p:
            return True
    return False


def match_rule(
    chat_id: str,
    sender: Optional[Dict[str, Any]],
    text: str,
    rules_cfg: Dict[str, Any],
    bot_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return the first matching enabled rule for (chat_id, sender, text), else None."""
    if not rules_cfg.get("enabled", True):
        return None
    if not isinstance(sender, dict):
        return None
    if sender.get("is_bot") or (bot_id is not None and sender.get("id") == bot_id):
        return None

    allowed_chats = {str(c).strip() for c in rules_cfg.get("allowed_chat_ids", []) if str(c).strip()}
    if not allowed_chats or str(chat_id).strip() not in allowed_chats:
        return None

    for rule in rules_cfg.get("rules", []):
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        if not sender_matches(sender, rule.get("allowed_users", [])):
            continue
        if text_matches(text, rule.get("phrases", []), rule.get("match_mode", "exact")):
            return rule
    return None


def load_rules(rules_path: Path = DEFAULT_RULES_PATH) -> Dict[str, Any]:
    """Load and return JSON rules config from rules_path."""
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"File cấu hình không hợp lệ (phải là JSON object): {rules_path}")
    return data


def format_mention(tag: str, user_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Format a Telegram mention as HTML user-id link or native @username ping."""
    clean = (tag or "").strip()
    if not clean:
        raise ValueError("Thiếu tên hoặc @username để tag.")
    if user_id and str(user_id).strip():
        uid = str(user_id).strip()
        label = clean.lstrip("@")
        return f'<a href="tg://user?id={html.escape(uid, quote=True)}">{html.escape(label)}</a>', "HTML"
    mention = clean if clean.startswith("@") else f"@{clean}"
    return mention, None


def render_template(
    template_name: str,
    rules_cfg: Dict[str, Any],
    tag: str,
    link: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Render a named message template with formatted Telegram mention and link."""
    templates = rules_cfg.get("templates") or {}
    tpl = templates.get(template_name)
    if not isinstance(tpl, dict) or not tpl.get("text"):
        raise ValueError(f"Không tìm thấy template '{template_name}' trong rules.json.")
    mention_str, parse_mode = format_mention(tag, user_id=user_id)
    final_link = (link or tpl.get("default_link") or "").strip()
    raw_text = str(tpl["text"])
    if parse_mode == "HTML":
        # Preserve template text safely around the HTML <a> mention tag
        escaped_tpl = html.escape(raw_text)
        rendered = escaped_tpl.replace("{tag}", mention_str).replace("{link}", html.escape(final_link))
        return rendered, "HTML"
    rendered = raw_text.replace("{tag}", mention_str).replace("{link}", final_link)
    return rendered, None


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
        raise RuntimeError(f"Telegram network error on {method}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code} on {method}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data.get('description', 'unknown')}")
    return data


def _extract_message_text(msg: Dict[str, Any]) -> str:
    return (msg.get("text") or msg.get("caption") or "").strip()


def _format_sender_tag(sender: Dict[str, Any]) -> str:
    uid = sender.get("id", "?")
    uname = sender.get("username")
    first = (sender.get("first_name") or "").strip()
    last = (sender.get("last_name") or "").strip()
    full = f"{first} {last}".strip() or "?"
    return f"{full} (id={uid}, @{uname or '-'})"


def run_polling(rules_path: Path, dry_run: bool, logger: logging.Logger) -> int:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Thiếu TELEGRAM_BOT_TOKEN trong .env hoặc biến môi trường.")
        return 1

    rules_cfg = load_rules(rules_path)
    last_mtime = rules_path.stat().st_mtime

    session = requests.Session()
    me = _tg_request(session, token, "getMe", timeout=15)["result"]
    bot_id = me.get("id")
    username = me.get("username")
    can_read_all = me.get("can_read_all_group_messages")
    logger.info(
        "Bot sẵn sàng (%s): @%s | can_read_all_group_messages=%s | enabled=%s | chats=%s | rules=%d",
        "DRY-RUN" if dry_run else "LIVE",
        username,
        can_read_all,
        rules_cfg.get("enabled", True),
        rules_cfg.get("allowed_chat_ids", []),
        len(rules_cfg.get("rules", [])),
    )

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

    logger.info("Đang lắng nghe theo cấu hình %s (tự tải lại khi sửa file)...", rules_path.name)
    while True:
        try:
            try:
                cur_mtime = rules_path.stat().st_mtime
                if cur_mtime != last_mtime:
                    rules_cfg = load_rules(rules_path)
                    last_mtime = cur_mtime
                    logger.info(
                        "Đã tự động tải lại %s | enabled=%s | chats=%s | rules=%d",
                        rules_path.name,
                        rules_cfg.get("enabled", True),
                        rules_cfg.get("allowed_chat_ids", []),
                        len(rules_cfg.get("rules", [])),
                    )
            except Exception as exc:
                logger.warning("Lỗi đọc lại %s (giữ cấu hình cũ): %s", rules_path.name, exc)

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
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                allowed_chats: Set[str] = {
                    str(c).strip() for c in rules_cfg.get("allowed_chat_ids", []) if str(c).strip()
                }
                if chat_id not in allowed_chats:
                    continue
                text = _extract_message_text(msg)
                if not text:
                    continue

                matched = match_rule(chat_id, sender, text, rules_cfg, bot_id=bot_id)
                if not matched:
                    if rules_cfg.get("log_unmatched_senders", True):
                        logger.info(
                            "[Bỏ qua] %s trong %s: %r",
                            _format_sender_tag(sender),
                            chat_id,
                            text[:100],
                        )
                    continue

                reply_text = str(matched.get("reply", "")).strip()
                if not reply_text:
                    continue
                rule_name = matched.get("name", "unnamed")
                logger.info(
                    "[Khớp rule=%s] %s trong %s (msg_id=%s): %r -> %r",
                    rule_name,
                    _format_sender_tag(sender),
                    chat_id,
                    msg.get("message_id"),
                    text[:100],
                    reply_text,
                )
                if dry_run:
                    continue

                delay = float(matched.get("typing_delay_sec", rules_cfg.get("default_typing_delay_sec", 0)) or 0)
                if delay > 0:
                    try:
                        _tg_request(
                            session,
                            token,
                            "sendChatAction",
                            {"chat_id": chat_id, "action": "typing"},
                            timeout=10,
                        )
                    except Exception:
                        pass
                    time.sleep(min(delay, 5.0))

                send_payload: Dict[str, Any] = {"chat_id": chat_id, "text": reply_text}
                if matched.get("reply_to_message", True) and msg.get("message_id") is not None:
                    send_payload["reply_to_message_id"] = msg["message_id"]
                _tg_request(session, token, "sendMessage", send_payload, timeout=15)

        except KeyboardInterrupt:
            logger.info("Nhận tín hiệu dừng; thoát Telegram bot.")
            return 0
        except Exception as exc:
            logger.warning("Lỗi polling (thử lại sau 5s): %s", exc)
            time.sleep(5)


def send_template_message(
    rules_path: Path,
    template_name: str,
    tag: str,
    link: Optional[str],
    user_id: Optional[str],
    chat_id_override: Optional[str],
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """Send a one-shot templated message (bypassing polling lock) to an allowed chat."""
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Thiếu TELEGRAM_BOT_TOKEN trong .env hoặc biến môi trường.")
        return 1

    rules_cfg = load_rules(rules_path)
    allowed_chats = [str(c).strip() for c in rules_cfg.get("allowed_chat_ids", []) if str(c).strip()]
    if not allowed_chats:
        logger.error("Thiếu allowed_chat_ids trong %s.", rules_path)
        return 1
    target_chat = (chat_id_override or allowed_chats[0]).strip()
    if target_chat not in allowed_chats:
        logger.error("Chat %s không nằm trong whitelist allowed_chat_ids=%s.", target_chat, allowed_chats)
        return 1

    text, parse_mode = render_template(template_name, rules_cfg, tag=tag, link=link, user_id=user_id)
    if dry_run:
        logger.info("[DRY-RUN template=%s -> %s] parse_mode=%s\n%s", template_name, target_chat, parse_mode, text)
        return 0

    payload: Dict[str, Any] = {"chat_id": target_chat, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    res = _tg_request(requests.Session(), token, "sendMessage", payload, timeout=15)
    msg_id = (res.get("result") or {}).get("message_id")
    logger.info("[Sent template=%s -> %s] message_id=%s\n%s", template_name, target_chat, msg_id, text)
    return 0


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Bot tự động trả lời theo rule JSON.")
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES_PATH,
        help="Đường dẫn tới file rules.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ log tin khớp rule / template, không gửi tin nhắn thật.",
    )
    parser.add_argument(
        "--send-template",
        type=str,
        default="",
        help="Gửi ngay 1 tin nhắn theo template trong rules.json (vd: update_link).",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default="",
        help="Tên/@username cần tag trong template (vd: @gipsupport).",
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default="",
        help="User ID tùy chọn để tag dạng link HTML clickable (nếu người nhận không có @username).",
    )
    parser.add_argument(
        "--link",
        type=str,
        default="",
        help="Link thay thế {link} trong template (mặc định dùng default_link).",
    )
    parser.add_argument(
        "--chat-id",
        type=str,
        default="",
        help="Chat ID trong allowed_chat_ids (mặc định dùng chat đầu tiên).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    logger = _setup_logger()
    if args.send_template:
        if not args.tag:
            logger.error("Cần truyền --tag (vd: --tag @gipsupport) khi dùng --send-template.")
            return 1
        return send_template_message(
            rules_path=args.rules,
            template_name=args.send_template,
            tag=args.tag,
            link=args.link or None,
            user_id=args.user_id or None,
            chat_id_override=args.chat_id or None,
            dry_run=bool(args.dry_run),
            logger=logger,
        )

    lock_fh = _acquire_lock()
    if lock_fh is None:
        logger.info("Đã có một tiến trình telegram_bot đang chạy; bỏ qua để tránh trùng.")
        return 0
    try:
        return run_polling(args.rules, dry_run=bool(args.dry_run), logger=logger)
    finally:
        _release_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
