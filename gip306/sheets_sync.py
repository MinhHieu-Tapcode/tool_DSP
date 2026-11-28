"""Parse Pancake POS order export (.xlsx) and overwrite Google Sheet 'GIP 306 - DATA NEW'."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import requests
from openpyxl import load_workbook

GIP306_SPREADSHEET_ID = "10Zoz39Ltskaw94AkSCGqCJce1X7jaHfBB2nbefLSZUw"
GIP306_SHEET_NAME = "sheet"
GIP306_SHEET_GID = "398217873"
EXPECTED_HEADERS = ("so dien thoai", "san pham", "ma san pham")


def _norm_header(val: Any) -> str:
    s = str(val or "").replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", s)
    return " ".join("".join(c for c in nfkd if not unicodedata.combining(c)).lower().split())


def _cell_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()


def parse_pos_export_xlsx(xlsx_path: Path) -> Tuple[List[str], List[List[str]]]:
    """Read columns A:C from exported Pancake POS .xlsx, validating header and keeping multi-item blanks."""
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        all_rows = list(ws.iter_rows(min_row=1, max_col=3, values_only=True))
    finally:
        wb.close()

    if not all_rows:
        raise ValueError(f"File Excel rỗng: {xlsx_path}")

    headers = [_cell_str(c) for c in all_rows[0][:3]]
    while len(headers) < 3:
        headers.append("")
    normed = tuple(_norm_header(h) for h in headers)
    if normed != EXPECTED_HEADERS:
        raise ValueError(
            f"Sai cấu trúc cột A:C trong {xlsx_path.name}: nhận {headers}, "
            "cần đúng ['Số điện thoại', 'Sản phẩm', 'Mã sản phẩm']."
        )

    data_rows: List[List[str]] = []
    for raw in all_rows[1:]:
        row = [_cell_str(raw[i] if i < len(raw) else "") for i in range(3)]
        if any(row):
            data_rows.append(row)
    return headers, data_rows


def resolve_sync_backend(backend: str = "auto") -> str:
    """Resolve sheet sync backend ('apps_script' | 'gsheets_cli')."""
    b = (backend or "auto").strip().lower()
    if b in {"apps_script", "gsheets_cli"}:
        return b
    if os.environ.get("GIP306_SHEET_WEBHOOK_URL", "").strip():
        return "apps_script"
    if shutil.which("gsheets"):
        return "gsheets_cli"
    raise RuntimeError(
        "Chưa cấu hình kênh ghi Google Sheets. "
        "Hãy deploy Apps Script (gip306/apps_script_snippet.gs) và điền GIP306_SHEET_WEBHOOK_URL vào .env."
    )


def _sync_via_apps_script(
    rows: Sequence[Sequence[str]],
    sheet_name: str,
    logger: Optional[logging.Logger] = None,
) -> int:
    url = os.environ.get("GIP306_SHEET_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("Thiếu GIP306_SHEET_WEBHOOK_URL trong .env.")
    secret = os.environ.get("GIP306_SHEET_WEBHOOK_SECRET", "").strip()
    payload = {
        "secret": secret,
        "sheet_name": sheet_name,
        "rows": [list(r[:3]) for r in rows],
    }
    resp = requests.post(url, json=payload, timeout=45)
    if resp.status_code != 200:
        raise RuntimeError(f"Apps Script HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script báo lỗi: {data.get('error', 'unknown')}")
    written = int(data.get("written", len(rows)))
    if logger:
        logger.info("Đã xóa dữ liệu cũ và ghi đè %d dòng lên Google Sheets qua Apps Script.", written)
    return written


def _run_gsheets(args: Sequence[str]) -> str:
    proc = subprocess.run(["gsheets", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gsheets {' '.join(args[:2])} lỗi ({proc.returncode}): {err[:300]}")
    return proc.stdout


def _sync_via_gsheets_cli(
    rows: Sequence[Sequence[str]],
    spreadsheet_id: str,
    sheet_name: str,
    logger: Optional[logging.Logger] = None,
) -> int:
    _run_gsheets(["mutate", "clear", spreadsheet_id, f"{sheet_name}!A2:C10000"])
    if not rows:
        return 0
    ops = [{"op": "write", "range": f"{sheet_name}!A2", "data": [list(r[:3]) for r in rows]}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(ops, tf, ensure_ascii=False)
        req_path = Path(tf.name)
    try:
        _run_gsheets(["mutate", "batch", spreadsheet_id, "-f", str(req_path)])
    finally:
        req_path.unlink(missing_ok=True)
    if logger:
        logger.info("Đã xóa A2:C và ghi đè %d dòng lên Google Sheets qua gsheets CLI.", len(rows))
    return len(rows)


def sync_rows_to_sheet(
    rows: Sequence[Sequence[str]],
    backend: str = "auto",
    spreadsheet_id: str = GIP306_SPREADSHEET_ID,
    sheet_name: str = GIP306_SHEET_NAME,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Clear A2:C and overwrite with new rows, leaving header row 1 untouched."""
    if dry_run:
        if logger:
            logger.info("[DRY-RUN] Sẽ xóa %s!A2:C và ghi đè %d dòng mới.", sheet_name, len(rows))
        return len(rows)
    chosen = resolve_sync_backend(backend)
    if chosen == "apps_script":
        return _sync_via_apps_script(rows, sheet_name=sheet_name, logger=logger)
    return _sync_via_gsheets_cli(rows, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name, logger=logger)
