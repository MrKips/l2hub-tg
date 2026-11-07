"""Единый снимок планов, безопасные подсказки и повторное обновление страниц."""
import copy
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import coordination as plans
import l2hub_tg as bot


def card(name, title="Работа", chronicle="High Five", status="в работе", **extra):
    fields = {"title": title, "who": "MrKips", "chronicle": chronicle, "status": status, **extra}
    return plans.parse_card(name, "---\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n---\nОписание")


def source(tree="tree"):
    return {"tasks_tree": tree, "commit": "commit", "updated": "2026-11-07T17:00:00+05:00", "ref": "origin/main"}


class PlanDataTests(unittest.TestCase):
    def test_ready_local_future_and_released_are_distinct(self):
        self.assertEqual(plans.stage(card("a", status="готово"), "v0.5.238"), "local")
        self.assertEqual(plans.stage(card("a", status="готово", release="v0.5.244"), "v0.5.238"), "accepted")
        self.assertEqual(plans.stage(card("a", status="готово", release="v0.5.224"), "v0.5.238"), "released")

    def test_status_conflicts_are_flags_not_edits(self):
        item = card("a")
        item["_body"] += "\n**Не начато.**\n"
        original = copy.deepcopy(item)
        findings = plans.issues([item])
        self.assertIn("Не начато", findings[0]["reason"])
        self.assertEqual(item, original)

    def test_other_chronicle_ready_feature_is_reuse_not_duplicate(self):
        cards = [card("ep", chronicle="Gracia Epilogue", feature="same", status="готово"),
                 card("hf", feature="same")]
        findings, relations = plans.intersections(cards, "v0.5.238")
        self.assertEqual(findings[0]["kind"], "reuse")
        self.assertEqual(relations, [])

    def test_same_feature_same_chronicle_is_only_possible_overlap(self):
        findings, _ = plans.intersections([card("a", feature="same"), card("b", feature="same", who="selaquik")], "v0.5.238")
        self.assertEqual(findings[0]["kind"], "possible_overlap")
        self.assertNotIn("confirmed", json.dumps(findings))

    def test_declared_port_and_duplicate_are_preserved_as_relations(self):
        base = card("base", feature="same", status="готово")
        port = card("port", feature="same", source="base", change="port", chronicle="Gracia Epilogue")
        findings, relations = plans.intersections([base, port], "v0.5.238")
        self.assertEqual(findings, [])
        self.assertEqual(relations[0]["kind"], "declared_source")
        marked = card("marked", duplicate_of="base")
        self.assertEqual(plans.intersections([base, marked], "v0.5.238")[1][0]["kind"], "marked_duplicate")

    def test_generic_bot_words_do_not_imply_overlap(self):
        findings, _ = plans.intersections([card("a", "High Five: боты"), card("b", "High Five: охотники")], "v0.5.238")
        self.assertEqual(findings, [])

    def test_common_shop_context_and_link_are_not_enough_to_suggest_overlap(self):
        editor = card("gm-shop-editor", "Редактор GM-шопа")
        price = card("gmshop-icarus", "Icarus в GM-шопе")
        price["references"] = [editor["id"]]
        findings, _ = plans.intersections([editor, price], "v0.5.238")
        self.assertEqual(findings, [])

    def test_query_keeps_other_chronicle_and_hides_irrelevant_registry(self):
        a = card("hf", "Краски по роли")
        b = card("ep", "Краски охотникам", chronicle="Gracia Epilogue", status="готово")
        c = card("index", "Полный реестр")
        c["_body"] += " Краски по роли"
        data = plans.report([b, c, a], source(), "v0.5.238", "краски", "High Five")
        self.assertEqual([item["id"] for item in data["cards"]], ["hf", "ep"])
        self.assertTrue(data["cards"][0]["target_chronicle_match"])
        self.assertFalse(data["cards"][1]["target_chronicle_match"])

    def test_sections_preserve_all_cards_and_escape_html(self):
        cards = [card(str(i), "Кланы <&> " + str(i)) for i in range(50)]
        data = plans.report(cards, source(), "v0.5.238")
        text = "\n".join(text for _, text in plans.sections(data))
        plain = "".join(ET.fromstring("<root>" + text + "</root>").itertext())
        self.assertIn("Кланы <&> 49", plain)
        for i in range(50):
            self.assertIn(f'tasks/{i}.md', text)
        for chunk in bot.split_html(text):
            ET.fromstring("<root>" + chunk + "</root>")

    def test_snapshot_excludes_local_uncommitted_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                r = subprocess.run(["git", "-C", temp, *args], capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                return r.stdout
            git("init", "-q")
            (root / "tasks").mkdir()
            task = root / "tasks/a.md"
            task.write_text("---\ntitle: Published\nstatus: план\n---\n", encoding="utf-8")
            git("add", "tasks/a.md")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            task.write_text("---\ntitle: Draft\nstatus: готово\n---\n", encoding="utf-8")
            cards, meta = plans.read_snapshot(root)
            self.assertEqual(cards[0]["title"], "Published")
            self.assertTrue(meta["local_changes_excluded"])
            self.assertIn("Draft", task.read_text(encoding="utf-8"))


class PlanTelegramTests(unittest.TestCase):
    def setUp(self):
        self.stack = []
        for manager in [patch.dict(os.environ, {"TG_CHAT_ID": "-1001234567", "TG_TOPIC_PLANS": "1270"}),
                        patch.object(bot, "DRY", False), patch.object(bot, "save_state"), patch.object(bot.time, "sleep")]:
            manager.start()
            self.addCleanup(manager.stop)
        self.cards = [card("a", "Проверка кланов")]

    def api(self, method, data):
        if method == "sendMessage":
            return {"message_id": self.send_count()}
        return True

    def send_count(self):
        self.counter = getattr(self, "counter", 100) + 1
        return self.counter

    def test_unconfigured_topic_is_inert(self):
        with patch.dict(os.environ, {"TG_TOPIC_PLANS": ""}), patch.object(plans, "read_snapshot") as read, patch.object(bot, "tg") as api:
            bot.sync_plans({})
        read.assert_not_called()
        api.assert_not_called()

    def test_initial_pages_then_unchanged_snapshot_makes_no_requests(self):
        state = {"release": "v0.5.238"}
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=self.api) as api:
            bot.sync_plans(state)
            initial = api.call_count
            self.assertGreater(initial, 1)
            self.assertTrue(all(call.args[1].get("disable_notification") == "true" for call in api.call_args_list))
            bot.sync_plans(state)
            self.assertEqual(api.call_count, initial)
        saved = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("Проверка кланов", saved)
        self.assertNotIn("1234567", saved)
        self.assertNotIn("1270", saved)

    def test_changed_task_edits_existing_pages_without_new_messages(self):
        state = {"release": "v0.5.238"}
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=self.api):
            bot.sync_plans(state)
        changed = [card("a", "Новая формулировка")]
        with patch.object(plans, "read_snapshot", return_value=(changed, source("new-tree"))), patch.object(bot, "tg", side_effect=self.api) as api:
            bot.sync_plans(state)
        self.assertTrue(api.call_count)
        self.assertTrue(all(call.args[0] == "editMessageText" for call in api.call_args_list))

    def test_partial_delivery_is_resumed_without_reposting_accepted_page(self):
        state = {"release": "v0.5.238"}
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), \
                patch.object(bot, "tg", side_effect=[{"message_id": 101}, RuntimeError("temporary failure")]):
            with self.assertRaises(RuntimeError):
                bot.sync_plans(state)
        self.assertEqual(state["plans"]["pages"]["work"][0]["id"], 101)
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=self.api) as api:
            bot.sync_plans(state)
        self.assertTrue(all("Проверка кланов" not in call.args[1].get("text", "") or call.args[0] != "sendMessage" for call in api.call_args_list))
        self.assertEqual(state["plans"]["tasks_tree"], "tree")

    def test_not_modified_response_is_success_but_other_errors_are_visible(self):
        state = {"release": "v0.5.238"}
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=self.api):
            bot.sync_plans(state)
        state["plans"].pop("tasks_tree")
        state["plans"]["pages"]["work"][0]["hash"] = "lost-checkpoint"
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=RuntimeError("Telegram editMessageText: Bad Request: message is not modified")):
            bot.sync_plans(state)
        state["plans"].pop("tasks_tree")
        state["plans"]["pages"]["work"][0]["hash"] = "lost-again"
        with patch.object(plans, "read_snapshot", return_value=(self.cards, source())), patch.object(bot, "tg", side_effect=RuntimeError("not allowed")):
            with self.assertRaisesRegex(RuntimeError, "not allowed"):
                bot.sync_plans(state)


if __name__ == "__main__":
    unittest.main()
