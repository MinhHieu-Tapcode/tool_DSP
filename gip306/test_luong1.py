"""Unit tests for GIP 306 Luong 1 (Excel parse + update-before-Telegram ordering guard)."""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from gip306.run_luong1 import execute_luong1
from gip306.sheets_sync import parse_pos_export_xlsx

TZ_VN = timezone(timedelta(hours=7))


def _make_pos_xlsx(path: Path, headers: list, data_rows: list) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "sheet"
    ws.append(headers)
    for row in data_rows:
        ws.append(row)
    wb.save(path)
    wb.close()


class Gip306Luong1Tests(unittest.TestCase):
    def test_parse_pos_export_xlsx_validates_headers_and_preserves_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ok.xlsx"
            _make_pos_xlsx(
                p,
                ["Số điện thoại", "Sản phẩm", "Mã sản phẩm"],
                [
                    [9162577549.0, "52+ Quick Math Tricks", "PL-11TNTH"],
                    [None, "52 QUICK MATH TRICKS Workbook", "PL-11BTNTH"],
                    ["+639456638528", "Logical Math", "GPH306-00037"],
                    [None, None, None],
                ],
            )
            headers, rows = parse_pos_export_xlsx(p)
            self.assertEqual(headers, ["Số điện thoại", "Sản phẩm", "Mã sản phẩm"])
            self.assertEqual(
                rows,
                [
                    ["9162577549", "52+ Quick Math Tricks", "PL-11TNTH"],
                    ["", "52 QUICK MATH TRICKS Workbook", "PL-11BTNTH"],
                    ["+639456638528", "Logical Math", "GPH306-00037"],
                ],
            )

            bad = Path(tmp) / "bad.xlsx"
            _make_pos_xlsx(bad, ["Order ID", "Phone", "Note"], [["1", "091", "x"]])
            with self.assertRaises(ValueError):
                parse_pos_export_xlsx(bad)

    def test_sheet_sync_runs_before_telegram_and_aborts_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pos.xlsx"
            _make_pos_xlsx(
                p,
                ["Số điện thoại", "Sản phẩm", "Mã sản phẩm"],
                [["09162577549", "52+ Quick Math Tricks", "PL-11TNTH"]],
            )
            logger = logging.getLogger("test_gip306")
            run_ts = datetime(2026, 11, 29, 8, 0, 0, tzinfo=TZ_VN)

            # Case 1: sheet sync fails -> MUST raise and NEVER call send_and_arm_reminder
            with mock.patch("gip306.run_luong1.sync_rows_to_sheet", side_effect=RuntimeError("Sheet down")), \
                 mock.patch("gip306.run_luong1.send_and_arm_reminder") as mock_tg:
                with self.assertRaises(RuntimeError):
                    execute_luong1(
                        apply_mode=True,
                        notify_telegram=True,
                        xlsx_override=p,
                        backend="apps_script",
                        headed=False,
                        run_ts=run_ts,
                        logger=logger,
                    )
                mock_tg.assert_not_called()

            # Case 2: happy path -> sync runs strictly BEFORE send_and_arm_reminder
            call_order = []

            def fake_sync(rows, **kwargs):
                call_order.append(("sync", len(rows)))
                return len(rows)

            def fake_tg(*args, **kwargs):
                call_order.append(("telegram", args[0]))
                return {"ok": True, "message_id": 123}

            with mock.patch("gip306.run_luong1.sync_rows_to_sheet", side_effect=fake_sync), \
                 mock.patch("gip306.run_luong1.send_and_arm_reminder", side_effect=fake_tg):
                rc = execute_luong1(
                    apply_mode=True,
                    notify_telegram=True,
                    xlsx_override=p,
                    backend="apps_script",
                    headed=False,
                    run_ts=run_ts,
                    logger=logger,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(call_order, [("sync", 1), ("telegram", "gip306_update_link")])

            # Case 3: 0 rows -> skips sheet sync and skips Telegram
            empty_p = Path(tmp) / "empty.xlsx"
            _make_pos_xlsx(empty_p, ["Số điện thoại", "Sản phẩm", "Mã sản phẩm"], [])
            with mock.patch("gip306.run_luong1.sync_rows_to_sheet") as mock_sync, \
                 mock.patch("gip306.run_luong1.send_and_arm_reminder") as mock_tg:
                rc_empty = execute_luong1(
                    apply_mode=True,
                    notify_telegram=True,
                    xlsx_override=empty_p,
                    backend="apps_script",
                    headed=False,
                    run_ts=run_ts,
                    logger=logger,
                )
            self.assertEqual(rc_empty, 0)
            mock_sync.assert_not_called()
            mock_tg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
