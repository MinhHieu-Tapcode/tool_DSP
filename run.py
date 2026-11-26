"""CLI entry point for the G-solution automated duplicate-check & assignment tool."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from attendance import get_attendance, parse_target_date, TZ_VN
from browser_client import GSolutionClient
from classifier import (
    ClassificationResult,
    OrderRecord,
    classify_gip287_phase1_orders,
    classify_gip287_phase1_test_orders,
    classify_gip287_phase2_orders,
    classify_step2_orders,
    classify_step3a_orders,
    classify_step3b_orders,
    extract_gip287_items,
    is_empty_order_fields,
    is_order_younger_than,
    is_within_days,
    normalize_phone,
    parse_created_at,
)
from config import (
    CANCEL_REASON_PATTERNS,
    DEFERRED_FILL_GIPS,
    EMPTY_ORDER_COOLDOWN_MINUTES,
    GIP287_DUPLICATE_WINDOW_DAYS,
    GIP287_GIP_KEY,
    LOGS_DIR,
    TAG_DIFFERENT_PRODUCT,
    TAG_DUPLICATE,
    TAG_MULTIPLE_ORDERS,
    TAG_MULTIPLE_RETURNED,
    TAG_NEED_PAY_ADVANCED,
    TAG_RE_ORDER,
    TAG_TEST,
    ensure_runtime_dirs,
    load_dotenv,
)
from reporter import send_telegram_summary, write_gip287_reports, write_reports


def _setup_logging(run_ts: datetime, mode: str = "fillin") -> logging.Logger:
    ensure_runtime_dirs()
    prefix = "run_gip287" if mode == "gip287" else "run"
    log_path = LOGS_DIR / f"{prefix}_{run_ts.strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger("gsolution_auto")
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


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tự động hóa quy trình check trùng & chia data G-solution (chạy ngầm, 0 token)."
    )
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ đọc dữ liệu & xuất báo cáo Excel/JSON (mặc định, an toàn tuyệt đối).",
    )
    mode_grp.add_argument(
        "--apply",
        action="store_true",
        help="Thực thi thật trên G-solution (chuyển CALL, gắn thẻ, hủy đơn, chia sale).",
    )
    parser.add_argument(
        "--mode",
        choices=["fillin", "gip287"],
        default="fillin",
        help="Chọn luồng xử lý: fillin (mặc định: FILL IN DATA) hoặc gip287 (GIP 287 WeUpBook).",
    )
    parser.add_argument(
        "--sales",
        type=str,
        default=None,
        help="Ghi đè danh sách sale nhận đơn (vd: --sales Frescadorado hoặc --sales Frescadorado,angie0987654321.gip@gmail.com).",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Ngày kiểm tra lịch trực theo định dạng DD/MM/YYYY (mặc định: hôm nay UTC+7).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Số tab trình duyệt chạy song song khi tra lịch sử SĐT (mặc định: 4).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Mở cửa sổ trình duyệt để quan sát trực tiếp (mặc định: chạy ngầm headless).",
    )
    parser.add_argument(
        "--skip-step2",
        action="store_true",
        help="Bỏ qua Bước 2 (check Return Rate >= 50%% ở 3 GIP 85/319/244).",
    )
    parser.add_argument(
        "--max-phones",
        type=int,
        default=None,
        help="Giới hạn số SĐT tra cứu trong chế độ --dry-run (dùng để test nhanh).",
    )
    parser.add_argument(
        "--no-cancel",
        action="store_true",
        help="Dành cho --mode gip287: chỉ gắn tag (TEST / DUPLICATE / DIFFERENT PRODUCT / MULTIPLE ORDERS / NEED PAY ADVANCED), không hủy đơn.",
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="Gửi tóm tắt kết quả qua Telegram nếu đã cấu hình trong .env.",
    )
    return parser.parse_args(argv)


async def _run_pipeline(args: argparse.Namespace, run_ts: datetime, logger: logging.Logger) -> int:
    apply_mode = bool(args.apply)
    if apply_mode and args.max_phones is not None:
        raise RuntimeError("Không được dùng --max-phones cùng với --apply.")

    target_date = parse_target_date(args.date)
    logger.info("=== BẮT ĐẦU (%s) | Ngày %s ===", "APPLY" if apply_mode else "DRY-RUN", target_date.strftime("%d/%m/%Y"))

    # Step 1: Attendance
    attendance = get_attendance(target_date=target_date, sales_override=args.sales)
    for st in attendance.staff_statuses:
        logger.info(
            "Bước 1 — %s (%s -> %s): %s [%s]",
            st.name,
            st.code,
            st.account,
            st.raw_value,
            "ĐI LÀM" if st.is_working else "NGHI",
        )
    logger.info("Danh sách nhận đơn Bước 5: %s", attendance.active_accounts or "(Không có ai)")

    apply_log: List[str] = []
    step2_total_new = 0
    step2_results: List[ClassificationResult] = []
    step2_already_call: List[OrderRecord] = []

    async with GSolutionClient(headless=not args.headed, concurrency=args.concurrency) as client:
        await client.ensure_logged_in()

        # Step 2: 3 GIPs Return Rate >= 50%
        step2_converted_ids = set()
        if not args.skip_step2:
            logger.info("Bước 2 — Đang quét 3 GIP (85, 319, 244) trên /admin/order...")
            step2_total_new, step2_rows = await client.scan_step2_orders()
            step2_results, step2_already_call = classify_step2_orders(step2_rows)
            step2_converted_ids = {r.order.id for r in step2_results}
            logger.info(
                "Bước 2 — Tổng New: %d | FILL_IN >=50%% cần chuyển CALL: %d | Đã CALL sẵn: %d",
                step2_total_new,
                len(step2_results),
                len(step2_already_call),
            )
            missing_call_tag_ids = [
                o.id for o in step2_already_call if not any("CALL TO CF" in t.upper() for t in o.tags)
            ]
            if apply_mode and (step2_results or missing_call_tag_ids):
                checked, skipped, msg = await client.apply_step2_convert_to_call(
                    [r.order.id for r in step2_results],
                    tag_only_ids=missing_call_tag_ids,
                )
                checked_set = set(checked)
                for r in step2_results:
                    r.applied_status = "APPLIED" if r.order.id in checked_set else "SKIPPED"
                apply_log.append(
                    f"Step2 CALL ({len(checked)} applied, {len(missing_call_tag_ids)} tag-only, {len(skipped)} skipped): {msg}"
                )
                logger.info(
                    "Bước 2 Apply: %d thành công (gắn bù tag CALL TO CF: %d), %d bỏ qua",
                    len(checked),
                    len(missing_call_tag_ids),
                    len(skipped),
                )
            elif not apply_mode:
                for r in step2_results:
                    r.applied_status = "DRY_RUN"

        # Step 3: Scrape Telesale FILL IN DATA lists
        logger.info("Bước 3 — Đang quét màn Telesale (Saved Filter: FILL IN DATA)...")
        total_3a, raw_3a, total_3b, raw_3b = await client.scan_telesale_lists()
        fillin_3a = [o for o in raw_3a if o.id not in step2_converted_ids and not o.assigning_sale.strip()]
        fillin_3b = [o for o in raw_3b if o.id not in step2_converted_ids and not o.assigning_sale.strip()]
        logger.info(
            "Bước 3 — Duplicated Phone: %d (FILL_IN=%d) | Not Duplicated Phone: %d (FILL_IN=%d)",
            total_3a,
            len(fillin_3a),
            total_3b,
            len(fillin_3b),
        )

        # Optional: limit phones in --dry-run smoke test
        if args.max_phones is not None:
            keep_phones = []
            for o in fillin_3a:
                np = o.normalized_phone
                if np and np not in keep_phones:
                    keep_phones.append(np)
                if len(keep_phones) >= args.max_phones:
                    break
            keep_set = set(keep_phones)
            fillin_3a = [o for o in fillin_3a if o.normalized_phone in keep_set]
            logger.info("--max-phones=%d: giới hạn quét lịch sử còn %d đơn / %d SĐT", args.max_phones, len(fillin_3a), len(keep_set))

        # Inspect detail modal for empty orders (skip deferred-fill GIPs 244/300 & orders < cooldown)
        for o in (*fillin_3a, *fillin_3b):
            if is_empty_order_fields(o):
                if o.gip_key in DEFERRED_FILL_GIPS:
                    logger.info(
                        "Đơn trống %s thuộc %s (fill in dữ liệu sau) -> bỏ qua kiểm tra hủy đơn trống.",
                        o.id,
                        o.gip_key,
                    )
                    continue
                if is_order_younger_than(o, run_ts, EMPTY_ORDER_COOLDOWN_MINUTES):
                    logger.info(
                        "Đơn trống %s mới tạo (< %d phút) -> tạm chờ đồng bộ dữ liệu.",
                        o.id,
                        EMPTY_ORDER_COOLDOWN_MINUTES,
                    )
                    continue
                logger.info("Kiểm tra chi tiết đơn trống %s...", o.id)
                o.empty_detail_confirmed = await client.inspect_empty_order_detail(o.id)
                logger.info("Đơn %s empty_detail_confirmed = %s", o.id, o.empty_detail_confirmed)

        # Concurrent phone history lookup
        unique_phones = []
        seen_norms = set()
        for o in fillin_3a:
            np = o.normalized_phone
            if np and np not in seen_norms:
                seen_norms.add(np)
                unique_phones.append(o.phone)

        logger.info(
            "Bước 3A — Đang tra cứu lịch sử %d SĐT duy nhất (%d tab song song)...",
            len(unique_phones),
            args.concurrency,
        )
        histories = await client.lookup_phones_concurrent(unique_phones)

        # Classify 3A & 3B
        step3a_results = classify_step3a_orders(fillin_3a, histories, run_ts)
        step3b_results = classify_step3b_orders(fillin_3b, histories, ref_date=run_ts)

        if not apply_mode:
            for r in (*step3a_results, *step3b_results):
                r.applied_status = "DRY_RUN"
        else:
            # Execute Step 4 & 5 mutations safely (skip AMBIGUOUS)
            all_results = [*step3a_results, *step3b_results]
            by_id: Dict[str, ClassificationResult] = {r.order.id: r for r in all_results}

            # 1) Cancel buckets: TEST, DUPLICATE, MULTIPLE RETURNED
            for tag_name in (TAG_TEST, TAG_DUPLICATE, TAG_MULTIPLE_RETURNED):
                target_ids = [r.order.id for r in all_results if r.action == "CANCEL" and r.bucket == tag_name]
                if not target_ids:
                    continue
                logger.info("Pha thực thi — Gắn thẻ & Hủy nhóm %s (%d đơn)...", tag_name, len(target_ids))
                checked, skipped, msg = await client.apply_bulk_tag_and_cancel(
                    target_ids, tag_name, CANCEL_REASON_PATTERNS[tag_name]
                )
                checked_set = set(checked)
                for oid in target_ids:
                    by_id[oid].applied_status = "APPLIED" if oid in checked_set else "SKIPPED"
                apply_log.append(f"Cancel {tag_name} ({len(checked)}/{len(target_ids)}, skipped={len(skipped)}): {msg}")

            # 2) Tag-only buckets: DIFFERENT PRODUCT, RE ORDER (skip if already tagged)
            for tag_name in (TAG_DIFFERENT_PRODUCT, TAG_RE_ORDER):
                target_ids = [
                    r.order.id
                    for r in step3a_results
                    if r.action == "ASSIGN"
                    and r.bucket == tag_name
                    and tag_name.upper() not in {t.upper() for t in r.order.tags}
                ]
                if not target_ids:
                    continue
                logger.info("Pha thực thi — Gắn thẻ nhóm %s (%d đơn)...", tag_name, len(target_ids))
                checked, skipped, msg = await client.apply_bulk_tag_only(target_ids, tag_name)
                apply_log.append(f"Tag {tag_name} ({len(checked)}/{len(target_ids)}, skipped={len(skipped)}): {msg}")

            # 3) Step 5: Assign sale (chunked <= 80 per bulk modal for 100% completion stability)
            if attendance.active_accounts:
                assign_chunk_size = 80
                batch_5a = [r.order.id for r in step3a_results if r.action == "ASSIGN"]
                if batch_5a:
                    checked_all: List[str] = []
                    skipped_all: List[str] = []
                    msgs: List[str] = []
                    for i in range(0, len(batch_5a), assign_chunk_size):
                        chunk = batch_5a[i : i + assign_chunk_size]
                        logger.info(
                            "Bước 5A — Chia %d đơn 3A (lô %d-%d/%d) cho %s...",
                            len(chunk),
                            i + 1,
                            i + len(chunk),
                            len(batch_5a),
                            attendance.active_accounts,
                        )
                        checked, skipped, msg = await client.apply_bulk_assign_sale(chunk, attendance.active_accounts)
                        checked_all.extend(checked)
                        skipped_all.extend(skipped)
                        msgs.append(msg)
                    checked_set = set(checked_all)
                    for oid in batch_5a:
                        by_id[oid].applied_status = "APPLIED" if oid in checked_set else "SKIPPED"
                    apply_log.append(
                        f"Assign 5A ({len(checked_all)}/{len(batch_5a)}, skipped={len(skipped_all)}): {' || '.join(msgs)}"
                    )

                batch_5b = [r.order.id for r in step3b_results if r.action == "ASSIGN"]
                if batch_5b:
                    checked_all = []
                    skipped_all = []
                    msgs = []
                    for i in range(0, len(batch_5b), assign_chunk_size):
                        chunk = batch_5b[i : i + assign_chunk_size]
                        logger.info(
                            "Bước 5B — Chia %d đơn 3B (lô %d-%d/%d) cho %s...",
                            len(chunk),
                            i + 1,
                            i + len(chunk),
                            len(batch_5b),
                            attendance.active_accounts,
                        )
                        checked, skipped, msg = await client.apply_bulk_assign_sale(chunk, attendance.active_accounts)
                        checked_all.extend(checked)
                        skipped_all.extend(skipped)
                        msgs.append(msg)
                    checked_set = set(checked_all)
                    for oid in batch_5b:
                        by_id[oid].applied_status = "APPLIED" if oid in checked_set else "SKIPPED"
                    apply_log.append(
                        f"Assign 5B ({len(checked_all)}/{len(batch_5b)}, skipped={len(skipped_all)}): {' || '.join(msgs)}"
                    )
            else:
                logger.warning("Không có nhân sự đi làm hôm nay -> Bỏ qua Bước 5 Assign sale.")
                for r in all_results:
                    if r.action == "ASSIGN":
                        r.applied_status = "SKIPPED"

            for r in all_results:
                if r.action == "REVIEW":
                    r.applied_status = "SKIPPED"

    elapsed = time.perf_counter() - getattr(args, "_start_time", time.perf_counter())
    xlsx_path, json_path = write_reports(
        run_ts=run_ts,
        mode="APPLY" if apply_mode else "DRY-RUN",
        elapsed_sec=elapsed,
        attendance=attendance,
        step2_total_new=step2_total_new,
        step2_results=step2_results,
        step2_already_call=step2_already_call,
        total_3a=total_3a,
        step3a_results=step3a_results,
        total_3b=total_3b,
        step3b_results=step3b_results,
        apply_log=apply_log,
    )

    c3a = Counter(r.bucket for r in step3a_results)
    c3b = Counter(r.bucket for r in step3b_results)
    summary_lines = [
        f"=== HOÀN TẤT ({'APPLY' if apply_mode else 'DRY-RUN'}) trong {elapsed:.1f}s ===",
        f"Lịch trực {attendance.date_header} ({attendance.source}): {', '.join(attendance.active_accounts) or 'Không có'}",
        f"Bước 2 (3 GIP): {len(step2_results)} cần CALL (đã CALL: {len(step2_already_call)})",
        (
            f"Bước 3A ({len(step3a_results)} đơn FILL_IN): "
            f"TEST={c3a.get('TEST', 0)}, DUPLICATE={c3a.get('DUPLICATE', 0)}, "
            f"MULTIPLE RETURNED={c3a.get('MULTIPLE RETURNED', 0)}, "
            f"DIFFERENT PRODUCT={c3a.get('DIFFERENT PRODUCT', 0)}, "
            f"RE ORDER={c3a.get('RE ORDER', 0)}, CA BIÊN={c3a.get('AMBIGUOUS', 0)}"
        ),
        (
            f"Bước 3B ({len(step3b_results)} đơn FILL_IN): "
            f"TEST={c3b.get('TEST', 0)}, MULTIPLE RETURNED={c3b.get('MULTIPLE RETURNED', 0)}, "
            f"Hợp lệ chia={c3b.get('ASSIGN_ONLY', 0)}, CA BIÊN={c3b.get('AMBIGUOUS', 0)}"
        ),
        f"File Excel: {xlsx_path}",
        f"File JSON : {json_path}",
    ]
    for line in summary_lines:
        logger.info(line)

    if args.notify_telegram:
        send_telegram_summary("\n".join(summary_lines))

    return 0


async def _run_gip287_pipeline(args: argparse.Namespace, run_ts: datetime, logger: logging.Logger) -> int:
    apply_mode = bool(args.apply)
    if apply_mode and args.max_phones is not None:
        raise RuntimeError("Không được dùng --max-phones cùng với --apply.")

    logger.info("=== BẮT ĐẦU GIP 287 (%s) | Ngày %s ===", "APPLY" if apply_mode else "DRY-RUN", run_ts.strftime("%d/%m/%Y"))
    apply_log: List[str] = []

    async with GSolutionClient(headless=not args.headed, concurrency=args.concurrency) as client:
        await client.ensure_logged_in()

        # Phase 1: /admin/order -> Return Rate >= 60% & Returned > 3 -> tag NEED PAY ADVANCED
        logger.info("GIP 287 Pha 1 — Quét tab New trên /admin/order để kiểm tra Return Rate (>= 60%% & Returned > 3)...")
        phase1_total_new, phase1_rows = await client.scan_gip287_phase1_orders()
        phase1_results, phase1_already_tagged = classify_gip287_phase1_orders(phase1_rows)
        p1_targets = [r for r in phase1_results if r.action == "TAG_ONLY"]
        logger.info(
            "GIP 287 Pha 1 — Tổng New: %d | Cần gắn %s: %d | Đã có tag sẵn: %d | Ca biên: %d",
            phase1_total_new,
            TAG_NEED_PAY_ADVANCED,
            len(p1_targets),
            len(phase1_already_tagged),
            sum(1 for r in phase1_results if r.action == "REVIEW"),
        )
        if apply_mode:
            if p1_targets:
                checked, skipped, msg = await client.apply_gip287_phase1_tag(
                    [r.order.id for r in p1_targets], TAG_NEED_PAY_ADVANCED
                )
                checked_set = set(checked)
                for r in p1_targets:
                    r.applied_status = "APPLIED" if r.order.id in checked_set else "SKIPPED"
                apply_log.append(f"GIP287 Pha 1 {TAG_NEED_PAY_ADVANCED} ({len(checked)}/{len(p1_targets)}, skipped={len(skipped)}): {msg}")
            for r in phase1_results:
                if r.action == "REVIEW":
                    r.applied_status = "SKIPPED"
        else:
            for r in phase1_results:
                r.applied_status = "DRY_RUN"

        # Catch TEST orders across ALL New GIP 287 orders (even Not Duplicated Phone like A558587)
        phase1_test_results = classify_gip287_phase1_test_orders(phase1_rows)
        if phase1_test_results:
            p1_test_targets = [r for r in phase1_test_results if r.action == "CANCEL" and r.bucket == TAG_TEST]
            logger.info(
                "GIP 287 Pha 1 — Phát hiện %d đơn TEST trên toàn bộ New (nghi ngờ=%d)",
                len(p1_test_targets),
                sum(1 for r in phase1_test_results if r.action == "REVIEW"),
            )
            if apply_mode:
                if args.no_cancel:
                    to_tag_test: List[str] = []
                    for r in p1_test_targets:
                        if TAG_TEST.upper() in {t.strip().upper() for t in r.order.tags}:
                            r.applied_status = "ALREADY_TAGGED"
                        else:
                            to_tag_test.append(r.order.id)
                    if to_tag_test:
                        checked, skipped, msg = await client.apply_gip287_phase1_tag(to_tag_test, TAG_TEST)
                        checked_set = set(checked)
                        for r in p1_test_targets:
                            if r.order.id in to_tag_test:
                                r.applied_status = "TAGGED_NO_CANCEL" if r.order.id in checked_set else "SKIPPED"
                        apply_log.append(
                            f"GIP287 Pha 1 TagOnly {TAG_TEST} ({len(checked)}/{len(to_tag_test)}, skipped={len(skipped)}): {msg}"
                        )
                elif p1_test_targets:
                    target_ids = [r.order.id for r in p1_test_targets]
                    checked, skipped, msg = await client.apply_gip287_phase1_cancel_test(target_ids)
                    checked_set = set(checked)
                    for r in p1_test_targets:
                        r.applied_status = "APPLIED" if r.order.id in checked_set else "SKIPPED"
                    apply_log.append(
                        f"GIP287 Pha 1 Cancel {TAG_TEST} ({len(checked)}/{len(target_ids)}, skipped={len(skipped)}): {msg}"
                    )
                for r in phase1_test_results:
                    if r.action == "REVIEW":
                        r.applied_status = "SKIPPED"
            else:
                for r in phase1_test_results:
                    r.applied_status = "DRY_RUN"
            phase1_results.extend(phase1_test_results)

        # Phase 2: /admin/order/telesale -> GIP 287 + Duplicated Phone (tab New)
        logger.info("GIP 287 Pha 2 — Quét màn Telesale (POS Shop: GIP 287 + Duplicated Phone, tab New)...")
        phase2_total_new, phase2_orders = await client.scan_gip287_telesale_dup_new()
        logger.info("GIP 287 Pha 2 — Số đơn New Duplicated Phone: %d", len(phase2_orders))

        if args.max_phones is not None:
            keep_phones = []
            for o in phase2_orders:
                np = o.normalized_phone
                if np and np not in keep_phones:
                    keep_phones.append(np)
                if len(keep_phones) >= args.max_phones:
                    break
            keep_set = set(keep_phones)
            phase2_orders = [o for o in phase2_orders if o.normalized_phone in keep_set]
            logger.info("--max-phones=%d: giới hạn còn %d đơn / %d SĐT", args.max_phones, len(phase2_orders), len(keep_set))

        unique_phones = []
        seen_norms = set()
        for o in phase2_orders:
            np = o.normalized_phone
            if np and np not in seen_norms:
                seen_norms.add(np)
                unique_phones.append(o.phone)

        logger.info("GIP 287 Pha 2 — Tra cứu lịch sử %d SĐT duy nhất (%d tab song song)...", len(unique_phones), args.concurrency)
        histories = await client.lookup_phones_concurrent(unique_phones)

        # Fallback read full SKU/Note from order detail modal if table snippet looks truncated/missing
        inspect_candidates: Dict[str, OrderRecord] = {}
        for o in phase2_orders:
            items_o, _, trunc_o = extract_gip287_items(o.internal_note, o.detail_sku_code)
            if not items_o or trunc_o:
                inspect_candidates[o.id] = o
            hist = histories.get(o.normalized_phone)
            if hist is None:
                continue
            for h in hist.orders:
                if h.id == o.id or h.gip_key != GIP287_GIP_KEY or h.status in {"Canceled", "Cancelled"}:
                    continue
                h_dt = parse_created_at(h.created_at, run_ts)
                if h_dt is not None and is_within_days(h_dt, run_ts, GIP287_DUPLICATE_WINDOW_DAYS):
                    items_h, _, trunc_h = extract_gip287_items(h.internal_note, h.detail_sku_code)
                    if not items_h or trunc_h:
                        inspect_candidates[h.id] = h

        if inspect_candidates:
            logger.info("GIP 287 Pha 2 — Mở chi tiết %d đơn bị cắt ngắn mã SP để đọc đủ combo...", len(inspect_candidates))
            for oid, rec in inspect_candidates.items():
                rec.detail_sku_code = await client.inspect_order_detail_sku(oid)

        phase2_results, peer_cancel_results = classify_gip287_phase2_orders(phase2_orders, histories, run_ts)

        if not apply_mode:
            for r in (*phase2_results, *peer_cancel_results):
                r.applied_status = "DRY_RUN"
        else:
            by_id: Dict[str, ClassificationResult] = {r.order.id: r for r in phase2_results}
            no_cancel = bool(args.no_cancel)
            if no_cancel:
                logger.info("GIP 287 Pha 2 Apply — CHẾ ĐỘ CHỈ GẮN TAG (--no-cancel): không hủy đơn TEST / DUPLICATE.")

            # 1) Handle TEST & DUPLICATE on New orders (cancel or tag-only if --no-cancel)
            for tag_name in (TAG_TEST, TAG_DUPLICATE):
                matching = [r for r in phase2_results if r.action == "CANCEL" and r.bucket == tag_name]
                if not matching:
                    continue
                if no_cancel:
                    to_tag: List[str] = []
                    for r in matching:
                        if tag_name.upper() in {t.upper() for t in r.order.tags}:
                            r.applied_status = "ALREADY_TAGGED"
                        else:
                            to_tag.append(r.order.id)
                    if not to_tag:
                        continue
                    logger.info("GIP 287 Pha 2 Apply (--no-cancel) — Chỉ gắn thẻ %s (%d đơn New)...", tag_name, len(to_tag))
                    checked, skipped, msg = await client.apply_gip287_tag_only(to_tag, tag_name)
                    checked_set = set(checked)
                    for oid in to_tag:
                        by_id[oid].applied_status = "TAGGED_NO_CANCEL" if oid in checked_set else "SKIPPED"
                    apply_log.append(f"GIP287 TagOnly {tag_name} ({len(checked)}/{len(to_tag)}, skipped={len(skipped)}): {msg}")
                else:
                    target_ids = [r.order.id for r in matching]
                    logger.info("GIP 287 Pha 2 Apply — Hủy nhóm %s (%d đơn New)...", tag_name, len(target_ids))
                    checked, skipped, msg = await client.apply_gip287_cancel_new(
                        target_ids, tag_name, CANCEL_REASON_PATTERNS[tag_name]
                    )
                    checked_set = set(checked)
                    for oid in target_ids:
                        by_id[oid].applied_status = "APPLIED" if oid in checked_set else "SKIPPED"
                    apply_log.append(f"GIP287 Cancel {tag_name} ({len(checked)}/{len(target_ids)}, skipped={len(skipped)}): {msg}")

            # 2) Handle smaller peer orders up to Pending (TH3 subset rule)
            for pr in peer_cancel_results:
                tag_name = pr.tag or TAG_DUPLICATE
                if no_cancel:
                    if tag_name.upper() in {t.upper() for t in pr.order.tags}:
                        pr.applied_status = "ALREADY_TAGGED"
                        continue
                    logger.info("GIP 287 Pha 2 Apply (--no-cancel) — Chỉ gắn thẻ %s cho đơn nhỏ %s (%s)...", tag_name, pr.order.id, pr.order.status)
                    checked, skipped, msg = await client.apply_gip287_tag_peer_order(pr.order.id, tag_name)
                    pr.applied_status = "TAGGED_NO_CANCEL" if pr.order.id in set(checked) else "SKIPPED"
                    apply_log.append(f"GIP287 TagOnly Peer {pr.order.id} ({pr.order.status}): {msg}")
                else:
                    logger.info("GIP 287 Pha 2 Apply — Hủy đơn nhỏ %s (%s)...", pr.order.id, pr.order.status)
                    checked, skipped, msg = await client.apply_gip287_cancel_peer_order(
                        pr.order.id, tag_name, pr.cancel_reason_pattern or r"^Duplicate"
                    )
                    pr.applied_status = "APPLIED" if pr.order.id in set(checked) else "SKIPPED"
                    apply_log.append(f"GIP287 Cancel Peer {pr.order.id} ({pr.order.status}): {msg}")

            # 3) Tag MULTIPLE ORDERS & DIFFERENT PRODUCT (no Assign sale)
            for tag_name in (TAG_MULTIPLE_ORDERS, TAG_DIFFERENT_PRODUCT):
                matching = [r for r in phase2_results if r.action == "TAG_ONLY" and r.bucket == tag_name]
                to_tag: List[str] = []
                for r in matching:
                    if tag_name.upper() in {t.upper() for t in r.order.tags}:
                        r.applied_status = "ALREADY_TAGGED"
                    else:
                        to_tag.append(r.order.id)
                if not to_tag:
                    continue
                logger.info("GIP 287 Pha 2 Apply — Gắn thẻ %s (%d đơn)...", tag_name, len(to_tag))
                checked, skipped, msg = await client.apply_gip287_tag_only(to_tag, tag_name)
                checked_set = set(checked)
                for oid in to_tag:
                    by_id[oid].applied_status = "APPLIED" if oid in checked_set else "SKIPPED"
                apply_log.append(f"GIP287 Tag {tag_name} ({len(checked)}/{len(to_tag)}, skipped={len(skipped)}): {msg}")

            for r in phase2_results:
                if r.action == "NO_ACTION":
                    r.applied_status = "NO_ACTION"
                elif r.action == "REVIEW":
                    r.applied_status = "SKIPPED"

    elapsed = time.perf_counter() - getattr(args, "_start_time", time.perf_counter())
    xlsx_path, json_path = write_gip287_reports(
        run_ts=run_ts,
        mode="APPLY" if apply_mode else "DRY-RUN",
        elapsed_sec=elapsed,
        phase1_total_new=phase1_total_new,
        phase1_results=phase1_results,
        phase1_already_tagged=phase1_already_tagged,
        phase2_total_new=phase2_total_new,
        phase2_results=phase2_results,
        peer_cancel_results=peer_cancel_results,
        apply_log=apply_log,
    )

    c1 = Counter(r.bucket for r in phase1_results)
    c2 = Counter(r.bucket for r in phase2_results)
    summary_lines = [
        f"=== HOÀN TẤT GIP 287 ({'APPLY' if apply_mode else 'DRY-RUN'}) trong {elapsed:.1f}s ===",
        (
            f"Pha 1 ({phase1_total_new} đơn New): "
            f"{TAG_NEED_PAY_ADVANCED}={c1.get(TAG_NEED_PAY_ADVANCED, 0)}, "
            f"TEST={c1.get(TAG_TEST, 0)}, "
            f"Đã gắn sẵn={len(phase1_already_tagged)}, CA BIÊN={c1.get('AMBIGUOUS', 0)}"
        ),
        (
            f"Pha 2 ({len(phase2_orders)}/{phase2_total_new} đơn Duplicated): "
            f"TEST={c2.get('TEST', 0)}, DUPLICATE(New)={c2.get('DUPLICATE', 0)}, "
            f"DUPLICATE(Pending)={len(peer_cancel_results)}, "
            f"MULTIPLE ORDERS={c2.get(TAG_MULTIPLE_ORDERS, 0)}, "
            f"DIFFERENT PRODUCT={c2.get(TAG_DIFFERENT_PRODUCT, 0)}, "
            f"NO_ACTION={c2.get('NO_ACTION', 0)}, CA BIÊN={c2.get('AMBIGUOUS', 0)}"
        ),
        f"File Excel: {xlsx_path}",
        f"File JSON : {json_path}",
    ]
    for line in summary_lines:
        logger.info(line)

    if args.notify_telegram:
        send_telegram_summary("\n".join(summary_lines))

    return 0


def _acquire_run_lock(mode: str = "fillin"):
    """Acquire non-blocking exclusive lock on run.lock (or run_gip287.lock) to avoid overlapping runs."""
    ensure_runtime_dirs()
    lock_name = "run_gip287.lock" if mode == "gip287" else "run.lock"
    lock_path = LOGS_DIR.parent / lock_name
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

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


def _release_run_lock(fh) -> None:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    args = _parse_args(argv)
    args._start_time = time.perf_counter()
    run_ts = datetime.now(TZ_VN)
    logger = _setup_logging(run_ts, mode=args.mode)
    lock_fh = _acquire_run_lock(mode=args.mode)
    if lock_fh is None:
        logger.info("Đã có một tiến trình gsolution-auto (%s) khác đang chạy; bỏ qua lượt này để tránh xung đột.", args.mode)
        return 0
    try:
        if args.mode == "gip287":
            return asyncio.run(_run_gip287_pipeline(args, run_ts, logger))
        return asyncio.run(_run_pipeline(args, run_ts, logger))
    except Exception as exc:
        logger.exception("Lỗi nghiêm trọng: %s", exc)
        return 1
    finally:
        _release_run_lock(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
