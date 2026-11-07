"""Общий read-only снимок карточек для людей и агентов. Исходные задачи не меняет."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import io
import itertools
import re
import subprocess
import tarfile
from urllib.parse import quote

WEB = "https://github.com/kraffty4/L2Hub-bridge/blob/main"
PEOPLE = {"selaquik": "Сергей", "kraffty": "Кирилл", "MrKips": "Илья"}
VERSION = re.compile(r"v?\d+\.\d+\.\d+$")
VIEW_VERSION = 1


def version_key(value):
    return tuple(int(n) for n in re.findall(r"\d+", value))


def scope(value):
    value = value.lower().replace("ё", "е").strip()
    if value in {"hf", "h5", "ct26_highfive", "high five"}:
        return "High Five"
    if value in {"ep", "epilogue", "gracia epilogue"}:
        return "Gracia Epilogue"
    if value in {"все", "общий", "all"}:
        return "все"
    return value


def administrative(card):
    title = card["title"].lower()
    return any(word in title for word in ("реестр", "порядок работы", "все незавершённые", "локальных id", "передача полного исходного", "сопровождение gracia")) or card["id"] == "shared-plans-preflight"


def parse_card(name, text):
    fields = {}
    if text.startswith("---"):
        for line in text.split("---", 2)[1].splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip().strip('"')
    card = {"id": name, "title": fields.get("title", name), "who": fields.get("who", "—"),
            "chronicle": fields.get("chronicle", "не указана"), "status": fields.get("status", "не указан"),
            "release": fields.get("release", ""), "updated": fields.get("updated", ""),
            "feature": fields.get("feature", ""), "change": fields.get("change", ""),
            "source": fields.get("source", ""), "duplicate_of": fields.get("duplicate_of", ""),
            "url": f"{WEB}/tasks/{quote(name)}.md", "_body": text}
    card["references"] = sorted(set(re.findall(r"\]\((?:\./|\.\./tasks/)?([\w-]+)\.md(?:#[^)]*)?\)", text)))
    body = text.split("---", 2)[-1].strip() if text.startswith("---") else text
    card["notes"] = body[:1600]
    return card


def read_snapshot(folder):
    """Берём опубликованный ref, не чужие незакоммиченные изменения рабочей папки."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("Не удалось прочитать снимок моста: " + args[0])
        return result.stdout
    check = subprocess.run(["git", "-C", str(folder), "rev-parse", "--verify", "origin/main"], capture_output=True)
    ref = "origin/main" if check.returncode == 0 else "HEAD"
    head = git("rev-parse", ref).decode().strip()
    tree = git("rev-parse", f"{ref}:tasks").decode().strip()
    stamp = git("log", "-1", "--format=%cI", ref, "--", "tasks").decode().strip()
    cards = []
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", ref, "tasks"))) as archive:
        for item in archive.getmembers():
            if item.isfile() and item.name.endswith(".md"):
                name = item.name.rsplit("/", 1)[-1][:-3]
                cards.append(parse_card(name, archive.extractfile(item).read().decode("utf-8")))
    dirty = bool(git("status", "--porcelain=v1").strip())
    return cards, {"ref": ref, "commit": head, "tasks_tree": tree, "updated": stamp, "local_changes_excluded": dirty}


def stage(card, latest):
    release = card["release"]
    if VERSION.fullmatch(release) and VERSION.fullmatch(latest or ""):
        return "released" if version_key(release) <= version_key(latest) else "accepted"
    return {"в работе": "work", "план": "plan", "свободна": "plan", "готово": "local", "брошена": "stopped"}.get(card["status"], "unknown")


def issues(cards):
    result = []
    ids = {card["id"] for card in cards}
    for card in cards:
        body = card["_body"]
        if card["status"] == "в работе" and re.search(r"(?im)^\s*\*{0,2}Не начато\b", body):
            result.append({"task": card["id"], "reason": "В статусе «в работе», а в тексте — «Не начато». Нужен актуальный этап."})
        if card["status"] == "в работе" and "статус карточки не меняли" in body.lower():
            result.append({"task": card["id"], "reason": "В тексте есть готовая реализация, а статус оставлен прежним. Уточнить оставшуюся работу и хронику."})
        if card["status"] == "в работе" and card["who"] in {"", "—", "-"}:
            result.append({"task": card["id"], "reason": "Работа отмечена начатой, но исполнитель не указан."})
        if card["change"] == "port" and not card["source"]:
            result.append({"task": card["id"], "reason": "Для переноса не указан источник реализации."})
        if card["duplicate_of"] and card["duplicate_of"] not in ids:
            result.append({"task": card["id"], "reason": "Ссылка на основную карточку дубля не найдена."})
    return result


STOP = {"high", "five", "gracia", "epilogue", "ep", "hf", "h5", "interlude", "classic", "essence", "local",
        "hunter", "hunters", "bot", "bots", "phantom", "phantoms",
        "для", "всех", "пока", "должны", "провер", "ботов", "боты", "охотни", "пешек", "работы", "полный"}
