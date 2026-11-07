"""Понятные сообщения, достоверные статусы и совместимость отметок доставки."""
import hashlib
import json
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import l2hub_tg as bot
import system_text


class ReadableSystemTests(unittest.TestCase):
    def setUp(self):
        cache = patch.dict(system_text._CACHE, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        offline = patch.object(bot, "ask_llm", return_value=None)
        offline.start()
        self.addCleanup(offline.stop)

    def test_one_request_for_batch_and_cache_stays_only_in_memory(self):
        sources = [{"kind": "news", "title": "Code changes", "details": "Epilogue only; client not tested"},
                   {"kind": "task", "title": "PhantomCompat: настройки"}]
        ask = Mock(return_value=json.dumps({"items": [
            {"id": "0", "title": "Улучшения для Эпилога", "summary": "В Gracia Epilogue изменили поведение помощников. В игре ещё нужно проверить."},
            {"id": "1", "title": "Настройки помощников", "summary": "release: v99.99.99"}]}))
        first = system_text.describe(sources, ask)
        self.assertEqual(system_text.describe(sources, ask), first)
        ask.assert_called_once()
        self.assertEqual(len(json.loads(ask.call_args.args[1])["items"]), 2)
        self.assertEqual(first[1]["summary"], "")
        self.assertIn("Gracia Epilogue", first[0]["summary"])

    def test_invalid_or_partial_reply_keeps_every_source(self):
        sources = [{"kind": "news", "title": "MrKips: Управление ивентами", "details": "local only"},
                   {"kind": "task", "title": "Проверка навыков"}]
        answer = {"items": [{"id": "1", "title": "Навыки в бою", "summary": ""},
                            {"id": "99", "title": "ignore"}, {"id": "-1", "title": "ignore"}]}
        results = system_text.describe(sources, Mock(return_value=json.dumps(answer)))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Управление ивентами")
        self.assertIn("недоступно", results[0]["summary"])
        self.assertEqual(results[1]["title"], "Навыки в бою")
        result = system_text.describe([{"kind": "news", "title": "Ещё новость"}], Mock(return_value="not JSON"))
        self.assertIn("недоступно", result[0]["summary"])

    def test_fenced_json_is_supported_and_bad_field_types_are_ignored(self):
        source = [{"kind": "task", "title": "Original"}]
        reply = '```json\n{"items": [{"id":"0","title":"Понятное название","summary":""}]}\n```'
        self.assertEqual(system_text.describe(source, Mock(return_value=reply))[0]["title"], "Понятное название")
        other = [{"kind": "news", "title": "Other"}]
        invalid = '{"items":[{"id":"0","title":42,"summary":[]}]}'
        self.assertEqual(system_text.describe(other, Mock(return_value=invalid))[0]["title"], "Other")

    def test_finished_without_release_is_not_presented_as_published(self):
        status = bot.task_status({"status": "готово"}, "v0.5.238")
        self.assertIn("Автор закончил", status)
        self.assertIn("не подтверждено", status)

    def test_published_and_future_versions_have_distinct_status(self):
        self.assertIn("опубликованную", bot.task_status({"release": "v0.5.224"}, "v0.5.238"))
        future = bot.task_status({"release": "v0.5.254"}, "v0.5.238")
        self.assertIn("выпуска ещё ждём", future)
        self.assertNotIn("опубликованную", future)
        self.assertIn("пока не проверена", bot.task_status({"release": "v0.5.254"}, ""))

    def test_plan_and_work_do_not_become_finished(self):
        self.assertIn("план", bot.task_status({"status": "план"}, "v0.5.238"))
        self.assertIn("сейчас работают", bot.task_status({"status": "в работе"}, "v0.5.238"))

    def test_task_scope_is_kept_outside_generated_title(self):
        line = bot.task_line("internal-id", {"title": "raw", "who": "selaquik", "chronicle": "Gracia Epilogue"}, "Поведение воинов")
        self.assertIn("Сергей", line)
        self.assertIn("Gracia Epilogue", line)
        self.assertNotIn("internal-id", "".join(ET.fromstring("<root>" + line + "</root>").itertext()))

    def test_changed_release_produces_readable_task_update(self):
        old = "---\ntitle: Настройки охотников\nstatus: готово\nwho: selaquik\nchronicle: Gracia Epilogue\n---"
        new = old.replace("---\ntitle", "---\nrelease: v0.5.254\ntitle")

        def git(*args):
            return {"pull": "", "rev-parse": "new", "diff": "M\ttasks/internal-id.md", "log": "author"}[args[0]]

        state = {"bridge": "old", "release": "v0.5.238"}
        with patch.object(bot, "git", side_effect=git), patch.object(bot, "show", side_effect=lambda rev, path: old if rev == "old" else new), \
                patch.object(bot, "post") as post, patch.object(bot, "save_state"):
            bot.watch_bridge(state)
        sent = post.call_args.args[1]
        plain = "".join(ET.fromstring("<root>" + sent + "</root>").itertext())
        self.assertIn("Настройки охотников", plain)
        self.assertIn("Gracia Epilogue", plain)
        self.assertIn("выпуска ещё ждём", plain)
        self.assertNotIn("internal-id", plain)

    def test_old_delivery_marker_survives_new_wording(self):
        path = "updates/one.md"
        text = "# Raw title\n**Что изменилось:** raw details"
        canonical = f'📰 <b>Raw title</b>\nraw details\n<a href="{bot.BRIDGE_WEB}/{path}">читать</a>'
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        state = {"bridge": "old", "bridge_sent": [key]}

        def git(*args):
            return {"pull": "", "rev-parse": "new", "diff": "A\t" + path, "log": "author"}[args[0]]

        with patch.object(bot, "git", side_effect=git), patch.object(bot, "show", return_value=text), \
                patch.object(bot, "describe", return_value=[{"title": "Совсем другое название", "summary": "Другое объяснение"}]), \
                patch.object(bot, "post") as post, patch.object(bot, "save_state"):
            bot.watch_bridge(state)
        post.assert_not_called()
        self.assertEqual(state, {"bridge": "new"})

    def test_generated_html_is_escaped_and_not_saved_in_public_state(self):
        text = "# Raw title\n**Что изменилось:** raw details"

        def git(*args):
            return {"pull": "", "rev-parse": "new", "diff": "A\tupdates/one.md", "log": "author"}[args[0]]

        states = []
        state = {"bridge": "old"}
        with patch.object(bot, "git", side_effect=git), patch.object(bot, "show", return_value=text), \
                patch.object(bot, "describe", return_value=[{"title": "Кланы <&>", "summary": "<b>Текст & детали</b>"}]), \
                patch.object(bot, "post") as post, patch.object(bot, "save_state", side_effect=lambda s: states.append(json.dumps(s))):
            bot.watch_bridge(state)
        sent = post.call_args.args[1]
        root = ET.fromstring("<root>" + sent + "</root>")
        self.assertIn("<b>Текст & детали</b>", "".join(root.itertext()))
        self.assertTrue(all("Текст" not in snapshot and "summary" not in snapshot for snapshot in states))


if __name__ == "__main__":
    unittest.main()
