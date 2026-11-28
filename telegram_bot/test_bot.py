"""Unit tests for the standalone Telegram auto-reply rule engine."""

from __future__ import annotations

import unittest

from telegram_bot.bot import (
    DEFAULT_RULES_PATH,
    load_rules,
    match_rule,
    normalize_vn,
    render_template,
    sender_matches,
    text_matches,
)


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


if __name__ == "__main__":
    unittest.main()