CONTEXT_WORDS = {"gm", "shop", "шоп", "гмшоп", "olympi", "олимпи", "patch", "code"}


def tokens(text):
    text = text.lower().replace("ё", "е").replace("gmshop", "gm shop")
    words = re.findall(r"[a-zа-я]+", text)
    result = set()
    for word in words:
        if len(word) < 4 and word not in {"gm", "mp", "hp", "sa"}:
            continue
        root = word[:6] if len(word) > 6 else word
        if root.startswith(("краск", "красок")):
            root = "краск"
        if root not in STOP and word not in STOP:
            result.add(root)
    return result


def intersections(cards, latest):
    """Только подсказки: совпадения не доказывают дубль кода и ничего не блокируют."""
    candidates, relations = [], []
    usable = [card for card in cards if not administrative(card) and card["status"] != "брошена"]
    for a, b in itertools.combinations(usable, 2):
        if b["id"] in {a["source"], a["duplicate_of"]} or a["id"] in {b["source"], b["duplicate_of"]}:
            kind = "marked_duplicate" if a["duplicate_of"] == b["id"] or b["duplicate_of"] == a["id"] else "declared_source"
            relations.append({"tasks": [a["id"], b["id"]], "kind": kind})
            continue
        explicit = bool(a["feature"] and a["feature"] == b["feature"])
        ta, tb = tokens(a["title"] + " " + a["id"]), tokens(b["title"] + " " + b["id"])
        shared = ta & tb
        linked = b["id"] in a["references"] or a["id"] in b["references"]
        similarity = len(shared) / max(1, len(ta | tb))
        if not explicit and (not (shared - CONTEXT_WORDS) or not ((len(shared) >= 2 and similarity >= 0.42) or (linked and shared))):
            continue
        overlap = scope(a["chronicle"]) == scope(b["chronicle"]) or "все" in {scope(a["chronicle"]), scope(b["chronicle"])}
        ready = any(stage(card, latest) in {"released", "accepted", "local"} for card in (a, b))
        if not overlap:
            kind = "reuse" if ready else "coordinate"
            reason = "Разные хроники: проверить готовую основу и необходимость переноса." if ready else "Похожая работа для разных хроник: согласовать общую основу."
        else:
            kind = "possible_overlap"
            reason = "Совпадает общая функция и хроника." if explicit else "Похожие или связанные карточки одной хроники: уточнить границы работ."
        score = 10 * explicit + 3 * linked + similarity + (a["who"] != b["who"])
        candidates.append({"tasks": [a["id"], b["id"]], "kind": kind, "reason": reason,
                           "evidence": "feature" if explicit else "link_and_terms" if linked else "similar_terms", "_score": score})
    candidates.sort(key=lambda item: -item.pop("_score"))
    return candidates, relations


def report(cards, source, latest, query="", chronicle=""):
    findings, relations = intersections(cards, latest)
    selected = cards
    if query:
        terms = tokens(query)
        selected = [card for card in cards if ((terms and terms <= tokens(card["title"] + " " + card["id"] + " " + card["_body"]))
                    and (not administrative(card) or terms & tokens(card["title"]))) or query.lower() == card["id"].lower()]
    if chronicle:
        selected = sorted(selected, key=lambda card: scope(card["chronicle"]) not in {scope(chronicle), "все"})
    selected_ids = {card["id"] for card in selected}
    public_cards = []
    for card in selected:
        item = {key: value for key, value in card.items() if not key.startswith("_")}
        item["stage"] = stage(card, latest)
        item["target_chronicle_match"] = not chronicle or scope(card["chronicle"]) in {scope(chronicle), "все"}
        public_cards.append(item)
    related_ids = {key for item in findings + relations if selected_ids.intersection(item["tasks"]) for key in item["tasks"]} - selected_ids
    related_cards = [{key: value for key, value in card.items() if not key.startswith("_")} for card in cards if card["id"] in related_ids]
    return {"source": source, "latest_published_release": latest, "query": query, "target_chronicle": chronicle,
            "cards": public_cards, "attention": [issue for issue in issues(cards) if issue["task"] in selected_ids],
            "related_cards": related_cards,
            "possible_intersections": [item for item in findings if selected_ids.intersection(item["tasks"])],
            "declared_relations": [item for item in relations if selected_ids.intersection(item["tasks"])],
            "notice": "Совпадения требуют просмотра исходных карточек и кода. Другая хроника может требовать переноса; готово у автора не равно общему выпуску."}


def card_link(card):
    title = card["title"].strip().strip('"')
    title = re.sub(r"^(?:Kraffty|MrKips|selaquik):\s*", "", title, flags=re.I)
    return f'<a href="{card["url"]}">{escape(title)}</a>'


