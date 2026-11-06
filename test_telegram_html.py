"""Проверки без Telegram, ключей и запросов к нейросети."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import l2hub_tg as bot
from telegram_html import split_html


def plain(text):
    return "".join(ET.fromstring("<root>" + text + "</root>").itertext())


class HtmlMessagesTests(unittest.TestCase):
    def assert_chunks(self, source, limit=3900):
        chunks = split_html(source, limit)
        for chunk in chunks:
            self.assertLessEqual(len(plain(chunk).encode("utf-16-le")) // 2, limit)
        self.assertEqual("".join(plain(chunk) for chunk in chunks), plain(source))
        return chunks

    def test_link_crossing_old_raw_cut_is_not_truncated(self):
        source = "я" * 3880 + '<a href="https://example.com/long-task-name">читать</a>'
        with self.assertRaises(ET.ParseError):
            plain(source[:3899] + "…")
        self.assertEqual(self.assert_chunks(source), [source])

    def test_long_list_keeps_each_task_and_link(self):
        lines = [f'• Задача {n}: ' + "текст " * 12 + f'<a href="https://example.com/{n}">›</a>\n' for n in range(100)]
        chunks = self.assert_chunks("".join(lines))
        self.assertGreater(len(chunks), 1)
        for n in range(100):
            self.assertEqual(sum(f'href="https://example.com/{n}"' in chunk for chunk in chunks), 1)
        for line in lines:
            self.assertTrue(any(line in chunk for chunk in chunks))

    def test_long_line_keeps_emoji_entities_and_nested_tags(self):
        source = '<b><i><a href="https://example.com/?a=1&amp;b=2">' + "🙂 &amp; &lt; &#128640; " * 900 + "</a></i></b>"
        chunks = self.assert_chunks(source)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all('href="https://example.com/?a=1&amp;b=2"' in chunk for chunk in chunks))

    def test_formatting_spanning_lines_is_reopened(self):
        self.assert_chunks("<b>" + "строка &amp;\n" * 80 + "</b>", 40)

    def test_limit_boundary_has_no_extra_message(self):
        source = "<b>" + "я" * 3900 + "</b>"
        self.assertEqual(self.assert_chunks(source), [source])

    def test_malformed_html_is_rejected_before_sending(self):
        with patch.object(bot, "tg") as send:
            with self.assertRaises(ValueError):
                bot.post("SYSTEM", "<b>broken")
        send.assert_not_called()

    def test_every_part_is_sent_to_the_requested_topic(self):
        source = "<b>" + "длинная строка " * 1000 + "</b>"
        with patch.dict(os.environ, {"TG_CHAT_ID": "test", "TG_TOPIC_SYSTEM": "27"}), \
                patch.object(bot, "DRY", False), patch.object(bot, "tg") as send, patch.object(bot.time, "sleep"):
            bot.post("SYSTEM", source)
        self.assertGreater(send.call_count, 1)
        sent = []
        for call in send.call_args_list:
            method, data = call.args
            self.assertEqual(method, "sendMessage")
            self.assertEqual(data["message_thread_id"], 27)
            self.assertEqual(data["parse_mode"], "HTML")
            sent.append(data["text"])
        self.assertEqual("".join(plain(x) for x in sent), plain(source))

    def test_caption_entities_are_not_cut(self):
        caption = bot.esc("&" * 900)
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "example.zip"
            archive.write_bytes(b"test")
            with patch.dict(os.environ, {"TG_CHAT_ID": "test"}), patch.object(bot, "DRY", False), \
                    patch.object(bot, "tg") as send, patch.object(bot.time, "sleep"):
                bot.post_file("WORK", archive, caption)
        self.assertEqual(plain(send.call_args.args[1]["caption"]), "&" * 900)

    def test_dry_run_makes_no_requests(self):
        with patch.object(bot, "DRY", True), patch.object(bot, "tg") as send, patch.object(bot, "log") as log:
            bot.post("SYSTEM", "x" * 10000)
        send.assert_not_called()
        self.assertEqual(log.call_count, 3)

    def test_digest_is_recorded_only_after_all_parts_and_not_repeated(self):
        tasks = {f"task-{n}": {"title": "Большая задача " + "текст " * 20, "who": "MrKips", "status": "в работе"} for n in range(80)}
        before = {"digest": {"day": "2020-01-01", "released": [], "done": []}}
        with patch.dict(os.environ, {"TG_CHAT_ID": "test"}), patch.object(bot, "DRY", False), \
                patch.object(bot, "bridge_tasks", return_value=tasks), patch.object(bot, "release_tags", return_value=["v0.5.205"]), \
                patch.object(bot, "save_state") as save, patch.object(bot.time, "sleep"):
            state = copy.deepcopy(before)
            with patch.object(bot, "tg", side_effect=[{}, RuntimeError("temporary failure")]):
                with self.assertRaises(RuntimeError):
                    bot.maybe_digest(state)
            self.assertEqual(state, before)
            save.assert_not_called()
            with patch.object(bot, "tg") as send:
                bot.maybe_digest(state)
                sent = send.call_count
                self.assertGreater(sent, 1)
                bot.maybe_digest(state)
                self.assertEqual(send.call_count, sent)
            save.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
