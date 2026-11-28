"""CLI entry point for GIP 306 Luong 1: Export Pancake POS -> Overwrite Sheet -> Notify Telegram."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

MODULE_DIR = Path(__file__).resolve().parent
ROOT_DIR = MODULE_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import LOGS_DIR, REPORTS_DIR, ensure_runtime_dirs, load_dotenv  # noqa: E402
from gip306.pancake_client import export_gip306_new_orders, interactive_login  # noqa: E402
from gip306.sheets_sync import parse_pos_export_xlsx, sync_rows_to_sheet  # noqa: E402
from telegram_bot.bot import send_and_arm_reminder  # noqa: E402

TZ_VN = timezone(timedelta(hours=7))
LOCK_PATH = MODULE_DIR / "gip306_luong1.lock"


def _setup_logger(run_ts: datetime) -> logging.Logger:
    ensure_runtime_dirs()
    log_path = LOGS_DIR / f"run_gip306_{run_ts.strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger("gip306_luong1")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
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


def execute_luong1(
    apply_mode: bool,
    notify_telegram: bool,
    xlsx_override: Optional[Path],
    backend: str,
    headed: bool,
    run_ts: datetime,
    logger: logging.Logger,
) -> int:
    """Run strictly in order: 1) Export POS -> 2) Update Google Sheet -> 3) Notify Telegram."""
    logger.info(
        "=== BẮT ĐẦU GIP 306 LUỒNG 1 (%s) | %s ===",
        "APPLY" if apply_mode else "DRY-RUN",
        run_ts.strftime("%d/%m/%Y %H:%M"),
    )

    if xlsx_override is not None:
        xlsx_path = xlsx_override
        if not xlsx_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file Excel: {xlsx_path}")
        logger.info("Dùng file Excel có sẵn: %s", xlsx_path)
    else:
        xlsx_path, order_count = asyncio.run(
            export_gip306_new_orders(REPORTS_DIR, run_ts=run_ts, headless=not headed, logger=logger)
        )
        if xlsx_path is None or order_count == 0:
            logger.info("Không có đơn Mới nào ở bộ lọc LỌC LOẠI TRỪ -> dừng, không ghi đè Sheet và không nhắn Telegram.")
            return 0

    _, rows = parse_pos_export_xlsx(xlsx_path)
    logger.info("Đọc file Excel thành công: %d dòng dữ liệu (không tính tiêu đề).", len(rows))
    if not rows:
        logger.info("File Excel không có dòng dữ liệu nào -> dừng, không ghi đè Sheet và không nhắn Telegram.")
        return 0

    written = sync_rows_to_sheet(rows, backend=backend, dry_run=not apply_mode, logger=logger)

    if notify_telegram:
        if apply_mode:
            res = send_and_arm_reminder("gip306_update_link", dry_run=False, logger=logger)
            logger.info("Đã nhắn Telegram sau khi cập nhật %d dòng (message_id=%s).", written, res.get("message_id"))
        else:
            send_and_arm_reminder("gip306_update_link", dry_run=True, logger=logger)

    logger.info("=== HOÀN TẤT GIP 306 LUỒNG 1 (%s) | %d dòng ===", "APPLY" if apply_mode else "DRY-RUN", written)
    return 0


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GIP 306 Luồng 1: Export Pancake -> Sheet -> Telegram.")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra xuất/đọc, không ghi Sheet & không nhắn thật.")
    mode_grp.add_argument("--apply", action="store_true", help="Ghi đè Google Sheets thật.")
    parser.add_argument("--login", action="store_true", help="Mở trình duyệt đăng nhập Pancake POS 1 lần để lưu phiên.")
    parser.add_argument("--notify-telegram", action="store_true", help="Nhắn tag chị Tuyết sau khi cập nhật Sheet xong.")
    parser.add_argument("--xlsx", type=Path, default=None, help="Dùng file Excel có sẵn thay vì mở Pancake POS.")
    parser.add_argument(
        "--backend",
        choices=["auto", "apps_script", "gsheets_cli"],
        default="auto",
        help="Kênh ghi Google Sheets (mặc định: auto).",
    )
    parser.add_argument("--headed", action="store_true", help="Hiện cửa sổ trình duyệt khi xuất Pancake POS.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    args = _parse_args(argv)
    run_ts = datetime.now(TZ_VN)
    logger = _setup_logger(run_ts)

    if args.login:
        asyncio.run(interactive_login(logger=logger))
        return 0

    lock_fh = _acquire_lock()
    if lock_fh is None:
        logger.info("Đã có một tiến trình gip306_luong1 khác đang chạy; bỏ qua lượt này.")
        return 0
    try:
        return execute_luong1(
            apply_mode=bool(args.apply),
            notify_telegram=bool(args.notify_telegram),
            xlsx_override=args.xlsx,
            backend=args.backend,
            headed=bool(args.headed),
            run_ts=run_ts,
            logger=logger,
        )
    except Exception as exc:
        logger.exception("Lỗi GIP 306 Luồng 1 (đã dừng trước bước sau): %s", exc)
        return 1
    finally:
        _release_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
