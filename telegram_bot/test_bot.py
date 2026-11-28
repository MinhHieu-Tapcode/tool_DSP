"""Unit tests for the standalone Telegram auto-reply rule engine."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from telegram_bot.bot import (
    DEFAULT_RULES_PATH,
    evaluate_scheduled_reminders,
    load_rules,
    match_reminder_ack,
    match_rule,
    normalize_vn,
    render_reminder_message,
    render_template,
    sender_matches,
    text_matches,
)

TZ_VN = timezone(timedelta(hours=7))


class TelegramBotRuleTests(unittest.TestCase):
    def test_normalize_vn_handles_accents_and_spaces(self) -> None:
        self.assertEqual(
            normalize_vn("  Anh    Xử Lý DATA giúp em nhé!!  "),
            "anh xu ly data giup em nhe",
        )
        self.assertEqual(normalize_vn("Đổi Đơn Đi"), "doi don di")

    def test_exact_vs_contains_matching(self) -> None:
        phrases = ["anh xu ly data giup em nhe"]
        self.assertTrue(text_matches("anh      xử lý data giúp em nhé", phrases, "exact"))
        self.assertFalse(text_matches("Tool check trùng à :)))", phrases, "exact"))
        self.assertFalse(text_matches("hôm nay anh xử lý data giúp em nhé bạn ơi", phrases, "exact"))
        self.assertTrue(text_matches("hôm nay anh xử lý data giúp em nhé bạn ơi", phrases, "contains"))

    def test_sender_matches_by_id_username_and_name(self) -> None:
        sender = {"id": 123456789, "username": "LadaStaff", "first_name": "Lada", "last_name": "PH"}
        self.assertTrue(sender_matches(sender, []))
        self.assertTrue(sender_matches(sender, ["123456789"]))
        self.assertTrue(sender_matches(sender, ["@ladastaff"]))
        self.assertTrue(sender_matches(sender, ["Lada"]))
        self.assertFalse(sender_matches(sender, ["999999999", "@other"]))

    def test_match_rule_enforces_chat_sender_bot_and_enabled(self) -> None:
        cfg = {
            "enabled": True,
            "allowed_chat_ids": ["-5482707500"],
            "rules": [
                {
                    "name": "xu_ly_data",
                    "enabled": True,
                    "allowed_users": ["@lada", "111222"],
                    "match_mode": "exact",
                    "phrases": ["anh xu ly data giup em nhe"],
                    "reply": "Vâng ạ 😊",
                }
            ],
        }
        ok_sender = {"id": 111222, "username": "minh", "first_name": "Minh", "is_bot": False}
        other_sender = {"id": 999, "username": "stranger", "first_name": "Guest", "is_bot": False}
        bot_sender = {"id": 111222, "username": "lada", "is_bot": True}

        self.assertIsNotNone(match_rule("-5482707500", ok_sender, "Anh xử lý data giúp em nhé", cfg))
        self.assertIsNone(match_rule("-999999", ok_sender, "Anh xử lý data giúp em nhé", cfg))
        self.assertIsNone(match_rule("-5482707500", other_sender, "Anh xử lý data giúp em nhé", cfg))
        self.assertIsNone(match_rule("-5482707500", bot_sender, "Anh xử lý data giúp em nhé", cfg))
        self.assertIsNone(match_rule("-5482707500", ok_sender, "Tool check trùng à :)))", cfg))

        cfg_off = dict(cfg, enabled=False)
        self.assertIsNone(match_rule("-5482707500", ok_sender, "Anh xử lý data giúp em nhé", cfg_off))

    def test_default_rules_json_loads_and_avoids_casual_chat(self) -> None:
        cfg = load_rules(DEFAULT_RULES_PATH)
        sender = {"id": 777, "first_name": "GIP", "is_bot": False}
        self.assertIsNotNone(match_rule("-5482707500", sender, "anh xử lý data giúp em nhé", cfg))
        self.assertIsNotNone(match_rule("-5482707500", sender, "a xử lý data giúp em nha", cfg))
        self.assertIsNone(match_rule("-5482707500", sender, "Tool check trùng à :)))", cfg))
        self.assertIsNone(match_rule("-5482707500", sender, "hnhu tool ăn theo chữ check trùng", cfg))

    def test_render_update_link_template_mentions(self) -> None:
        cfg = load_rules(DEFAULT_RULES_PATH)
        txt1, pm1 = render_template("update_link", cfg, tag="@gipsupport")
        self.assertIsNone(pm1)
        self.assertIn("Chị @gipsupport ơi, em vừa update thông tin mới vào link, chị check link giúp em với ạ", txt1)
        self.assertIn("https://docs.google.com/spreadsheets/d/10Zoz39Ltskaw94AkSCGqCJce1X7jaHfBB2nbefLSZUw/edit", txt1)

        txt2, pm2 = render_template("update_link", cfg, tag="gipsupport")
        self.assertEqual(txt1, txt2)
        self.assertIsNone(pm2)

        txt_html, pm_html = render_template("update_link", cfg, tag="Tuyết", user_id="987654321")
        self.assertEqual(pm_html, "HTML")
        self.assertIn('<a href="tg://user?id=987654321">Tuyết</a>', txt_html)

    def test_scheduled_reminders_0800_1330_escalation_and_lada_guard(self) -> None:
        cfg = copy.deepcopy(load_rules(DEFAULT_RULES_PATH))
        rem = cfg["scheduled_reminders"][0]
        self.assertTrue(rem["enabled"])
        self.assertEqual(rem["primary_mention"]["user_id"], "7374545108")

        # Setting enabled=False suppresses firing
        cfg_off = copy.deepcopy(cfg)
        cfg_off["scheduled_reminders"][0]["enabled"] = False
        t_0800 = datetime(2026, 11, 29, 8, 1, 0, tzinfo=TZ_VN)
        self.assertEqual(evaluate_scheduled_reminders(cfg_off, {"reminders": {}}, t_0800), [])

        state = {"reminders": {}}
        # Before 08:00 -> nothing
        self.assertEqual(
            evaluate_scheduled_reminders(cfg, state, datetime(2026, 11, 29, 7, 59, 0, tzinfo=TZ_VN)),
            [],
        )
        # 08:01 -> fires initial once, sets 60-min escalation
        acts = evaluate_scheduled_reminders(cfg, state, t_0800)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["kind"], "initial")
        init_txt, pmode = render_reminder_message(acts[0]["reminder"], is_escalation=False)
        self.assertEqual(pmode, "HTML")
        self.assertIn('<a href="tg://user?id=7374545108">Tuyết</a>', init_txt)
        self.assertNotIn("@SA_ThaoNT18", init_txt)

        # Duplicate check at 08:02 -> does not double-fire
        self.assertEqual(
            evaluate_scheduled_reminders(cfg, state, datetime(2026, 11, 29, 8, 2, 0, tzinfo=TZ_VN)),
            [],
        )
        # At 08:59 -> escalation not due yet
        self.assertEqual(
            evaluate_scheduled_reminders(cfg, state, datetime(2026, 11, 29, 8, 59, 0, tzinfo=TZ_VN)),
            [],
        )
        # At 09:01 (>= 60 min later, still unacknowledged) -> fires escalation with both Tuyet & @SA_ThaoNT18
        esc_acts = evaluate_scheduled_reminders(cfg, state, datetime(2026, 11, 29, 9, 1, 0, tzinfo=TZ_VN))
        self.assertEqual(len(esc_acts), 1)
        self.assertEqual(esc_acts[0]["kind"], "escalation")
        esc_txt, _ = render_reminder_message(esc_acts[0]["reminder"], is_escalation=True)
        self.assertIn('<a href="tg://user?id=7374545108">Tuyết</a> @SA_ThaoNT18', esc_txt)

        # Afternoon 13:30 slot fires; Tuyet replies "Anh xử lý data giúp em nhé" -> cancels escalation
        t_1330 = datetime(2026, 11, 29, 13, 32, 0, tzinfo=TZ_VN)
        pm_acts = evaluate_scheduled_reminders(cfg, state, t_1330)
        self.assertEqual(len(pm_acts), 1)
        self.assertEqual(pm_acts[0]["kind"], "initial")

        tuyet = {"id": 7374545108, "first_name": "Tuyết Nhung", "is_bot": False}
        # Lada's phrase ("Chị xử lý data giúp em nhé") MUST be ignored!
        self.assertIsNone(match_reminder_ack("-5474079036", tuyet, "Chị xử lý data giúp em nhé", cfg, state))
        # Tuyet's phrase for Minh ("Anh xử lý data giúp em nhé") matches & clears escalation
        ack = match_reminder_ack("-5474079036", tuyet, "Anh xử lý data giúp em nhé", cfg, state)
        self.assertIsNotNone(ack)
        self.assertEqual(ack["reply"], "dạ vâng ạ")
        # 60+ minutes later at 14:35 -> no escalation because Tuyet already acknowledged
        self.assertEqual(
            evaluate_scheduled_reminders(cfg, state, datetime(2026, 11, 29, 14, 35, 0, tzinfo=TZ_VN)),
            [],
        )

        # Thao's phrase ("Em chia cho sale nhé") also matches for @SA_ThaoNT18
        thao = {"id": 555444, "username": "SA_ThaoNT18", "first_name": "Thảo Nguyễn", "is_bot": False}
        ack_thao = match_reminder_ack("-5474079036", thao, "Em chia cho sale nhé", cfg, state)
        self.assertIsNotNone(ack_thao)
        self.assertEqual(ack_thao["reply"], "dạ vâng ạ")

        # Per-rule chat_ids isolation: general xu_ly_data does not bleed into -5474079036
        self.assertIsNone(match_rule("-5474079036", tuyet, "anh xử lý data giúp em nhé", cfg))


if __name__ == "__main__":
    unittest.main()