def sections(data):
    grouped = defaultdict(list)
    cards = {card["id"]: card for card in data["cards"]}
    for card in cards.values():
        group = "organisation" if administrative(card) else card["stage"]
        grouped[group].append(card)
    headers = [("work", "🛠 Кто сейчас работает", "Исполнитель закреплён в карточке."),
               ("plan", "💡 Планы и идеи", "Ответственный за карточку указан; это ещё не начало работы."),
               ("local", "🧪 Готово у авторов", "Общий опубликованный выпуск пока не подтверждён."),
               ("accepted", "📦 Принято для будущих версий", "Эти версии новее последнего опубликованного выпуска."),
               ("released", "✅ Уже в опубликованных версиях", "Учитывайте указанную хронику; для получения изменений нужно обновление."),
               ("organisation", "🗂 Организация и учёт работ", "Реестры, передачи и общие договорённости."),
               ("unknown", "❔ Требуется уточнить состояние", "В карточке не хватает однозначного этапа."),
               ("stopped", "⏸ Остановленные работы", "Не считать активной разработкой.")]
    result = []
    for key, title, description in headers:
        entries = grouped.get(key, [])
        if not entries:
            continue
        lines = [f"<b>{title}</b> · {len(entries)}", description, ""]
        for card in sorted(entries, key=lambda item: (scope(item["chronicle"]), item["who"], item["title"])):
            owner = PEOPLE.get(card["who"], card["who"])
            release = f' · {card["release"]}' if card["release"] else ""
            lines.append(f'• {card_link(card)}\n{escape(owner)} · {escape(card["chronicle"] + release)}')
            if card["source"]:
                lines.append(f'Исходная работа: {escape(card["source"])} · тип: {escape(card["change"] or "уточнить")}.')
            if card["duplicate_of"]:
                lines.append(f'В карточке отмечена связь с основной задачей: {escape(card["duplicate_of"])}.')
        result.append((key, "\n".join(lines)))
    lines = ["<b>⚠ Что нужно сверить</b>", "Это замечания к данным, а не автоматическое решение о чужой работе.", ""]
    for issue in data["attention"]:
        lines.append(f'• {card_link(cards[issue["task"]])}\n{escape(issue["reason"])}')
    for item in data["possible_intersections"][:8]:
        a, b = (cards.get(key) for key in item["tasks"])
        if a and b:
            lines.append(f'• {card_link(a)} ↔ {card_link(b)}\n{escape(item["reason"])}')
    if not data["attention"] and not data["possible_intersections"]:
        lines.append("По имеющимся полям явных сигналов нет. Это не доказывает отсутствие пересечений в коде.")
    if len(data["possible_intersections"]) > 8:
        lines.append(f'Ещё возможных пересечений: {len(data["possible_intersections"]) - 8}. Полный вывод доступен агентам через команду plans --json.')
    lines += ["", "Подтверждают совпадения владельцы карточек после просмотра кода. Перенос между хрониками и разные этапы одной темы не считаются дублем автоматически."]
    result.append(("attention", "\n".join(lines)))
    return result


def overview(data, links):
    counts = defaultdict(int)
    for card in data["cards"]:
        counts[card["stage"]] += 1
    stamp = data["source"].get("updated", "")
    try:
        stamp = datetime.fromisoformat(stamp).astimezone(timezone(timedelta(hours=5))).strftime("%d.%m %H:%M")
    except ValueError:
        stamp = "не указано"
    lines = ["📌 <b>Общие планы L2Hub</b>", f'Карточек: {len(data["cards"])} · данные на {escape(stamp)} (Ташкент).',
             f'Последняя известная опубликованная версия: <b>{escape(data["latest_published_release"] or "не определена")}</b>.', ""]
    names = {"work": "Кто сейчас работает", "plan": "Планы и идеи", "local": "Готово у авторов",
             "accepted": "Принято для будущих версий", "released": "Уже опубликовано", "organisation": "Организация работы",
             "unknown": "Неясное состояние", "stopped": "Остановлено", "attention": "Пересечения и замечания"}
    for key, urls in links.items():
        line = f'<a href="{urls[0]}">{names[key]}</a>'
        for number, url in enumerate(urls[1:], 2):
            line += f' · <a href="{url}">стр. {number}</a>'
        lines.append(line)
    lines += ["", "Перед новой работой найдите похожую задачу, проверьте существующий код и хронику, затем закрепите исполнителя в карточке моста.",
              "Для доработки укажите, что уже существует и что добавляете; для переноса — исходную реализацию.",
              "Похожие названия — повод свериться, а не разрешение закрывать чужие задачи.",
              f'<a href="{WEB}/AGENTS.md">Действующие правила</a> · <a href="{WEB}/tasks">Все исходные карточки</a>',
              f'<a href="{WEB}/proposals/shared-plans.md">Предложение по общему порядку — на согласовании</a>']
    return "\n".join(lines)
