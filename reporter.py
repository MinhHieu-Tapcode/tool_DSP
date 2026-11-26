"""Excel, JSON, and optional Telegram reporting for gsolution-auto."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from attendance import AttendanceResult
from classifier import ClassificationResult, OrderRecord
from config import REPORTS_DIR

LOGGER = logging.getLogger("gsolution_auto")

_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_CANCEL_FILL = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
_ASSIGN_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
_REVIEW_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")


def _style_sheet(ws, headers: Sequence[str], rows: Sequence[Sequence[Any]], row_fills: Optional[Sequence[Optional[PatternFill]]] = None) -> None:
    ws.append(list(headers))
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for r_idx, row in enumerate(rows, start=2):
        ws.append(list(row))
        fill = row_fills[r_idx - 2] if row_fills and (r_idx - 2) < len(row_fills) else None
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=r_idx, column=col_idx)
            c.alignment = Alignment(vertical="top", wrap_text=True)
            if fill is not None:
                c.fill = fill

    ws.freeze_panes = "A2"
    for col_idx in range(1, len(headers) + 1):
        max_len = max(
            [len(str(ws.cell(row=r, column=col_idx).value or "")) for r in range(1, min(len(rows) + 2, 120))]
            or [12]
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 4, 12), 48)


def _classification_fill(c: ClassificationResult) -> Optional[PatternFill]:
    if c.action == "CANCEL":
        return _CANCEL_FILL
    if c.action in {"ASSIGN", "CONVERT_TO_CALL", "TAG_ONLY"}:
        return _ASSIGN_FILL
    if c.action == "REVIEW":
        return _REVIEW_FILL
    return None


def write_reports(
    run_ts: datetime,
    mode: str,
    elapsed_sec: float,
    attendance: AttendanceResult,
    step2_total_new: int,
    step2_results: Sequence[ClassificationResult],
    step2_already_call: Sequence[OrderRecord],
    total_3a: int,
    step3a_results: Sequence[ClassificationResult],
    total_3b: int,
    step3b_results: Sequence[ClassificationResult],
    apply_log: Sequence[str],
    output_dir: Path = REPORTS_DIR,
) -> Tuple[Path, Path]:
    """Write Excel (.xlsx) and JSON (.json) reports and return their file paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_ts.strftime("%Y%m%d_%H%M%S")
    xlsx_path = output_dir / f"report_{stamp}.xlsx"
    json_path = output_dir / f"report_{stamp}.json"

    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    c3a = Counter(r.bucket for r in step3a_results)
    c3b = Counter(r.bucket for r in step3b_results)
    ambiguous_all = [r for r in (*step3a_results, *step3b_results) if r.bucket == "AMBIGUOUS"]

    sum_rows: List[List[Any]] = [
        ["Thời gian chạy", run_ts.strftime("%Y-%m-%d %H:%M:%S %Z")],
        ["Chế độ", mode],
        ["Thời lượng xử lý (giây)", round(elapsed_sec, 2)],
        ["Ngày kiểm tra lịch trực", attendance.date_header],
        ["Nguồn lịch trực", f"{attendance.source} (tab: {attendance.tab_name})"],
        ["Sale đi làm (nhận đơn Bước 5)", ", ".join(attendance.active_accounts) or "(Không có ai)"],
        ["", ""],
        ["Bước 2 — Tổng đơn New (3 GIP 85/319/244)", step2_total_new],
        ["Bước 2 — FILL_IN hoàn >= 50% cần chuyển CALL", len(step2_results)],
        ["Bước 2 — Đơn hoàn >= 50% đã là CALL sẵn", len(step2_already_call)],
        ["", ""],
        ["Bước 3A — Tổng đơn Duplicated Phone (New)", total_3a],
        ["Bước 3A — Đơn FILL_IN phân loại", len(step3a_results)],
        ["  • Hủy: TEST", c3a.get("TEST", 0)],
        ["  • Hủy: DUPLICATE", c3a.get("DUPLICATE", 0)],
        ["  • Hủy: MULTIPLE RETURNED", c3a.get("MULTIPLE RETURNED", 0)],
        ["  • Chia sale: DIFFERENT PRODUCT", c3a.get("DIFFERENT PRODUCT", 0)],
        ["  • Chia sale: RE ORDER", c3a.get("RE ORDER", 0)],
        ["  • Ca biên cần duyệt (AMBIGUOUS)", c3a.get("AMBIGUOUS", 0)],
        ["", ""],
        ["Bước 3B — Tổng đơn Not Duplicated Phone (New)", total_3b],
        ["Bước 3B — Đơn FILL_IN kiểm tra", len(step3b_results)],
        ["  • Hủy: TEST", c3b.get("TEST", 0)],
        ["  • Hủy: MULTIPLE RETURNED (giao thoa 3A)", c3b.get("MULTIPLE RETURNED", 0)],
        ["  • Chia sale: Hợp lệ", c3b.get("ASSIGN_ONLY", 0)],
        ["  • Ca biên cần duyệt (AMBIGUOUS)", c3b.get("AMBIGUOUS", 0)],
    ]
    if apply_log:
        sum_rows.append(["", ""])
        for idx, entry in enumerate(apply_log, start=1):
            sum_rows.append([f"Apply Log #{idx}", entry])

    _style_sheet(ws_sum, ["Hạng mục", "Kết quả"], sum_rows)

    # Sheet Step 2
    ws_s2 = wb.create_sheet("Step2_HighReturn")
    s2_rows = [
        [
            r.order.id,
            r.order.shop,
            r.order.service_types,
            f"{r.order.return_rate:.0f}%" if r.order.return_rate is not None else "",
            r.order.phone,
            r.order.products or r.order.internal_note,
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for r in step2_results
    ] + [
        [
            o.id,
            o.shop,
            o.service_types,
            f"{o.return_rate:.0f}%" if o.return_rate is not None else "",
            o.phone,
            o.products or o.internal_note,
            "ALREADY_CALL",
            "SKIP",
            "SKIPPED",
            "Đã là CALL sẵn",
        ]
        for o in step2_already_call
    ]
    _style_sheet(
        ws_s2,
        ["ID", "Shop", "Service", "Return Rate", "Phone", "Sản phẩm / Note", "Nhãn", "Hành động", "Trạng thái Apply", "Ghi chú"],
        s2_rows,
    )

    # Sheet Step 3A
    ws_3a = wb.create_sheet("Step3A_Duplicated")
    rows_3a = [
        [
            idx,
            r.order.id,
            r.order.shop,
            r.order.phone,
            r.order.customer_name,
            r.order.products or r.order.internal_note,
            r.returned_count,
            ", ".join(r.reference_ids),
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for idx, r in enumerate(step3a_results, start=1)
    ]
    _style_sheet(
        ws_3a,
        ["STT", "ID", "Shop", "Phone", "Khách hàng", "Sản phẩm / Internal Note", "Số đơn Returned", "Đơn tham chiếu", "Phân loại", "Hành động", "Trạng thái Apply", "Lý do"],
        rows_3a,
        [_classification_fill(r) for r in step3a_results],
    )

    # Sheet Step 3B
    ws_3b = wb.create_sheet("Step3B_NotDuplicated")
    rows_3b = [
        [
            idx,
            r.order.id,
            r.order.shop,
            r.order.phone,
            r.order.customer_name,
            r.order.products or r.order.internal_note,
            r.returned_count,
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for idx, r in enumerate(step3b_results, start=1)
    ]
    _style_sheet(
        ws_3b,
        ["STT", "ID", "Shop", "Phone", "Khách hàng", "Sản phẩm / Internal Note", "Số đơn Returned", "Phân loại", "Hành động", "Trạng thái Apply", "Lý do"],
        rows_3b,
        [_classification_fill(r) for r in step3b_results],
    )

    # Sheet Ambiguous Review
    ws_amb = wb.create_sheet("Ambiguous_Review")
    rows_amb = [
        [
            r.order.id,
            r.order.shop,
            r.order.phone,
            r.order.customer_name,
            r.order.internal_note,
            r.order.products,
            r.returned_count,
            ", ".join(r.reference_ids),
            r.reason,
        ]
        for r in ambiguous_all
    ]
    _style_sheet(
        ws_amb,
        ["ID", "Shop", "Phone", "Khách hàng", "Internal Note", "Products", "Returned", "Đơn tham chiếu", "Lý do cần duyệt"],
        rows_amb,
        [_REVIEW_FILL for _ in rows_amb],
    )

    wb.save(xlsx_path)

    payload = {
        "timestamp": run_ts.isoformat(),
        "mode": mode,
        "elapsed_seconds": round(elapsed_sec, 2),
        "attendance": {
            "date": attendance.date_header,
            "source": attendance.source,
            "tab_name": attendance.tab_name,
            "active_accounts": attendance.active_accounts,
            "staff": [asdict(s) for s in attendance.staff_statuses],
        },
        "step2": {
            "total_new": step2_total_new,
            "to_convert": [
                {"id": r.order.id, "shop": r.order.shop, "return_rate": r.order.return_rate, "applied": r.applied_status}
                for r in step2_results
            ],
            "already_call_count": len(step2_already_call),
        },
        "step3a": {
            "total_new": total_3a,
            "counts": dict(c3a),
            "items": [
                {
                    "id": r.order.id,
                    "shop": r.order.shop,
                    "phone": r.order.phone,
                    "bucket": r.bucket,
                    "action": r.action,
                    "returned_count": r.returned_count,
                    "refs": r.reference_ids,
                    "applied": r.applied_status,
                    "reason": r.reason,
                }
                for r in step3a_results
            ],
        },
        "step3b": {
            "total_new": total_3b,
            "counts": dict(c3b),
            "items": [
                {
                    "id": r.order.id,
                    "shop": r.order.shop,
                    "phone": r.order.phone,
                    "bucket": r.bucket,
                    "action": r.action,
                    "applied": r.applied_status,
                    "reason": r.reason,
                }
                for r in step3b_results
            ],
        },
        "apply_log": list(apply_log),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return xlsx_path, json_path


def write_gip287_reports(
    run_ts: datetime,
    mode: str,
    elapsed_sec: float,
    phase1_total_new: int,
    phase1_results: Sequence[ClassificationResult],
    phase1_already_tagged: Sequence[OrderRecord],
    phase2_total_new: int,
    phase2_results: Sequence[ClassificationResult],
    peer_cancel_results: Sequence[ClassificationResult],
    apply_log: Sequence[str],
    output_dir: Path = REPORTS_DIR,
) -> Tuple[Path, Path]:
    """Write Excel (.xlsx) and JSON (.json) reports for the GIP 287 pipeline."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_ts.strftime("%Y%m%d_%H%M%S")
    xlsx_path = output_dir / f"report_gip287_{stamp}.xlsx"
    json_path = output_dir / f"report_gip287_{stamp}.json"

    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    c1 = Counter(r.bucket for r in phase1_results)
    c2 = Counter(r.bucket for r in phase2_results)
    ambiguous_all = [
        r for r in (*phase1_results, *phase2_results, *peer_cancel_results) if r.bucket == "AMBIGUOUS"
    ]

    sum_rows: List[List[Any]] = [
        ["Thời gian chạy", run_ts.strftime("%Y-%m-%d %H:%M:%S %Z")],
        ["Luồng xử lý", "GIP 287 (WeUpBook)"],
        ["Chế độ", mode],
        ["Thời lượng xử lý (giây)", round(elapsed_sec, 2)],
        ["", ""],
        ["Pha 1 — Tổng đơn New trên /admin/order (GIP 287)", phase1_total_new],
        ["Pha 1 — Gắn tag NEED PAY ADVANCED (>= 60% & Returned > 3)", c1.get("NEED PAY ADVANCED", 0)],
        ["Pha 1 — Đã gắn NEED PAY ADVANCED sẵn", len(phase1_already_tagged)],
        ["Pha 1 — Ca biên (AMBIGUOUS)", c1.get("AMBIGUOUS", 0)],
        ["", ""],
        ["Pha 2 — Tổng đơn New Duplicated Phone (GIP 287)", phase2_total_new],
        ["  • Hủy: TEST", c2.get("TEST", 0)],
        ["  • Hủy: DUPLICATE (đơn New)", c2.get("DUPLICATE", 0)],
        ["  • Hủy: DUPLICATE (đơn nhỏ Pending từ lịch sử)", len(peer_cancel_results)],
        ["  • Gắn tag: MULTIPLE ORDERS", c2.get("MULTIPLE ORDERS", 0)],
        ["  • Gắn tag: DIFFERENT PRODUCT", c2.get("DIFFERENT PRODUCT", 0)],
        ["  • Giữ nguyên (NO_ACTION)", c2.get("NO_ACTION", 0)],
        ["  • Ca biên cần duyệt (AMBIGUOUS)", c2.get("AMBIGUOUS", 0)],
    ]
    if apply_log:
        sum_rows.append(["", ""])
        for idx, entry in enumerate(apply_log, start=1):
            sum_rows.append([f"Apply Log #{idx}", entry])

    _style_sheet(ws_sum, ["Hạng mục", "Kết quả"], sum_rows)

    ws_p1 = wb.create_sheet("Phase1_NeedPayAdvanced")
    p1_rows = [
        [
            r.order.id,
            r.order.shop,
            f"{r.order.return_rate:.0f}%" if r.order.return_rate is not None else "",
            r.order.return_rate_success if r.order.return_rate_success is not None else "",
            r.order.return_rate_returned if r.order.return_rate_returned is not None else "",
            r.order.phone,
            r.order.internal_note,
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for r in phase1_results
    ] + [
        [
            o.id,
            o.shop,
            f"{o.return_rate:.0f}%" if o.return_rate is not None else "",
            o.return_rate_success if o.return_rate_success is not None else "",
            o.return_rate_returned if o.return_rate_returned is not None else "",
            o.phone,
            o.internal_note,
            "ALREADY_TAGGED",
            "SKIP",
            "SKIPPED",
            "Đã có tag NEED PAY ADVANCED sẵn",
        ]
        for o in phase1_already_tagged
    ]
    _style_sheet(
        ws_p1,
        ["ID", "Shop", "Return Rate", "Success", "Returned", "Phone", "Internal Note", "Phân loại", "Hành động", "Trạng thái Apply", "Lý do"],
        p1_rows,
        [_classification_fill(r) for r in phase1_results] + [None for _ in phase1_already_tagged],
    )

    ws_p2 = wb.create_sheet("Phase2_Duplicated")
    p2_rows = [
        [
            idx,
            r.order.id,
            r.order.phone,
            r.order.customer_name,
            r.order.internal_note,
            r.order.detail_sku_code,
            ", ".join(r.reference_ids),
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for idx, r in enumerate(phase2_results, start=1)
    ]
    _style_sheet(
        ws_p2,
        ["STT", "ID", "Phone", "Khách hàng", "Internal Note", "Detail Code", "Đơn tham chiếu", "Phân loại", "Hành động", "Trạng thái Apply", "Lý do"],
        p2_rows,
        [_classification_fill(r) for r in phase2_results],
    )

    ws_peer = wb.create_sheet("Phase2_PeerCancelSmall")
    peer_rows = [
        [
            idx,
            r.order.id,
            r.order.status,
            r.order.phone,
            r.order.internal_note,
            ", ".join(r.reference_ids),
            r.bucket,
            r.action,
            r.applied_status,
            r.reason,
        ]
        for idx, r in enumerate(peer_cancel_results, start=1)
    ]
    _style_sheet(
        ws_peer,
        ["STT", "ID đơn nhỏ", "Trạng thái cũ", "Phone", "Internal Note", "Đơn lớn giữ lại", "Phân loại", "Hành động", "Trạng thái Apply", "Lý do"],
        peer_rows,
        [_classification_fill(r) for r in peer_cancel_results],
    )

    ws_amb = wb.create_sheet("Ambiguous_Review")
    amb_rows = [
        [
            r.order.id,
            r.order.shop,
            r.order.status,
            r.order.phone,
            r.order.internal_note,
            r.order.detail_sku_code,
            ", ".join(r.reference_ids),
            r.reason,
        ]
        for r in ambiguous_all
    ]
    _style_sheet(
        ws_amb,
        ["ID", "Shop", "Status", "Phone", "Internal Note", "Detail Code", "Đơn tham chiếu", "Lý do cần duyệt"],
        amb_rows,
        [_REVIEW_FILL for _ in amb_rows],
    )

    wb.save(xlsx_path)

    payload = {
        "timestamp": run_ts.isoformat(),
        "pipeline": "gip287",
        "mode": mode,
        "elapsed_seconds": round(elapsed_sec, 2),
        "phase1": {
            "total_new": phase1_total_new,
            "counts": dict(c1),
            "already_tagged_count": len(phase1_already_tagged),
            "items": [
                {
                    "id": r.order.id,
                    "phone": r.order.phone,
                    "return_rate": r.order.return_rate,
                    "success": r.order.return_rate_success,
                    "returned": r.order.return_rate_returned,
                    "bucket": r.bucket,
                    "action": r.action,
                    "applied": r.applied_status,
                    "reason": r.reason,
                }
                for r in phase1_results
            ],
        },
        "phase2": {
            "total_new": phase2_total_new,
            "counts": dict(c2),
            "items": [
                {
                    "id": r.order.id,
                    "phone": r.order.phone,
                    "bucket": r.bucket,
                    "action": r.action,
                    "tag": r.tag,
                    "refs": r.reference_ids,
                    "applied": r.applied_status,
                    "reason": r.reason,
                }
                for r in phase2_results
            ],
            "peer_cancel_small": [
                {
                    "id": r.order.id,
                    "status": r.order.status,
                    "phone": r.order.phone,
                    "bucket": r.bucket,
                    "action": r.action,
                    "refs": r.reference_ids,
                    "applied": r.applied_status,
                    "reason": r.reason,
                }
                for r in peer_cancel_results
            ],
        },
        "apply_log": list(apply_log),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return xlsx_path, json_path


def send_telegram_summary(text: str) -> bool:
    """Send a concise Telegram message if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOGGER.info("Bỏ qua Telegram vì chưa cấu hình TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    if resp.status_code != 200:
        LOGGER.warning("Gửi Telegram thất bại (%s): %s", resp.status_code, resp.text[:200])
        return False
    return True
