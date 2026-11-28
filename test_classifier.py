"""Unit tests for attendance parsing and Step 2 / Step 3A / Step 3B classification rules."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from attendance import parse_attendance_rows, resolve_sales_override
from classifier import (
    OrderRecord,
    PhoneHistory,
    classify_gip287_phase1_orders,
    classify_gip287_phase1_test_orders,
    classify_gip287_phase2_orders,
    classify_step2_orders,
    classify_step3a_orders,
    classify_step3b_orders,
    detect_test_text,
    extract_gip287_items,
    extract_gip_key,
    extract_product_keys,
    is_within_days,
    normalize_phone,
    parse_created_at,
    parse_return_rate_tooltip,
    products_overlap,
)

TZ_VN = timezone(timedelta(hours=7))
REF_DT = datetime(2026, 11, 24, 10, 30, 0, tzinfo=TZ_VN)


class AttendanceTests(unittest.TestCase):
    def test_parse_attendance_validates_month_year_and_reads_x_off(self) -> None:
        rows = [
            ["", "", "", "", "", "", "", "Month", "11", ""],
            ["", "", "", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", "Year", "2026", ""],
            ["", "", "", "", "", "", "", "23/11", "24/11", "25/11"],
            ["NO.", "CODE", "NAME", "MARKET", "FACILITY", "DEPARTMENT", "POSITION", "Mon", "Tue", "Wed"],
            ["1", "G238", "Fresca May Mariano Dorado", "GIP PH", "VP", "TLS", "STAFF", "X", "X", "OFF"],
            ["2", "G293", "Angelica Agarin Salimbao", "GIP PH", "VP", "TLS", "STAFF", "X", "OFF", "X"],
        ]
        res = parse_attendance_rows(rows, date(2026, 11, 24), "NOVEMBER 2026", "google_sheets_csv")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.active_accounts, ["Frescadorado"])

        # Wrong month fallback guard (e.g. gviz returned April 2025 when December requested)
        wrong = parse_attendance_rows(rows, date(2026, 12, 24), "DECEMBER 2026", "google_sheets_csv")
        self.assertIsNone(wrong)

    def test_sales_override_aliases(self) -> None:
        res = resolve_sales_override("Fresca, angelica", date(2026, 11, 24))
        self.assertEqual(
            res.active_accounts,
            ["Frescadorado", "angie0987654321.gip@gmail.com"],
        )


class ClassifierHelperTests(unittest.TestCase):
    def test_phone_normalization(self) -> None:
        self.assertEqual(normalize_phone("+639971433904"), "09971433904")
        self.assertEqual(normalize_phone("639971433904"), "09971433904")
        self.assertEqual(normalize_phone("9175348871"), "09175348871")
        self.assertEqual(normalize_phone("09175348871"), "09175348871")

    def test_parse_created_at_and_30_day_window(self) -> None:
        today_dt = parse_created_at("07:15:38 Today", REF_DT)
        self.assertEqual(today_dt.date(), date(2026, 11, 24))

        oct25_dt = parse_created_at("19:05:00 25/10", REF_DT)
        oct24_dt = parse_created_at("19:05:00 24/10", REF_DT)
        aug29_dt = parse_created_at("03:45:46 29/08", REF_DT)
        dec31_dt = parse_created_at("17:05:29 31/12/2025", REF_DT)

        self.assertTrue(is_within_days(oct25_dt, REF_DT, 30))
        self.assertFalse(is_within_days(oct24_dt, REF_DT, 30))
        self.assertFalse(is_within_days(aug29_dt, REF_DT, 30))
        self.assertEqual(dec31_dt.year, 2025)

    def test_test_keyword_avoids_false_positives(self) -> None:
        self.assertEqual(detect_test_text(["Latest edition jacket"])[0], "NONE")
        self.assertEqual(detect_test_text(["Greatest waterproof tarp"])[0], "NONE")
        self.assertEqual(detect_test_text(["Buy 1 <12V CAR BATTERY TESTER ALTERNATOR TOOL>"])[0], "NONE")
        self.assertEqual(detect_test_text(["testing order"])[0], "NONE")
        self.assertEqual(detect_test_text(["retest customer"])[0], "NONE")
        self.assertEqual(detect_test_text(["link: https://example.com/p?utm_campaign=MT04+TEST"])[0], "NONE")
        self.assertEqual(detect_test_text(["Test Buyer 123"])[0], "DEFINITE")
        self.assertEqual(detect_test_text(["test"])[0], "DEFINITE")

    def test_product_matching_across_variants(self) -> None:
        a = OrderRecord(
            id="YXXK008",
            shop="GIP 264 - Xuân Tú",
            internal_note="QTY: 2, < hair twist clip >|Buy 2\nBLUE",
        )
        b = OrderRecord(
            id="EEEV00D",
            shop="GIP 264 - Xuân Tú",
            internal_note="QTY: 2, < hair twist clip >|Buy 2\nRED",
            products="hair twist clip red x 2",
        )
        ov, _ = products_overlap(a, b)
        self.assertEqual(ov, "MATCH")

        c = OrderRecord(
            id="KMMGXWW",
            shop="GIP 264 - Xuân Tú",
            internal_note="QTY: 6, < Universal Water Pipe Threading Tools >",
            products="Universal Water Pipe Threading Tools x 1",
        )
        d = OrderRecord(
            id="R66R3PB",
            shop="GIP 264 - Xuân Tú",
            internal_note="QTY: 6, < Threaded pipe connector >",
        )
        ov2, _ = products_overlap(c, d)
        self.assertEqual(ov2, "DIFFERENT")
        self.assertEqual(extract_gip_key("715144184 - GIP 319 - HỒNG SƠN"), "GIP 319")
        self.assertEqual(extract_product_keys("1 GPH304-00024 | Buy 1", "")[1], {"GPH304-00024"})

        u1 = OrderRecord(
            id="0EEEG08",
            shop="GIP 319 - HỒNG SƠN",
            internal_note="link: https://www.activaate.com/solar-aircon?fbclid=abc IP: 1.2.3.4",
            products="",
        )
        u2 = OrderRecord(
            id="PRWGDRA",
            shop="GIP 319 - HỒNG SƠN",
            internal_note="link: https://www.activaate.com/solar-aircon?fbclid=def",
            products="Solar Aircon x 1",
        )
        self.assertEqual(products_overlap(u1, u2)[0], "MATCH")


class SopClassificationTests(unittest.TestCase):
    def test_step2_inclusive_50_percent(self) -> None:
        orders = [
            OrderRecord(id="A1", shop="GIP 244 - XUÂN VŨ", service_types="FILL_IN", return_rate=50.0),
            OrderRecord(id="A2", shop="GIP 244 - XUÂN VŨ", service_types="FILL_IN", return_rate=49.0),
            OrderRecord(id="A3", shop="GIP 244 - XUÂN VŨ", service_types="CALL", return_rate=80.0),
        ]
        conv, already = classify_step2_orders(orders)
        self.assertEqual([c.order.id for c in conv], ["A1"])
        self.assertEqual([o.id for o in already], ["A3"])

    def test_priority_test_over_duplicate_and_multiple_returned(self) -> None:
        o = OrderRecord(
            id="R66RJPE",
            shop="GIP 304 - QA TEAM",
            service_types="FILL_IN",
            phone="09173042468",
            internal_note="1 GPH304-00024",
            products="WALL-MOUNTED BELL x 1",
            created_at="07:00:30 Today",
            status="New",
        )
        hist = PhoneHistory(
            normalized_phone="09173042468",
            all_count=5,
            returned_count=4,
            orders=[
                o,
                OrderRecord(
                    id="OAEJ5DV",
                    shop="GIP 170",
                    tags=["TEST"],
                    created_at="20:31:39 02/11/2025",
                    status="Canceled",
                ),
            ],
        )
        res = classify_step3a_orders([o], {"09173042468": hist}, REF_DT)
        self.assertEqual(res[0].bucket, "TEST")
        self.assertEqual(res[0].action, "CANCEL")

    def test_priority_duplicate_over_multiple_returned(self) -> None:
        newer = OrderRecord(
            id="ZXXA873",
            shop="GIP 264 - Xuân Tú",
            service_types="FILL_IN",
            phone="09973620863",
            internal_note="QTY: 1, < Waterproof rain tarp >",
            created_at="23:57:03 Yesterday",
            status="New",
        )
        shipped = OrderRecord(
            id="EEVMARD",
            shop="GIP 264 - Xuân Tú",
            phone="09973620863",
            internal_note="QTY: 1, < Waterproof rain tarp >",
            products="Waterproof rain tarp x 1",
            created_at="12:38:19 21/11",
            status="Shipped",
        )
        hist = PhoneHistory(
            normalized_phone="09973620863",
            all_count=8,
            returned_count=3,
            orders=[newer, shipped],
        )
        res = classify_step3a_orders([newer], {"09973620863": hist}, REF_DT)
        self.assertEqual(res[0].bucket, "DUPLICATE")

    def test_newer_duplicate_canceled_and_older_new_becomes_reorder(self) -> None:
        newer = OrderRecord(
            id="N88JA97",
            shop="GIP 264 - Xuân Tú",
            phone="09958420462",
            internal_note="QTY: 2, < Double-ended faucet >",
            created_at="03:03:46 Today",
            status="New",
        )
        older = OrderRecord(
            id="EEEVW6E",
            shop="GIP 264 - Xuân Tú",
            phone="09958420462",
            internal_note="QTY: 2, < Double-ended faucet >",
            created_at="13:58:48 Yesterday",
            status="New",
        )
        returning = OrderRecord(
            id="0EL93BV",
            shop="GIP 264 - Xuân Tú",
            phone="09958420462",
            products="Double-ended faucet x 2",
            created_at="09:32:11 16/11",
            status="Returning",
        )
        hist = PhoneHistory(
            normalized_phone="09958420462",
            all_count=5,
            returned_count=2,
            tab_counts={"Returning": 1, "Returned": 2},
            orders=[newer, older, returning],
        )
        res = classify_step3a_orders([newer, older], {"09958420462": hist}, REF_DT)
        by_id = {r.order.id: r for r in res}
        self.assertEqual(by_id["N88JA97"].bucket, "DUPLICATE")
        self.assertEqual(by_id["EEEVW6E"].bucket, "RE ORDER")

    def test_pending_over_30_days_ignored_and_different_product_wins(self) -> None:
        o = OrderRecord(
            id="A55X9B7",
            shop="GIP 182 - TIẾN ĐỨC",
            phone="09164117255",
            internal_note="<AGED GARLIC>|Buy 2",
            products="AGED GARLIC x 1",
            created_at="02:45:46 Today",
            status="New",
        )
        shipped_diff = OrderRecord(
            id="R6R8DPP",
            shop="GIP 182 - TIẾN ĐỨC",
            phone="09164117255",
            products="Magnesium Glycinate x 2",
            created_at="07:04:25 21/11",
            status="Shipped",
        )
        old_pending_same = OrderRecord(
            id="ON3P58P",
            shop="GIP 182 - TIẾN ĐỨC",
            phone="09164117255",
            products="AGED GARLIC x 1",
            created_at="03:45:46 29/08",
            status="Pending",
        )
        delivered = OrderRecord(
            id="ONA59D7",
            shop="GIP 142",
            phone="09164117255",
            products="NEYGOLD 400g x 3",
            created_at="10:30:44 19/07",
            status="Delivered",
        )
        hist = PhoneHistory(
            normalized_phone="09164117255",
            all_count=4,
            returned_count=2,
            tab_counts={"Delivered": 1, "Returned": 2},
            orders=[o, shipped_diff, old_pending_same, delivered],
        )
        res = classify_step3a_orders([o], {"09164117255": hist}, REF_DT)
        self.assertEqual(res[0].bucket, "DIFFERENT PRODUCT")

    def test_canceled_only_history_gets_different_product(self) -> None:
        o = OrderRecord(
            id="R66R3K3",
            shop="GIP 244 - XUÂN VŨ",
            phone="09763081558",
            products="Nike White Size L x 1",
            created_at="15:41:21 Yesterday",
            status="New",
        )
        hist = PhoneHistory(
            normalized_phone="09763081558",
            all_count=3,
            returned_count=0,
            orders=[
                o,
                OrderRecord(id="C1", created_at="15:40:11 Yesterday", status="Canceled"),
                OrderRecord(id="C2", created_at="09:22:21 15/11", status="Canceled"),
            ],
        )
        res = classify_step3a_orders([o], {"09763081558": hist}, REF_DT)
        self.assertEqual(res[0].bucket, "DIFFERENT PRODUCT")

    def test_empty_order_confirmed_gets_duplicate(self) -> None:
        o = OrderRecord(
            id="JEELKMD",
            shop="GIP 264 - Xuân Tú",
            phone="09509872458",
            created_at="07:17:52 Today",
            status="New",
            empty_detail_confirmed=True,
        )
        hist = PhoneHistory(
            normalized_phone="09509872458",
            all_count=2,
            returned_count=0,
            orders=[
                o,
                OrderRecord(
                    id="PRWG9VZ",
                    shop="GIP 264 - Xuân Tú",
                    products="CK Boxer x 4",
                    created_at="11:19:44 21/11",
                    status="Shipped",
                ),
            ],
        )
        res = classify_step3a_orders([o], {"09509872458": hist}, REF_DT)
        self.assertEqual(res[0].bucket, "DUPLICATE")

    def test_step3b_cross_catches_test_and_multiple_returned(self) -> None:
        o_clean = OrderRecord(id="C1", customer_name="Maria Santos", phone="09111111111")
        o_test = OrderRecord(id="T1", customer_name="Test Order", phone="09222222222")
        o_mult = OrderRecord(id="YXXK5K8", customer_name="Purita", phone="09662151580")
        histories = {
            "09662151580": PhoneHistory(normalized_phone="09662151580", all_count=47, returned_count=13),
        }
        res = classify_step3b_orders([o_clean, o_test, o_mult], histories)
        self.assertEqual([r.bucket for r in res], ["ASSIGN_ONLY", "TEST", "MULTIPLE RETURNED"])

    def test_step3b_empty_order_gets_duplicate(self) -> None:
        o_empty = OrderRecord(
            id="EMPTY3B",
            shop="GIP 264 - Xuân Tú",
            customer_name="Juan",
            phone="09333333333",
            created_at="09:00:00 Today",
            empty_detail_confirmed=True,
        )
        res = classify_step3b_orders([o_empty], {}, ref_date=REF_DT)
        self.assertEqual(res[0].bucket, "DUPLICATE")
        self.assertEqual(res[0].action, "CANCEL")

    def test_deferred_gip_244_300_empty_in_3a_gets_different_product(self) -> None:
        o244 = OrderRecord(
            id="E244",
            shop="GIP 244 - XUÂN VŨ",
            phone="09111111111",
            created_at="10:28:00 Today",
            status="New",
            empty_detail_confirmed=True,
        )
        h244 = PhoneHistory(
            normalized_phone="09111111111",
            all_count=2,
            returned_count=1,
            orders=[
                o244,
                OrderRecord(
                    id="P244",
                    shop="GIP 244 - XUÂN VŨ",
                    products="Adidas White x 1",
                    created_at="10:00:00 Yesterday",
                    status="Shipped",
                ),
            ],
        )
        o300 = OrderRecord(
            id="E300",
            shop="GIP 300 - QUANG ĐỨC",
            phone="09222222222",
            created_at="10:28:00 Today",
            status="New",
        )
        h300 = PhoneHistory(
            normalized_phone="09222222222",
            all_count=2,
            returned_count=0,
            orders=[
                o300,
                OrderRecord(
                    id="P300",
                    shop="GIP 182 - TIẾN ĐỨC",
                    created_at="09:00:00 Yesterday",
                    status="Delivered",
                ),
            ],
        )
        o300_mult = OrderRecord(
            id="E300M",
            shop="GIP 300 - QUANG ĐỨC",
            phone="09333333333",
            created_at="10:28:00 Today",
            status="New",
        )
        h300_mult = PhoneHistory(
            normalized_phone="09333333333",
            all_count=4,
            returned_count=3,
            orders=[o300_mult],
        )
        res = classify_step3a_orders(
            [o244, o300, o300_mult],
            {"09111111111": h244, "09222222222": h300, "09333333333": h300_mult},
            REF_DT,
        )
        self.assertEqual([r.bucket for r in res], ["DIFFERENT PRODUCT", "DIFFERENT PRODUCT", "MULTIPLE RETURNED"])
        self.assertEqual([r.action for r in res], ["ASSIGN", "ASSIGN", "CANCEL"])

    def test_deferred_gip_244_300_empty_in_3b_gets_assigned_not_canceled(self) -> None:
        n888 = OrderRecord(
            id="N888VR0",
            shop="GIP 300 - QUANG ĐỨC",
            customer_name="Macmac",
            phone="09164311264",
            created_at="10:29:00 Today",
            empty_detail_confirmed=True,
        )
        e244 = OrderRecord(
            id="E244B",
            shop="GIP 244 - XUÂN VŨ",
            customer_name="Ana",
            phone="09164311265",
            created_at="09:00:00 Today",
        )
        res = classify_step3b_orders([n888, e244], {}, ref_date=REF_DT)
        self.assertEqual([r.bucket for r in res], ["ASSIGN_ONLY", "ASSIGN_ONLY"])
        self.assertEqual([r.action for r in res], ["ASSIGN", "ASSIGN"])

    def test_non_deferred_fresh_empty_order_waits_cooldown(self) -> None:
        fresh_3b = OrderRecord(
            id="FRESH3B",
            shop="GIP 264 - Xuân Tú",
            customer_name="Pedro",
            phone="09444444444",
            created_at="10:27:00 Today",
            empty_detail_confirmed=True,
        )
        old_3b = OrderRecord(
            id="OLD3B",
            shop="GIP 264 - Xuân Tú",
            customer_name="Pedro",
            phone="09555555555",
            created_at="10:00:00 Today",
            empty_detail_confirmed=True,
        )
        res = classify_step3b_orders([fresh_3b, old_3b], {}, ref_date=REF_DT)
        self.assertEqual([r.bucket for r in res], ["AMBIGUOUS", "DUPLICATE"])
        self.assertEqual([r.action for r in res], ["REVIEW", "CANCEL"])


class Gip287ClassificationTests(unittest.TestCase):
    def test_tooltip_and_item_extraction(self) -> None:
        self.assertEqual(parse_return_rate_tooltip("Success: 4 - Returned: 7"), (4, 7))
        self.assertEqual(parse_return_rate_tooltip("Success: 0 · Returned: 1"), (0, 1))
        self.assertEqual(parse_return_rate_tooltip("Success: 1 - Returned: 1"), (1, 1))
        self.assertEqual(parse_return_rate_tooltip(""), (None, None))

        items, exp, trunc = extract_gip287_items(
            "31.CB4.G354.E294.E339.E343|Combo 4: 1 Physical Book..."
        )
        self.assertEqual(items, {"G354", "E294", "E339", "E343"})
        self.assertEqual(exp, 4)
        self.assertFalse(trunc)

        items_t, exp_t, trunc_t = extract_gip287_items("33.CB6.G612.G523.E531.E558...")
        self.assertEqual(items_t, {"G612", "G523", "E531", "E558"})
        self.assertEqual(exp_t, 6)
        self.assertTrue(trunc_t)

        # Detail modal text resolves truncated internal note while keeping G vs E distinct
        items_full, _, trunc_full = extract_gip287_items(
            "33.CB6.G612.G523...",
            "33.CB6.G612.G523.E531.E558.E249.E292",
        )
        self.assertEqual(items_full, {"G612", "G523", "E531", "E558", "E249", "E292"})
        self.assertFalse(trunc_full)
        self.assertNotEqual(
            extract_gip287_items("31.CB2.G354.E294")[0],
            extract_gip287_items("31.CB2.E354.E294")[0],
        )
        items_k, exp_k, trunc_k = extract_gip287_items("31.CB7.G595.G576.E294.E339.E343.E565.K002|Combo 7")
        self.assertEqual(items_k, {"G595", "G576", "E294", "E339", "E343", "E565", "K002"})
        self.assertEqual(exp_k, 7)
        self.assertFalse(trunc_k)

    def test_phase1_need_pay_advanced_thresholds(self) -> None:
        orders = [
            OrderRecord(
                id="EEEZZLD",
                shop="GIP 287",
                service_types="DSP-CSR-TRK-ORD",
                return_rate=64.0,
                return_rate_success=4,
                return_rate_returned=7,
            ),
            OrderRecord(
                id="LOW_RET",
                shop="GIP 287",
                return_rate=75.0,
                return_rate_success=1,
                return_rate_returned=3,
            ),
            OrderRecord(
                id="LOW_PCT",
                shop="GIP 287",
                return_rate=50.0,
                return_rate_success=5,
                return_rate_returned=5,
            ),
            OrderRecord(
                id="ALREADY",
                shop="GIP 287",
                tags=["NEED PAY ADVANCED"],
                return_rate=67.0,
                return_rate_success=2,
                return_rate_returned=4,
            ),
            OrderRecord(
                id="NO_TIP",
                shop="GIP 287",
                return_rate=67.0,
                return_rate_success=None,
                return_rate_returned=None,
            ),
        ]
        res, already = classify_gip287_phase1_orders(orders)
        by_id = {r.order.id: r for r in res}
        self.assertEqual([o.id for o in already], ["ALREADY"])
        self.assertEqual(set(by_id.keys()), {"EEEZZLD", "NO_TIP"})
        self.assertEqual(by_id["EEEZZLD"].bucket, "NEED PAY ADVANCED")
        self.assertEqual(by_id["EEEZZLD"].action, "TAG_ONLY")
        self.assertEqual(by_id["NO_TIP"].bucket, "AMBIGUOUS")
        self.assertEqual(by_id["NO_TIP"].action, "REVIEW")

    def test_phase2_test_canceled_other_gip_no_multiple_returned_and_30d(self) -> None:
        # 1) TEST cancels first; 2) Canceled peer + other GIP only -> NO_ACTION even with Returned>=3;
        # 3) >30d Delivered order -> DIFFERENT PRODUCT
        o_test = OrderRecord(
            id="XZZJJX5",
            shop="GIP 287",
            customer_name="test",
            phone="09950826239",
            internal_note="31.CB5.G595.G576.E294.E339.E343|Combo",
            created_at="03:37:03 Today",
            status="New",
        )
        o_canceled_only = OrderRecord(
            id="EEEZZLD",
            shop="GIP 287",
            phone="09633518156",
            internal_note="31.CB7.G576.G354.E294.E339.E343.E612.E558|Combo",
            created_at="21:06:29 Yesterday",
            status="New",
        )
        o_old_30d = OrderRecord(
            id="977335A",
            shop="GIP 287",
            phone="09176358356",
            internal_note="33.CB4.G612.E531.E558.E292|Combo",
            created_at="00:12:58 Today",
            status="New",
        )
        histories = {
            "09950826239": PhoneHistory("09950826239", all_count=2, returned_count=0, orders=[o_test]),
            "09633518156": PhoneHistory(
                "09633518156",
                all_count=6,
                returned_count=7,
                orders=[
                    o_canceled_only,
                    OrderRecord(id="OLD_C", shop="GIP 287", status="Canceled", created_at="08:00:00 Yesterday"),
                    OrderRecord(id="OTHER_GIP", shop="GIP 170", status="Returned", created_at="08:00:00 10/11"),
                    OrderRecord(id="OTHER_R2", shop="GIP 170", status="Returned", created_at="08:00:00 09/11"),
                    OrderRecord(id="OTHER_R3", shop="GIP 170", status="Returned", created_at="08:00:00 08/11"),
                ],
            ),
            "09176358356": PhoneHistory(
                "09176358356",
                all_count=2,
                returned_count=0,
                orders=[
                    o_old_30d,
                    OrderRecord(
                        id="OLD_DELIV",
                        shop="GIP 287",
                        internal_note="33.CB4.G612.E531.E558.E292|Combo",
                        status="Delivered",
                        created_at="10:00:00 10/10",
                    ),
                ],
            ),
        }
        res, peers = classify_gip287_phase2_orders([o_test, o_canceled_only, o_old_30d], histories, REF_DT)
        self.assertEqual([r.bucket for r in res], ["TEST", "NO_ACTION", "DIFFERENT PRODUCT"])
        self.assertEqual([r.action for r in res], ["CANCEL", "NO_ACTION", "TAG_ONLY"])
        self.assertEqual(peers, [])

    def test_phase2_th1_th2_th3_and_partial_overlaps(self) -> None:
        # TH1: Book vs Ebook same number (G354 vs E354) -> DIFFERENT PRODUCT
        o_th1 = OrderRecord(
            id="TH1_EBOOK",
            shop="GIP 287",
            phone="09110000001",
            internal_note="31.CB2.E354.E294|Combo",
            created_at="09:00:00 Today",
            status="New",
        )
        # TH2: 100% same with Restocking -> DUPLICATE CANCEL
        o_th2 = OrderRecord(
            id="PRR6W5Y",
            shop="GIP 287",
            phone="09560450786",
            internal_note="31.CB4.G354.E294.E339.E343|Combo",
            created_at="16:22:58 Yesterday",
            status="New",
        )
        # TH3: Large New (3 items) vs smaller Pending peer (2 items) -> keep New, cancel Pending peer!
        o_th3_big = OrderRecord(
            id="NEW_BIG",
            shop="GIP 287",
            phone="09110000003",
            internal_note="31.CB3.G101.E102.E103|Combo",
            created_at="09:30:00 Today",
            status="New",
        )
        peer_th3_pending = OrderRecord(
            id="PEND_SMALL",
            shop="GIP 287",
            phone="09110000003",
            internal_note="31.CB2.G101.E103|Combo",
            created_at="08:00:00 Yesterday",
            status="Pending",
        )
        # TH3 guard: smaller peer in Restocking (> Pending) -> REVIEW (do not cancel!)
        o_th3_restock = OrderRecord(
            id="NEW_RESTOCK_BIG",
            shop="GIP 287",
            phone="09110000004",
            internal_note="31.CB3.G101.E102.E103|Combo",
            created_at="09:30:00 Today",
            status="New",
        )
        # Partial overlap: two active combos sharing items -> MULTIPLE ORDERS
        o_multi = OrderRecord(
            id="BVVRV68",
            shop="GIP 287",
            phone="09158435637",
            internal_note="31.CB4.G354.E294.E339.E343|Combo",
            created_at="09:00:00 Today",
            status="New",
        )
        # Partial overlap with final (<50%) -> DIFFERENT PRODUCT
        o_final_lt50 = OrderRecord(
            id="FINAL_LT50",
            shop="GIP 287",
            phone="09110000005",
            internal_note="31.CB4.G354.E294.E339.E343|Combo",
            created_at="09:00:00 Today",
            status="New",
        )
        # Partial overlap with final (>=50%) -> REVIEW
        o_final_ge50 = OrderRecord(
            id="FINAL_GE50",
            shop="GIP 287",
            phone="09110000006",
            internal_note="31.CB2.G354.E294|Combo",
            created_at="09:00:00 Today",
            status="New",
        )

        histories = {
            "09110000001": PhoneHistory(
                "09110000001",
                all_count=2,
                orders=[
                    o_th1,
                    OrderRecord(
                        id="P1",
                        shop="GIP 287",
                        internal_note="31.CB2.G354.E500|Combo",
                        status="Pending",
                        created_at="08:00:00 Yesterday",
                    ),
                ],
            ),
            "09560450786": PhoneHistory(
                "09560450786",
                all_count=2,
                orders=[
                    o_th2,
                    OrderRecord(
                        id="7EWM3YD",
                        shop="GIP 287",
                        internal_note="31.CB4.G354.E294.E339.E343|Combo",
                        status="Restocking",
                        created_at="15:35:09 19/11",
                    ),
                ],
            ),
            "09110000003": PhoneHistory(
                "09110000003",
                all_count=2,
                orders=[o_th3_big, peer_th3_pending],
            ),
            "09110000004": PhoneHistory(
                "09110000004",
                all_count=2,
                orders=[
                    o_th3_restock,
                    OrderRecord(
                        id="REST_SMALL",
                        shop="GIP 287",
                        internal_note="31.CB2.G101.E103|Combo",
                        status="Restocking",
                        created_at="08:00:00 Yesterday",
                    ),
                ],
            ),
            "09158435637": PhoneHistory(
                "09158435637",
                all_count=2,
                orders=[
                    o_multi,
                    OrderRecord(
                        id="6888AY9",
                        shop="GIP 287",
                        internal_note="31.CB3.G354.E294.E999|Combo",
                        status="Pending",
                        created_at="05:18:16 Yesterday",
                    ),
                ],
            ),
            "09110000005": PhoneHistory(
                "09110000005",
                all_count=2,
                orders=[
                    o_final_lt50,
                    OrderRecord(
                        id="DEL_1",
                        shop="GIP 287",
                        internal_note="31.CB2.G354.E999|Combo",
                        status="Delivered",
                        created_at="08:00:00 10/11",
                    ),
                ],
            ),
            "09110000006": PhoneHistory(
                "09110000006",
                all_count=2,
                orders=[
                    o_final_ge50,
                    OrderRecord(
                        id="DEL_2",
                        shop="GIP 287",
                        internal_note="31.CB2.G354.E999|Combo",
                        status="Returned",
                        created_at="08:00:00 10/11",
                    ),
                ],
            ),
        }

        orders = [o_th1, o_th2, o_th3_big, o_th3_restock, o_multi, o_final_lt50, o_final_ge50]
        res, peers = classify_gip287_phase2_orders(orders, histories, REF_DT)
        by_id = {r.order.id: r for r in res}

        self.assertEqual(by_id["TH1_EBOOK"].bucket, "DIFFERENT PRODUCT")
        self.assertEqual(by_id["PRR6W5Y"].bucket, "DUPLICATE")
        self.assertEqual(by_id["PRR6W5Y"].action, "CANCEL")
        self.assertEqual(by_id["NEW_BIG"].bucket, "NO_ACTION")
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].order.id, "PEND_SMALL")
        self.assertEqual(peers[0].action, "CANCEL")
        self.assertEqual(peers[0].bucket, "DUPLICATE")
        self.assertEqual(by_id["NEW_RESTOCK_BIG"].bucket, "AMBIGUOUS")
        self.assertEqual(by_id["BVVRV68"].bucket, "MULTIPLE ORDERS")
        self.assertEqual(by_id["FINAL_LT50"].bucket, "DIFFERENT PRODUCT")
        self.assertEqual(by_id["FINAL_GE50"].bucket, "AMBIGUOUS")

    def test_phase1_catches_non_duplicated_test_orders(self) -> None:
        orders = [
            OrderRecord(
                id="A558587",
                shop="GIP 287",
                customer_name="test",
                phone="09123654784",
                address="adadad",
                status="New",
            ),
            OrderRecord(
                id="NORMAL_1",
                shop="GIP 287",
                customer_name="Maria",
                phone="09111112222",
                status="New",
            ),
            OrderRecord(
                id="TESTER_1",
                shop="GIP 287",
                customer_name="Juan",
                internal_note="Buy 1 <12V CAR BATTERY TESTER ALTERNATOR TOOL>",
                status="New",
            ),
            OrderRecord(
                id="TEST_STRIPS_1",
                shop="GIP 287",
                customer_name="Ana",
                internal_note="Buy 1 <BLOOD SUGAR TEST STRIPS>",
                status="New",
            ),
            OrderRecord(
                id="RETEST_1",
                shop="GIP 287",
                customer_name="Pedro",
                note="retest customer",
                status="New",
            ),
        ]
        res = classify_gip287_phase1_test_orders(orders)
        by_id = {r.order.id: r for r in res}
        self.assertEqual(set(by_id.keys()), {"A558587"})
        self.assertEqual(by_id["A558587"].bucket, "TEST")
        self.assertEqual(by_id["A558587"].action, "CANCEL")


if __name__ == "__main__":
    unittest.main()
