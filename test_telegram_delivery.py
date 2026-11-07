"""Лимиты Telegram и продолжение пачки после частичной отправки, без сети."""
import copy
import io
import os
import unittest
from unittest.mock import Mock, call, patch

import requests

import l2hub_tg as bot


def response(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


class DeliveryTests(unittest.TestCase):
    def test_retry_after_is_respected_and_success_returned(self):
        limited = response({"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}, 429)
        ok = response({"ok": True, "result": {"message_id": 10}})
        payload = {"chat_id": "example", "text": "news"}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", side_effect=[limited, ok]) as post, \
                patch.object(bot.time, "sleep") as sleep, patch.object(bot, "log"):
            result = bot.tg("sendMessage", payload)
        self.assertEqual(result, {"message_id": 10})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        sleep.assert_called_once_with(13)

    def test_retry_count_is_bounded_when_limit_persists(self):
        limited = response({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 2}}, 429)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", return_value=limited) as post, \
                patch.object(bot.time, "sleep") as sleep, patch.object(bot, "log"):
            with self.assertRaisesRegex(RuntimeError, "Too Many Requests"):
                bot.tg("sendMessage", {})
        self.assertEqual(post.call_count, bot.TG_RATE_RETRIES + 1)
        self.assertEqual(sleep.call_args_list, [call(3)] * bot.TG_RATE_RETRIES)

    def test_success_is_not_retried(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", return_value=response({"ok": True, "result": True})) as post, \
                patch.object(bot.time, "sleep") as sleep:
            self.assertTrue(bot.tg("sendMessage", {}))
        post.assert_called_once()
        sleep.assert_not_called()

    def test_other_telegram_errors_are_not_hidden_or_retried(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", return_value=response({"ok": False, "error_code": 400, "description": "bad HTML"}, 400)) as post, \
                patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "bad HTML"):
                bot.tg("sendMessage", {})
        post.assert_called_once()
        sleep.assert_not_called()

    def test_uncertain_network_failure_is_not_blindly_reposted(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", side_effect=requests.Timeout("unknown delivery status")) as post:
            with self.assertRaises(requests.Timeout):
                bot.tg("sendMessage", {})
        post.assert_called_once()

    def test_file_is_rewound_before_retry(self):
        stream = io.BytesIO(b"archive-data")
        received = []

        def upload(*args, **kwargs):
            received.append(kwargs["files"]["document"][1].read())
            if len(received) == 1:
                return response({"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}, 429)
            return response({"ok": True, "result": {}})

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(bot.requests, "post", side_effect=upload), \
                patch.object(bot.time, "sleep"), patch.object(bot, "log"):
            bot.tg("sendDocument", {}, {"document": ("archive.zip", stream)})
        self.assertEqual(received, [b"archive-data", b"archive-data"])

    def test_message_parts_are_paced_below_group_limit(self):
        with patch.dict(os.environ, {"TG_CHAT_ID": "test"}), patch.object(bot, "DRY", False), \
                patch.object(bot, "tg") as send, patch.object(bot.time, "sleep") as sleep:
            bot.post("SYSTEM", "x" * 10000)
        self.assertEqual(send.call_count, 3)
        self.assertGreater(bot.TG_SEND_INTERVAL, 60 / 20)
        self.assertEqual(sleep.call_args_list, [call(bot.TG_SEND_INTERVAL)] * 3)

    def test_bridge_resumes_after_last_accepted_news_even_if_head_changes(self):
        state = {"bridge": "old"}
        records = {}
        snapshots = []
        sent = []

        def git(*args):
            if args[0] == "pull":
                return ""
            if args[0] == "rev-parse":
                return "head-" + str(len(records))
            if args[0] == "diff":
                return "".join("A\t" + name + "\n" for name in records)
            if args[0] == "log":
                return "author\n"
            raise AssertionError(args)

        def deliver(where, text):
            if "News two" in text:
                raise RuntimeError("limit persists")
            sent.append(text)

        records.update({"updates/one.md": "# News one\n**Что изменилось:** first", "updates/two.md": "# News two\n**Что изменилось:** second"})
        with patch.object(bot, "git", side_effect=git), patch.object(bot, "show", side_effect=lambda rev, path: records[path]), \
                patch.object(bot, "save_state", side_effect=lambda s: snapshots.append(copy.deepcopy(s))):
            with patch.object(bot, "post", side_effect=deliver):
                with self.assertRaisesRegex(RuntimeError, "limit persists"):
                    bot.watch_bridge(state)
            self.assertEqual(state["bridge"], "old")
            self.assertEqual(len(state["bridge_sent"]), 1)
            self.assertEqual(snapshots[-1], state)
            records["updates/three.md"] = "# News three\n**Что изменилось:** third"
            with patch.object(bot, "post", side_effect=lambda where, text: sent.append(text)):
                bot.watch_bridge(state)
        self.assertEqual(sum("News one" in text for text in sent), 1)
        self.assertEqual(sum("News two" in text for text in sent), 1)
        self.assertEqual(sum("News three" in text for text in sent), 1)
        self.assertEqual(state["bridge"], "head-3")
        self.assertNotIn("bridge_sent", state)


if __name__ == "__main__":
    unittest.main()
